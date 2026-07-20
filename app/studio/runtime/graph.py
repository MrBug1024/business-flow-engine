"""LangGraph-backed execution runtime for AI Business Studio."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, time
from typing import Any, Literal

from jsonschema import Draft202012Validator
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    SummarizationMiddleware,
    wrap_tool_call,
)
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.core.config import settings
from app.studio.runtime.agent import _safe_arguments, _system_prompt
from app.studio.runtime.capabilities import (
    Capability,
    CapabilityResult,
    discover_capabilities,
    execute_capability,
    optional_capability_catalog,
    result_for_model,
)
from app.studio.runtime.ledger import ToolExecutionLedger
from app.studio.runtime.model_adapter import ModelTurn, StudioChatModel
from app.studio.models import AIRun, BusinessRecord
from app.studio.capabilities.registry import SYSTEM_SKILLS_ROOT, list_skills
from app.studio.runtime.sandbox import sandbox_manager
from app.studio.capabilities.skill_middleware import ReloadingSkillsMiddleware
from app.studio.storage import new_id, store


SKILL_SOURCE = "/skills/"
CONTEXT_SUMMARY_PROMPT = """You compress execution context for an ongoing AI Business
Studio task. Preserve only information needed to continue accurately. Do not invent
business steps. Return these concise sections: USER OBJECTIVE, AGREEMENTS AND
ASSUMPTIONS, ACTIVE WORK ITEM AND WHY, VERIFIED RESULTS, ARTIFACTS/CHECKPOINTS,
BLOCKERS, NEXT STEP. Preserve exact workspace paths, identifiers, validation errors,
and user decisions. Omit repetitive tool transcripts and superseded attempts.

<messages>
{messages}
</messages>"""
_BUSINESS_LOCKS: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
_FILE_TOOL_OPERATIONS = {
    "ls": "list",
    "read_file": "read",
    "write_file": "create",
    "edit_file": "edit",
    "glob": "search",
    "grep": "search",
}
_WORKSPACE_SNAPSHOT_LIMIT = 10_000


class _CapabilityDiscoveryInput(BaseModel):
    kind: Literal["all", "skill", "tool", "mcp"] = Field(
        default="all",
        description="Capability family to search.",
    )
    query: str = Field(
        default="",
        max_length=300,
        description="Short task or capability search phrase.",
    )
    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0)
    include_schema: bool = Field(
        default=False,
        description="Include the input schema for only the best matching MCP result.",
    )


class _MCPGatewayInput(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=128,
        description="Exact MCP capability name returned by discover_studio_capabilities.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments matching the discovered MCP input_schema.",
    )


class _ToolGatewayInput(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=128,
        description="Exact Tool name returned by discover_studio_capabilities.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments matching the discovered Tool input_schema.",
    )


class _StudioSummarizationMiddleware(SummarizationMiddleware):
    """Emit one semantic event when LangChain actually compacts message history."""

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", []) if isinstance(state, dict) else []
        token_count = self.token_counter(messages)
        update = super().before_model(state, runtime)
        if update:
            runtime.stream_writer(
                {
                    "type": "context_compaction",
                    "status": "completed",
                    "title": "上下文已自动压缩",
                    "summary": "保留目标、共识、已验证结果、产物和下一步，继续同一任务。",
                    "token_count_before": token_count,
                }
            )
        return update


@dataclass(slots=True)
class _Invocation:
    call_id: str
    capability: Capability
    result: CapabilityResult | None = None
    replayed: bool = False
    error: str = ""


@dataclass(slots=True)
class _StreamedFileDraft:
    index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    emitted_content: str = ""
    announced: bool = False


class _SkillTrace:
    """Run-local identity map for real Skill activations and their child actions."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._ids: dict[str, str] = {}

    def activate(self, skill_name: str) -> str:
        skill_id = self._ids.get(skill_name)
        if skill_id is None:
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", skill_name).strip("_") or "skill"
            skill_id = f"skill_{self.run_id}_{slug}"
            self._ids[skill_name] = skill_id
        return skill_id

    def parent_for(self, skill_name: str | None) -> str | None:
        return self._ids.get(skill_name or "")

    @property
    def active_names(self) -> tuple[str, ...]:
        return tuple(self._ids)


_CURRENT_INVOCATION: ContextVar[_Invocation | None] = ContextVar(
    "studio_capability_invocation",
    default=None,
)


class StudioGraphRuntime:
    """Run the model/tool loop with durable LangGraph checkpoints."""

    def __init__(
        self,
        *,
        checkpointer: Any | None = None,
        ledger: ToolExecutionLedger | None = None,
        model_turn: ModelTurn | None = None,
    ) -> None:
        runtime_root = settings.data_path / "business_studio" / "runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection | None = None
        if checkpointer is None:
            self._connection = sqlite3.connect(
                runtime_root / "checkpoints.sqlite3",
                check_same_thread=False,
            )
            checkpointer = SqliteSaver(self._connection)
            checkpointer.setup()
        self.checkpointer = checkpointer
        self.ledger = ledger or ToolExecutionLedger(runtime_root / "tool_ledger.sqlite3")
        self.model_turn = model_turn or _live_model_turn

    def stream(
        self,
        record: BusinessRecord,
        run: AIRun,
        *,
        requested_model: str | None,
        user_prompt: str | None,
        include_history: bool = True,
        resume_payload: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        discovered_capabilities = discover_capabilities(record)
        direct_protocols = {"task_progress", "user_input", "workspace_file"}
        direct_capabilities = [
            item
            for item in discovered_capabilities
            if item.kind == "tool" and item.protocol in direct_protocols
        ]
        optional_tool_capabilities = [
            item
            for item in discovered_capabilities
            if item.kind == "tool" and item.protocol not in direct_protocols
        ]
        mcp_capabilities = [
            item for item in discovered_capabilities if item.kind == "mcp"
        ]
        reserved_names = {"discover_studio_capabilities", "call_tool", "call_mcp"}
        conflicts = sorted(
            item.function_name
            for item in direct_capabilities
            if item.function_name in reserved_names
        )
        if conflicts:
            raise RuntimeError(
                "Project Tool names conflict with Studio capability gateways: "
                + ", ".join(conflicts)
            )
        gateway_capabilities = self._gateway_capabilities(
            optional_tool_capabilities,
            mcp_capabilities,
            record,
            run,
        )
        capabilities = [*direct_capabilities, *gateway_capabilities]
        capability_map = {item.function_name: item for item in capabilities}
        tools = [self._tool_for(item, record, run) for item in capabilities]
        skill_trace = _SkillTrace(run.id)
        tool_middleware = self._tool_middleware(record, run, capability_map, skill_trace)
        model = StudioChatModel(
            record=record,
            requested_model=requested_model,
            model_turn=self.model_turn,
        )
        sandbox = sandbox_manager.backend_for(
            business_id=record.id,
            workspace_root=store.workspace_dir(record.id),
            skills_root=SYSTEM_SKILLS_ROOT,
        )
        backend = CompositeBackend(
            default=sandbox,
            routes={
                SKILL_SOURCE: FilesystemBackend(
                    root_dir=SYSTEM_SKILLS_ROOT,
                    virtual_mode=True,
                )
            },
        )
        skills_middleware = ReloadingSkillsMiddleware(
            backend=backend,
            sources=[(SKILL_SOURCE, "Studio")],
            system_prompt=None,
        )
        filesystem_middleware = FilesystemMiddleware(
            backend=backend,
            system_prompt="",
            max_execute_timeout=settings.sandbox_command_timeout,
            _permissions=[
                FilesystemPermission(
                    operations=["write"],
                    paths=[f"{SKILL_SOURCE}**"],
                    mode="deny",
                )
            ],
        )
        agent = create_agent(
            model,
            tools,
            system_prompt=_system_prompt(record),
            middleware=[
                skills_middleware,
                filesystem_middleware,
                _StudioSummarizationMiddleware(
                    model,
                    trigger=("tokens", max(4_000, settings.agent_context_summarize_tokens)),
                    keep=(
                        "tokens",
                        max(
                            2_000,
                            min(
                                settings.agent_context_keep_tokens,
                                settings.agent_context_summarize_tokens // 2,
                            ),
                        ),
                    ),
                    summary_prompt=CONTEXT_SUMMARY_PROMPT,
                ),
                ModelRetryMiddleware(max_retries=2, on_failure="error"),
                ModelCallLimitMiddleware(
                    run_limit=max(1, settings.agent_model_call_limit),
                    exit_behavior="error",
                ),
                tool_middleware,
            ],
            checkpointer=self.checkpointer,
            name="ai_business_studio",
        )

        thread_id = self.thread_id(record.id, self.checkpoint_id(run))
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(16, settings.agent_graph_recursion_limit),
        }
        snapshot = agent.get_state(config)
        has_checkpoint = bool(snapshot.values)
        has_pending_interrupt = any(
            bool(getattr(task, "interrupts", None))
            for task in getattr(snapshot, "tasks", ())
        )
        if resume_payload is not None and has_pending_interrupt:
            graph_input: Any = Command(
                resume=_resume_value_for_interrupt(snapshot, resume_payload)
            )
        else:
            graph_input = {
                "messages": _input_messages(
                    record,
                    run,
                    user_prompt,
                    include_history=include_history and not has_checkpoint,
                )
            }

        streamed_file_drafts: dict[int, _StreamedFileDraft] = {}
        for chunk in agent.stream(
            graph_input,
            config,
            stream_mode=["messages", "updates", "custom"],
            durability="sync",
            version="v2",
        ):
            chunk_type = chunk.get("type")
            if chunk_type == "custom":
                data = chunk.get("data")
                if isinstance(data, dict) and data.get("type"):
                    yield data
                continue

            if chunk_type == "messages":
                data = chunk.get("data")
                if not isinstance(data, tuple) or not data:
                    continue
                message = data[0]
                metadata = data[1] if len(data) > 1 and isinstance(data[1], dict) else {}
                if metadata.get("langgraph_node") != "model" or not isinstance(
                    message, (AIMessage, AIMessageChunk)
                ):
                    continue
                content = _message_text(message.content)
                if content:
                    yield {"type": "token", "content": content}
                if isinstance(message, AIMessageChunk):
                    yield from _streamed_file_events(message, streamed_file_drafts)
                continue

            if chunk_type != "updates":
                continue
            updates = chunk.get("data")
            if not isinstance(updates, dict) or "__interrupt__" not in updates:
                continue
            run.status = "waiting_for_user"
            run.summary = "Waiting for user confirmation."
            store.save(record)
            interrupts = updates.get("__interrupt__") or []
            values = [getattr(item, "value", None) for item in interrupts]
            question_ids = [
                str(question_id)
                for value in values
                if isinstance(value, dict)
                for question_id in value.get("question_ids", [])
                if question_id
            ]
            if not question_ids:
                hitl_questions = _ensure_hitl_questions(record, run, values)
                question_ids = [str(item["id"]) for item in hitl_questions]
                for question in hitl_questions:
                    yield {"type": "question", "question": question}
            yield {
                "type": "waiting_for_user",
                "status": "waiting_for_user",
                "question_ids": question_ids,
            }

    def clear_thread(
        self,
        business_id: str,
        session_id: str,
        run_ids: Sequence[str] = (),
    ) -> None:
        execution_ids = {session_id}
        execution_ids.update(str(run_id) for run_id in run_ids if run_id)
        for execution_id in execution_ids:
            thread_id = self.thread_id(business_id, execution_id)
            self.checkpointer.delete_thread(thread_id)
            self.ledger.delete_scope(thread_id)

    @staticmethod
    def checkpoint_id(run: AIRun) -> str:
        return run.resumed_from_run_id or run.id

    @staticmethod
    def thread_id(business_id: str, session_id: str) -> str:
        return f"{business_id}:{session_id}"

    @staticmethod
    def _gateway_capabilities(
        tool_capabilities: list[Capability],
        mcp_capabilities: list[Capability],
        record: BusinessRecord,
        run: AIRun,
    ) -> list[Capability]:
        """Expose compact discovery/invocation gateways instead of every MCP schema."""

        tool_by_name = {item.function_name: item for item in tool_capabilities}
        mcp_by_name = {item.function_name: item for item in mcp_capabilities}

        def discover_optional_capabilities(
            kind: str = "all",
            query: str = "",
            limit: int = 10,
            offset: int = 0,
            include_schema: bool = False,
        ) -> dict[str, Any]:
            return optional_capability_catalog(
                tool_capabilities,
                mcp_capabilities,
                list_skills(),
                kind=kind,
                query=query,
                limit=limit,
                offset=offset,
                include_schema=include_schema,
            )

        discovery_tool = StructuredTool.from_function(
            func=discover_optional_capabilities,
            name="discover_studio_capabilities",
            description=(
                "Search optional Tools, Skills, and MCP capabilities only when the current "
                "task needs them. Results are bounded; set include_schema for a narrow "
                "Tool or MCP search before calling it."
            ),
            args_schema=_CapabilityDiscoveryInput,
            infer_schema=False,
        )

        def invoke_tool(name: str, arguments: dict[str, Any]) -> CapabilityResult:
            capability = tool_by_name.get(name)
            if capability is None:
                raise ToolException(
                    "Unknown optional Tool. Search discover_studio_capabilities first."
                )
            validation_errors = sorted(
                Draft202012Validator(capability.input_schema).iter_errors(arguments),
                key=lambda item: list(item.path),
            )
            if validation_errors:
                message = "; ".join(item.message for item in validation_errors[:4])
                raise ToolException(
                    f"Invalid arguments for {name}: {message}. Discover it with "
                    "include_schema=true and retry with the documented schema."
                )
            return execute_capability(
                capability,
                record,
                arguments,
                run_id=run.id,
                session_id=run.session_id or "",
            )

        tool_gateway = StructuredTool.from_function(
            func=invoke_tool,
            name="call_tool",
            description=(
                "Call one exact optional Tool previously returned by "
                "discover_studio_capabilities. Do not guess names or arguments."
            ),
            args_schema=_ToolGatewayInput,
            infer_schema=False,
        )

        def invoke_mcp(name: str, arguments: dict[str, Any]) -> CapabilityResult:
            capability = mcp_by_name.get(name)
            if capability is None:
                raise ToolException(
                    "Unknown MCP capability. Search discover_studio_capabilities first."
                )
            validation_errors = sorted(
                Draft202012Validator(capability.input_schema).iter_errors(arguments),
                key=lambda item: list(item.path),
            )
            if validation_errors:
                message = "; ".join(item.message for item in validation_errors[:4])
                raise ToolException(
                    f"Invalid arguments for {name}: {message}. Discover it with "
                    "include_schema=true and retry with the documented schema."
                )
            return execute_capability(
                capability,
                record,
                arguments,
                run_id=run.id,
                session_id=run.session_id or "",
            )

        mcp_tool = StructuredTool.from_function(
            func=invoke_mcp,
            name="call_mcp",
            description=(
                "Call one exact MCP capability previously returned by "
                "discover_studio_capabilities. Do not guess names or arguments."
            ),
            args_schema=_MCPGatewayInput,
            infer_schema=False,
        )

        return [
            Capability(
                function_name=discovery_tool.name,
                display_name="Capability discovery",
                kind="tool",
                description=discovery_tool.description,
                input_schema=discovery_tool.get_input_schema().model_json_schema(),
                handler=discovery_tool,
                protocol="capability_discovery",
                source="studio-runtime",
            ),
            Capability(
                function_name=tool_gateway.name,
                display_name="Tool gateway",
                kind="tool",
                description=tool_gateway.description,
                input_schema=tool_gateway.get_input_schema().model_json_schema(),
                handler=tool_gateway,
                protocol="tool_gateway",
                source="studio-runtime",
            ),
            Capability(
                function_name=mcp_tool.name,
                display_name="MCP gateway",
                kind="tool",
                description=mcp_tool.description,
                input_schema=mcp_tool.get_input_schema().model_json_schema(),
                handler=mcp_tool,
                protocol="mcp_gateway",
                source="studio-runtime",
            ),
        ]

    def _tool_for(
        self,
        capability: Capability,
        record: BusinessRecord,
        run: AIRun,
    ) -> StructuredTool:
        def invoke_capability(**arguments: Any) -> str:
            invocation = _CURRENT_INVOCATION.get()
            if invocation is None or invocation.capability.function_name != capability.function_name:
                raise ToolException("Capability invocation context is unavailable.")
            validation_errors = sorted(
                Draft202012Validator(capability.input_schema).iter_errors(arguments),
                key=lambda item: list(item.path),
            )
            if validation_errors:
                message = "; ".join(item.message for item in validation_errors[:4])
                invocation.error = message
                raise ToolException(f"Invalid capability arguments: {message}")
            scope = self.thread_id(record.id, self.checkpoint_id(run))
            try:
                result, replayed = self.ledger.execute_once(
                    scope=scope,
                    call_id=invocation.call_id,
                    capability_name=capability.function_name,
                    arguments=arguments,
                    retry_safe=capability.retry_safe,
                    executor=lambda: execute_capability(
                        capability,
                        record,
                        arguments,
                        run_id=run.id,
                        session_id=run.session_id or "",
                    ),
                )
            except Exception as exc:
                invocation.error = str(exc)
                raise ToolException(str(exc)) from exc
            invocation.result = result
            invocation.replayed = replayed
            return result_for_model(result)

        return StructuredTool.from_function(
            func=invoke_capability,
            name=capability.function_name,
            description=capability.description,
            args_schema=capability.input_schema,
            infer_schema=False,
            handle_tool_error=True,
        )

    def _tool_middleware(
        self,
        record: BusinessRecord,
        run: AIRun,
        capability_map: dict[str, Capability],
        skill_trace: _SkillTrace,
    ) -> Any:
        @wrap_tool_call
        def execute_studio_tool(request: Any, handler: Callable[[Any], ToolMessage]) -> ToolMessage:
            call = request.tool_call
            call_id = str(call.get("id") or f"tool_{run.id}_{time()}")
            name = str(call.get("name") or "")
            arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
            capability = capability_map.get(name)

            if _defer_for_user_input(request.state, call_id, capability_map):
                return ToolMessage(
                    content="Deferred because another tool call requires user input first.",
                    tool_call_id=call_id,
                    name=name,
                )

            if capability is None:
                return self._execute_runtime_tool(
                    request,
                    handler,
                    record,
                    run,
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    skill_trace=skill_trace,
                )

            event_type = (
                "file_operation"
                if capability.protocol == "workspace_file"
                else f"{capability.kind}_call"
            )
            event_details = _file_event_details(capability.function_name, arguments)
            started = perf_counter()

            if capability.protocol == "user_input":
                errors = sorted(
                    Draft202012Validator(capability.input_schema).iter_errors(arguments),
                    key=lambda item: list(item.path),
                )
                if errors:
                    message = "; ".join(item.message for item in errors[:4])
                    raise ToolException(f"Invalid user-input request: {message}")
                question, created = _ensure_question(record, run, call_id, arguments)
                if created:
                    request.runtime.stream_writer(
                        {
                            "type": event_type,
                            "kind": capability.kind,
                            "call_id": call_id,
                            "name": capability.display_name,
                            "function_name": capability.function_name,
                            "status": "running",
                            "input": _safe_arguments(arguments),
                        }
                    )
                    _append_invocation(run, call_id, capability, "succeeded", "等待用户确认")
                    store.save(record)
                    request.runtime.stream_writer(
                        {
                            "type": event_type,
                            "kind": capability.kind,
                            "call_id": call_id,
                            "name": capability.display_name,
                            "function_name": capability.function_name,
                            "status": "succeeded",
                            "output": "等待用户确认",
                            "duration_ms": round((perf_counter() - started) * 1000),
                        }
                    )
                    request.runtime.stream_writer({"type": "question", "question": question})
                answers = interrupt(
                    {
                        "kind": "user_input",
                        "question_ids": [question["id"]],
                        "questions": [question],
                    }
                )
                if not created:
                    request.runtime.stream_writer(
                        {
                            "type": event_type,
                            "kind": capability.kind,
                            "call_id": call_id,
                            "name": capability.display_name,
                            "function_name": capability.function_name,
                            "status": "succeeded",
                            "output": "用户已回答，继续执行",
                            "duration_ms": round((perf_counter() - started) * 1000),
                        }
                    )
                return ToolMessage(
                    content=json.dumps(
                        {"status": "answered", "question_id": question["id"], "answers": answers},
                        ensure_ascii=False,
                    ),
                    tool_call_id=call_id,
                    name=name,
                )

            request.runtime.stream_writer(
                {
                    "type": event_type,
                    "kind": capability.kind,
                    "call_id": call_id,
                    "name": capability.display_name,
                    "function_name": capability.function_name,
                    "status": "running",
                    "input": _safe_arguments(arguments),
                    **event_details,
                }
            )
            invocation = _Invocation(call_id=call_id, capability=capability)
            token = _CURRENT_INVOCATION.set(invocation)
            try:
                with _BUSINESS_LOCKS[record.id]:
                    response = handler(request)
            finally:
                _CURRENT_INVOCATION.reset(token)

            if invocation.result is not None:
                result = invocation.result
                _append_invocation(run, call_id, capability, "succeeded", result.summary)
                store.save(record)
                request.runtime.stream_writer(
                    {
                        "type": event_type,
                        "kind": capability.kind,
                        "call_id": call_id,
                        "name": capability.display_name,
                        "function_name": capability.function_name,
                        "status": "succeeded",
                        "output": result.summary,
                        **event_details,
                        "replayed": invocation.replayed,
                        "duration_ms": round((perf_counter() - started) * 1000),
                    }
                )
                for emitted in result.emitted_events:
                    request.runtime.stream_writer(emitted)
            else:
                error = invocation.error or _message_text(response.content) or "Capability failed."
                _append_invocation(run, call_id, capability, "failed", error)
                store.save(record)
                request.runtime.stream_writer(
                    {
                        "type": event_type,
                        "kind": capability.kind,
                        "call_id": call_id,
                        "name": capability.display_name,
                        "function_name": capability.function_name,
                        "status": "failed",
                        "error": error,
                        **event_details,
                        "duration_ms": round((perf_counter() - started) * 1000),
                    }
                )
            return response

        return execute_studio_tool

    def _execute_runtime_tool(
        self,
        request: Any,
        handler: Callable[[Any], ToolMessage],
        record: BusinessRecord,
        run: AIRun,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        skill_trace: _SkillTrace,
    ) -> ToolMessage:
        """Observe DeepAgents runtime Tools without folding them into Studio Tool discovery."""

        skill = _skill_resource_for_call(request.state, name, arguments)
        is_activation = skill is not None and skill[1].casefold() == "skill.md"
        skill_name = skill[0] if skill is not None else _skill_for_sandbox_command(
            name,
            arguments,
            skill_trace.active_names,
        )
        if is_activation:
            parent_skill_id = skill_trace.activate(skill_name or "")
            event_type = "skill_activation"
            kind = "skill"
            display_name = skill_name or "Skill"
        elif skill is not None:
            parent_skill_id = skill_trace.parent_for(skill_name)
            event_type = "skill_resource"
            kind = "skill_resource"
            display_name = skill_name or "Skill resource"
        elif name == "execute":
            parent_skill_id = skill_trace.parent_for(skill_name)
            event_type = "sandbox_command"
            kind = "sandbox"
            display_name = "Sandbox"
        elif name in _FILE_TOOL_OPERATIONS:
            parent_skill_id = None
            event_type = "file_operation"
            kind = "workspace_file"
            display_name = name
        else:
            parent_skill_id = None
            event_type = "tool_call"
            kind = "runtime_tool"
            display_name = name
        started = perf_counter()
        workspace_root = store.workspace_dir(record.id)
        workspace_before = _workspace_file_state(workspace_root) if name == "execute" else None
        base_event = {
            "type": event_type,
            "kind": kind,
            "call_id": call_id,
            "name": display_name,
            "function_name": name,
            "status": "running",
            "input": _safe_arguments(arguments),
            **_file_event_details(name, arguments),
            **({"resource": skill[1]} if skill is not None else {}),
            **({"skill_name": skill_name} if skill_name else {}),
            **({"parent_skill_id": parent_skill_id} if parent_skill_id else {}),
            **({"skill_id": parent_skill_id} if is_activation else {}),
        }
        request.runtime.stream_writer(base_event)
        try:
            with _BUSINESS_LOCKS[record.id]:
                response = handler(request)
        except Exception as exc:
            summary = str(exc) or f"{display_name} failed"
            _append_runtime_invocation(
                run,
                call_id,
                display_name,
                kind,
                "failed",
                summary,
                parent_skill_id=parent_skill_id,
                skill_name=skill_name,
            )
            store.save(record)
            request.runtime.stream_writer(
                base_event
                | {
                    "status": "failed",
                    "error": summary,
                    "duration_ms": round((perf_counter() - started) * 1000),
                }
            )
            _emit_workspace_changes(
                request.runtime.stream_writer,
                call_id,
                workspace_root,
                workspace_before,
            )
            raise

        failed = _runtime_tool_failed(name, response)
        status = "failed" if failed else "succeeded"
        summary = _runtime_tool_summary(name, skill, failed)
        _append_runtime_invocation(
            run,
            call_id,
            display_name,
            kind,
            status,
            summary,
            parent_skill_id=parent_skill_id,
            skill_name=skill_name,
        )
        if status == "succeeded" and skill is not None:
            _record_runtime_usage(record, name, skill, summary)
        store.save(record)
        event = base_event | {
            "status": status,
            "duration_ms": round((perf_counter() - started) * 1000),
        }
        if failed:
            event["error"] = _message_text(response.content)[:2000] or summary
        else:
            event["output"] = summary
        request.runtime.stream_writer(event)
        _emit_workspace_changes(
            request.runtime.stream_writer,
            call_id,
            workspace_root,
            workspace_before,
        )
        return response


def _live_model_turn(
    record: BusinessRecord,
    messages: list[dict[str, Any]],
    requested_model: str | None,
    tools: list[dict[str, Any]] | None,
) -> Iterator[Any]:
    # Resolve at call time so tests and provider plugins can replace the gateway.
    from app.studio.runtime import agent

    return agent.stream_model_turn(record, messages, requested_model, tools)


def _input_messages(
    record: BusinessRecord,
    run: AIRun,
    user_prompt: str | None,
    *,
    include_history: bool,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if include_history and run.session_id:
        history = [
            item
            for item in record.messages
            if item.session_id == run.session_id and item.role in {"user", "assistant", "system"}
        ][-max(1, settings.agent_history_message_limit):]
        if history and user_prompt and history[-1].role == "user" and history[-1].content == user_prompt:
            history = history[:-1]
        messages.extend(_bounded_history_messages(history, settings.agent_history_character_limit))
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    return messages


def _bounded_history_messages(history: Sequence[Any], character_limit: int) -> list[dict[str, str]]:
    remaining = max(1_000, int(character_limit))
    selected: list[dict[str, str]] = []
    for item in reversed(history):
        if remaining <= 0:
            break
        content = str(item.content or "")
        content = _bounded_message_content(content, min(12_000, remaining))
        if not content:
            continue
        selected.append({"role": str(item.role), "content": content})
        remaining -= len(content)
    selected.reverse()
    return selected


def _bounded_message_content(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    marker = "\n\n[Earlier message content omitted by context budget]\n\n"
    available = max(0, limit - len(marker))
    head = available // 3
    tail = available - head
    return content[:head] + marker + content[-tail:]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _skill_resource_for_call(
    state: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, str] | None:
    """Classify a DeepAgents read_file call using SkillsMiddleware state metadata."""

    if tool_name != "read_file" or not isinstance(state, dict):
        return None
    raw_path = str(arguments.get("file_path") or "").strip()
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path).resolve()
    except OSError:
        return None
    for raw in state.get("skills_metadata") or []:
        if not isinstance(raw, dict):
            continue
        skill_path = str(raw.get("path") or "").strip()
        skill_name = str(raw.get("name") or "").strip()
        if not skill_path or not skill_name:
            continue
        try:
            instruction_path = Path(skill_path).resolve()
            relative = candidate.relative_to(instruction_path.parent)
        except (OSError, ValueError):
            continue
        return skill_name, relative.as_posix()
    return None


def _runtime_tool_summary(
    tool_name: str,
    skill: tuple[str, str] | None,
    failed: bool,
) -> str:
    if skill is not None:
        action = "Failed to read" if failed else "Read"
        return f"{action} {skill[0]}/{skill[1]}"
    if tool_name == "execute":
        return "Sandbox command failed" if failed else "Sandbox command completed"
    action = "failed" if failed else "completed"
    return f"{tool_name} {action}"


def _file_event_details(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    operation = _FILE_TOOL_OPERATIONS.get(tool_name)
    if tool_name == "manage_workspace_entry":
        operation = str(arguments.get("action") or "manage")
    if not operation:
        return {}
    path = str(
        arguments.get("file_path")
        or arguments.get("path")
        or arguments.get("source")
        or "/workspace"
    )
    return {
        "operation": operation,
        "path": path,
        "destination": str(arguments.get("destination") or ""),
        "mutating": operation in {"create", "edit", "delete", "move", "create_directory"},
    }


def _streamed_file_events(
    message: AIMessageChunk,
    drafts: dict[int, _StreamedFileDraft],
) -> Iterator[dict[str, Any]]:
    for fallback_index, raw in enumerate(message.tool_call_chunks or []):
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw.get("index", fallback_index))
        except (TypeError, ValueError):
            index = fallback_index
        draft = drafts.setdefault(index, _StreamedFileDraft(index=index))
        if raw.get("id"):
            draft.call_id = _merge_stream_identifier(draft.call_id, str(raw["id"]))
        if raw.get("name"):
            draft.name = _merge_stream_identifier(draft.name, str(raw["name"]))
        if raw.get("args"):
            draft.arguments += str(raw["args"])
        if draft.name not in {"write_file", "edit_file"}:
            continue

        path, path_complete = _partial_json_string_field(draft.arguments, "file_path")
        if not path_complete or not path.startswith("/workspace/"):
            continue
        operation = _FILE_TOOL_OPERATIONS[draft.name]
        field = "content" if draft.name == "write_file" else "new_string"
        content, _content_complete = _partial_json_string_field(draft.arguments, field)
        reset = not content.startswith(draft.emitted_content)
        delta = content if reset else content[len(draft.emitted_content) :]
        old_text = ""
        if draft.name == "edit_file":
            old_text, _ = _partial_json_string_field(draft.arguments, "old_string")
        if not draft.announced or delta or reset:
            yield {
                "type": "file_operation",
                "kind": "workspace_file",
                "call_id": draft.call_id or f"streamed_file_{index}",
                "name": draft.name,
                "function_name": draft.name,
                "status": "streaming",
                "operation": operation,
                "path": path,
                "destination": "",
                "mutating": True,
                "content_delta": delta,
                "content_reset": reset,
                "content_length": len(content),
                **({"old_text": old_text} if old_text else {}),
            }
            draft.announced = True
        draft.emitted_content = content


def _merge_stream_identifier(current: str, fragment: str) -> str:
    if not fragment or fragment == current or current.endswith(fragment):
        return current
    if fragment.startswith(current):
        return fragment
    return current + fragment


def _partial_json_string_field(raw: str, field: str) -> tuple[str, bool]:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"', raw)
    if match is None:
        return "", False
    index = match.end()
    output: list[str] = []
    while index < len(raw):
        char = raw[index]
        if char == '"':
            return "".join(output), True
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(raw):
            break
        escape = raw[index + 1]
        replacements = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escape in replacements:
            output.append(replacements[escape])
            index += 2
            continue
        if escape != "u" or index + 6 > len(raw):
            break
        code = raw[index + 2 : index + 6]
        if not re.fullmatch(r"[0-9a-fA-F]{4}", code):
            break
        value = int(code, 16)
        if 0xD800 <= value <= 0xDBFF:
            if index + 12 > len(raw) or raw[index + 6 : index + 8] != "\\u":
                break
            low_code = raw[index + 8 : index + 12]
            if not re.fullmatch(r"[0-9a-fA-F]{4}", low_code):
                break
            low = int(low_code, 16)
            if not 0xDC00 <= low <= 0xDFFF:
                break
            output.append(chr(0x10000 + ((value - 0xD800) << 10) + low - 0xDC00))
            index += 12
            continue
        output.append(chr(value))
        index += 6
    return "".join(output), False


def _workspace_file_state(root: Path) -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return state
    for path in root.rglob("*"):
        if len(state) >= _WORKSPACE_SNAPSHOT_LIMIT:
            break
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            metadata = path.stat()
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        state[relative] = (metadata.st_mtime_ns, metadata.st_size)
    return state


def _emit_workspace_changes(
    writer: Callable[[dict[str, Any]], None],
    parent_call_id: str,
    workspace_root: Path,
    before: dict[str, tuple[int, int]] | None,
) -> None:
    if before is None:
        return
    after = _workspace_file_state(workspace_root)
    changes: list[tuple[str, str]] = []
    for path in sorted(after.keys() - before.keys()):
        changes.append(("create", path))
    for path in sorted(before.keys() & after.keys()):
        if before[path] != after[path]:
            changes.append(("edit", path))
    for path in sorted(before.keys() - after.keys()):
        changes.append(("delete", path))
    for index, (operation, path) in enumerate(changes[:50], start=1):
        writer(
            {
                "type": "file_operation",
                "kind": "workspace_file",
                "call_id": f"{parent_call_id}:workspace:{index}",
                "parent_call_id": parent_call_id,
                "name": "Sandbox",
                "function_name": "execute",
                "status": "succeeded",
                "operation": operation,
                "path": f"/workspace/{path}",
                "destination": "",
                "mutating": True,
                "source": "sandbox",
                "auto_open": False,
            }
        )


def _runtime_tool_failed(tool_name: str, response: ToolMessage) -> bool:
    if str(getattr(response, "status", "") or "").casefold() == "error":
        return True
    if tool_name != "execute":
        return False
    content = _message_text(response.content)
    return re.search(r"\[Command failed with exit code -?\d+\]", content) is not None


def _skill_for_sandbox_command(
    tool_name: str,
    arguments: dict[str, Any],
    active_skill_names: tuple[str, ...],
) -> str | None:
    """Associate sandbox work only when the command names an activated package path."""

    if tool_name != "execute":
        return None
    command = str(arguments.get("command") or "").replace("\\", "/")
    for skill_name in active_skill_names:
        package_path = re.escape(f"/skills/{skill_name}")
        if re.search(rf"(?<![A-Za-z0-9_.-]){package_path}(?:/|(?=[\s;&|()'\"`]|$))", command):
            return skill_name
    return None


def _append_runtime_invocation(
    run: AIRun,
    call_id: str,
    name: str,
    kind: str,
    status: str,
    summary: str,
    *,
    parent_skill_id: str | None = None,
    skill_name: str | None = None,
) -> None:
    existing = next(
        (item for item in run.tool_invocations if item.get("call_id") == call_id),
        None,
    )
    payload: dict[str, Any] = {
        "call_id": call_id,
        "name": name,
        "kind": kind,
        "status": status,
        **({"parent_skill_id": parent_skill_id} if parent_skill_id else {}),
        **({"skill_name": skill_name} if skill_name else {}),
    }
    payload["summary" if status == "succeeded" else "error"] = summary
    if existing is None:
        run.tool_invocations.append(payload)
    else:
        existing.update(payload)


def _record_runtime_usage(
    record: BusinessRecord,
    tool_name: str,
    skill: tuple[str, str] | None,
    summary: str,
) -> None:
    payload = {
        "id": new_id("call"),
        "status": "succeeded",
        "summary": summary,
        "created_at": time(),
    }
    if skill is None:
        record.context.tool_usages.append(
            payload | {"name": tool_name, "kind": "tool", "tool": tool_name}
        )
        return
    existing = next(
        (item for item in record.context.skill_references if item.get("name") == skill[0]),
        None,
    )
    skill_payload = payload | {
        "name": skill[0],
        "kind": "skill",
        "resource": skill[1],
        "reason": "Read through DeepAgents SkillsMiddleware.",
        "locked": True,
    }
    if existing is None:
        record.context.skill_references.append(skill_payload)
    else:
        existing.update(skill_payload)


def _pending_interrupt_values(snapshot: Any) -> list[Any]:
    return [
        getattr(item, "value", None)
        for task in getattr(snapshot, "tasks", ())
        for item in getattr(task, "interrupts", ()) or ()
    ]


def _hitl_action_requests(values: list[Any]) -> list[dict[str, Any]]:
    return [
        dict(action)
        for value in values
        if isinstance(value, dict)
        for action in value.get("action_requests") or []
        if isinstance(action, dict)
    ]


def _ensure_hitl_questions(
    record: BusinessRecord,
    run: AIRun,
    values: list[Any],
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for index, action in enumerate(_hitl_action_requests(values)):
        question_id = f"q_hitl_{run.id}_{index + 1}"
        existing = next(
            (item for item in record.context.questions if item.get("id") == question_id),
            None,
        )
        if existing is not None:
            questions.append(existing)
            continue
        name = str(action.get("name") or "execute")
        arguments = action.get("args") if isinstance(action.get("args"), dict) else {}
        command = str(arguments.get("command") or "").strip()
        description = str(action.get("description") or "").strip()
        detail = command[:1200] or json.dumps(_safe_arguments(arguments), ensure_ascii=False)[:1200]
        question = {
            "id": question_id,
            "question": f"是否允许 AI 执行本次 {name} 操作？",
            "reason": description[:2000] or detail,
            "category": "命令确认",
            "options": [
                {
                    "id": f"{question_id}_approve",
                    "label": "允许本次执行",
                    "description": detail,
                    "recommended": False,
                },
                {
                    "id": f"{question_id}_reject",
                    "label": "拒绝执行",
                    "description": "不运行该命令，并让 AI 根据拒绝结果继续处理。",
                    "recommended": False,
                },
            ],
            "status": "open",
            "run_id": run.id,
            "session_id": run.session_id,
            "created_at": time(),
            "source": "deepagents_hitl",
            "hitl_index": index,
            "action_name": name,
        }
        record.context.questions.append(question)
        questions.append(question)
    if questions:
        store.create_version(record, "AI 请求命令执行确认", "agent_tool_approval", model="agent")
        store.save(record)
    return questions


def _resume_value_for_interrupt(snapshot: Any, resume_payload: dict[str, Any]) -> dict[str, Any]:
    actions = _hitl_action_requests(_pending_interrupt_values(snapshot))
    if not actions:
        return resume_payload
    answers = [item for item in resume_payload.get("answers") or [] if isinstance(item, dict)]
    by_index = {
        int(item["hitl_index"]): str(item.get("answer") or "")
        for item in answers
        if isinstance(item.get("hitl_index"), int)
    }
    decisions: list[dict[str, Any]] = []
    for index, _action in enumerate(actions):
        answer = by_index.get(index, "")
        lowered = answer.strip().casefold()
        rejected = any(
            token in lowered
            for token in ("拒绝", "不允许", "reject", "deny", "do not run", "don't run")
        )
        approved = not rejected and any(
            token in lowered
            for token in ("允许本次执行", "允许执行", "approve", "approved", "run once")
        )
        if not approved:
            decisions.append({"type": "reject", "message": answer or "用户拒绝执行该命令。"})
        else:
            decisions.append({"type": "approve"})
    return {"decisions": decisions}


def _defer_for_user_input(
    state: Any,
    call_id: str,
    capabilities: dict[str, Capability],
) -> bool:
    messages = state.get("messages") if isinstance(state, dict) else None
    latest = messages[-1] if isinstance(messages, list) and messages else None
    calls = getattr(latest, "tool_calls", []) or []
    question_calls: list[dict[str, Any]] = []
    for item in calls:
        capability = capabilities.get(str(item.get("name") or ""))
        if capability is not None and capability.protocol == "user_input":
            question_calls.append(item)
    if not question_calls:
        return False
    first_question_id = str(question_calls[0].get("id") or "")
    return call_id != first_question_id


def _ensure_question(
    record: BusinessRecord,
    run: AIRun,
    call_id: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    existing = next(
        (item for item in record.context.questions if item.get("tool_call_id") == call_id),
        None,
    )
    if existing is not None:
        return existing, False
    safe_call_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", call_id).strip("_")
    question_id = f"q_{safe_call_id}"[:120]
    options: list[dict[str, Any]] = []
    recommended_seen = False
    for index, raw in enumerate(arguments.get("options") or []):
        if not isinstance(raw, dict) or not str(raw.get("label") or "").strip():
            continue
        recommended = bool(raw.get("recommended")) and not recommended_seen
        recommended_seen = recommended_seen or recommended
        options.append(
            {
                "id": f"{question_id}_option_{index + 1}",
                "label": str(raw.get("label") or "").strip()[:200],
                "description": str(raw.get("description") or "").strip()[:1000],
                "recommended": recommended,
            }
        )
    question = {
        "id": question_id,
        "tool_call_id": call_id,
        "question": str(arguments.get("question") or "").strip(),
        "reason": str(arguments.get("reason") or "").strip(),
        "category": str(arguments.get("category") or "agent_clarification"),
        "options": options[:5],
        "status": "open",
        "run_id": run.id,
        "session_id": run.session_id,
        "created_at": time(),
        "source": "agent",
    }
    if not question["question"]:
        raise ToolException("question cannot be empty")
    record.context.questions.append(question)
    store.create_version(record, "AI 提出待确认问题", "agent_question", model="agent")
    run.status = "waiting_for_user"
    run.summary = "Waiting for user confirmation."
    return question, True


def _append_invocation(
    run: AIRun,
    call_id: str,
    capability: Capability,
    status: str,
    summary: str,
) -> None:
    existing = next(
        (item for item in run.tool_invocations if item.get("call_id") == call_id),
        None,
    )
    payload = {
        "call_id": call_id,
        "name": capability.display_name,
        "kind": capability.kind,
        "status": status,
    }
    if status == "succeeded":
        payload["summary"] = summary
    else:
        payload["error"] = summary
    if existing is None:
        run.tool_invocations.append(payload)
    else:
        existing.update(payload)
