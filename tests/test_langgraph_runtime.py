from __future__ import annotations

import hashlib
import json
import sqlite3
import pytest
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.studio.capability_runtime import Capability, CapabilityResult
from app.studio.execution_ledger import (
    ToolExecutionCollision,
    ToolExecutionInProgress,
    ToolExecutionLedger,
    ToolExecutionUncertain,
)
from app.studio.llm import ModelStreamEvent, ModelToolCall
from app.studio.graph_runtime import (
    StudioGraphRuntime,
    _StudioSummarizationMiddleware,
    _runtime_tool_failed,
    _skill_for_sandbox_command,
)
from app.studio.model_adapter import StudioChatModel
from app.studio.models import AIRun, BusinessContext, BusinessRecord, ChatMessage


class _FakeSandboxBackend(BaseSandbox):
    def __init__(self) -> None:
        self.commands: list[str] = []

    @property
    def id(self) -> str:
        return "graph-runtime"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        del timeout
        self.commands.append(command)
        return ExecuteResponse(
            output="sandbox command output\n",
            exit_code=0,
            truncated=False,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=None) for path, _content in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=path, content=b"", error=None) for path in paths]


def test_sandbox_failure_and_skill_path_attribution_use_real_runtime_data() -> None:
    failed = ToolMessage(
        content="boom\n[Command failed with exit code 7]",
        tool_call_id="call-failed",
        name="execute",
        status="success",
    )

    assert _runtime_tool_failed("execute", failed) is True
    assert _skill_for_sandbox_command(
        "execute",
        {"command": "cd /skills/ocr-parser && python scripts/parse.py"},
        ("ocr-parser",),
    ) == "ocr-parser"


def _result(value: str = "done") -> CapabilityResult:
    return CapabilityResult(
        output={"value": value},
        summary=f"result: {value}",
        emitted_events=[{"type": "progress", "value": value}],
    )


def _arguments_hash(arguments: dict) -> str:
    normalized = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _business_record() -> BusinessRecord:
    return BusinessRecord(
        id="business-test",
        name="Runtime contract",
        created_at=1,
        updated_at=1,
        context=BusinessContext(business_id="business-test", name="Runtime contract"),
    )


def _run(*, run_id: str = "run-test", session_id: str = "session-test") -> AIRun:
    return AIRun(
        id=run_id,
        business_id="business-test",
        session_id=session_id,
        model="test-provider-model",
        started_at=1,
    )


def _configure_runtime_dependencies(
    monkeypatch,
    capabilities,
    executor,
    *,
    sandbox: BaseSandbox | None = None,
) -> BaseSandbox:
    def compatible_executor(capability, record, arguments, **_runtime_context):
        return executor(capability, record, arguments)

    selected_sandbox = sandbox or _FakeSandboxBackend()

    monkeypatch.setattr(
        "app.studio.graph_runtime.discover_capabilities",
        lambda record: capabilities,
    )
    monkeypatch.setattr("app.studio.graph_runtime.execute_capability", compatible_executor)
    monkeypatch.setattr(
        "app.studio.graph_runtime._system_prompt",
        lambda record, available: "Use only the registered capabilities.",
    )
    monkeypatch.setattr("app.studio.graph_runtime.store.save", lambda record: None)
    monkeypatch.setattr(
        "app.studio.graph_runtime.store.create_version",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.studio.graph_runtime.sandbox_manager.backend_for",
        lambda **_kwargs: selected_sandbox,
    )
    return selected_sandbox


def _model_tool_names(tools: list[dict] | None) -> set[str]:
    return {
        str(item.get("function", {}).get("name") or "")
        for item in tools or []
        if isinstance(item, dict)
    }


def test_deepagents_skills_are_progressively_disclosed_without_skill_tools(tmp_path, monkeypatch):
    skill_root = tmp_path / "skills"
    skill = skill_root / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Use this demo Skill for progressive disclosure tests.\n"
        "---\n\n"
        "BODY_MUST_NOT_BE_PRELOADED\n",
        encoding="utf-8",
    )
    observed_system: list[str] = []
    observed_tools: list[set[str]] = []

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model
        observed_system.append(
            "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        )
        observed_tools.append(_model_tool_names(tools))
        yield ModelStreamEvent(kind="content", content="ready")
        yield ModelStreamEvent(kind="completed")

    monkeypatch.setattr("app.studio.graph_runtime.SYSTEM_SKILLS_ROOT", skill_root)
    _configure_runtime_dependencies(monkeypatch, [], lambda capability, record, arguments: _result())
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "skill-disclosure-ledger.sqlite3"),
        model_turn=model_turn,
    )

    list(
        runtime.stream(
            _business_record(),
            _run(run_id="run-skill-disclosure"),
            requested_model=None,
            user_prompt="Use the demo skill",
            include_history=False,
        )
    )

    assert "demo-skill" in observed_system[0]
    assert "BODY_MUST_NOT_BE_PRELOADED" not in observed_system[0]
    assert "read_file" in observed_tools[0]
    assert "execute" in observed_tools[0]
    assert "run_skill_script" not in observed_tools[0]
    assert not any(name.startswith("skill__") for name in observed_tools[0])


def test_reading_skill_instructions_emits_a_real_skill_activation_event(tmp_path, monkeypatch):
    skill_root = tmp_path / "skills"
    skill = skill_root / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Load this Skill on demand.\n---\n\n"
        "FOLLOW_THE_DEMO_WORKFLOW\n",
        encoding="utf-8",
    )
    observed_tool_messages: list[dict] = []

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-load-skill",
                        name="read_file",
                        arguments={"file_path": "/skills/demo-skill/SKILL.md", "limit": 1000},
                    )
                ],
            )
            return
        observed_tool_messages.extend(tool_messages)
        yield ModelStreamEvent(kind="content", content="Skill instructions loaded.")
        yield ModelStreamEvent(kind="completed")

    monkeypatch.setattr("app.studio.graph_runtime.SYSTEM_SKILLS_ROOT", skill_root)
    _configure_runtime_dependencies(monkeypatch, [], lambda capability, record, arguments: _result())
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "skill-load-ledger.sqlite3"),
        model_turn=model_turn,
    )
    record = _business_record()
    run = _run(run_id="run-skill-load")

    events = list(
        runtime.stream(
            record,
            run,
            requested_model=None,
            user_prompt="Load demo-skill",
            include_history=False,
        )
    )

    assert "FOLLOW_THE_DEMO_WORKFLOW" in observed_tool_messages[-1]["content"]
    skill_events = [item for item in events if item.get("call_id") == "call-load-skill"]
    assert [item["type"] for item in skill_events] == ["skill_activation", "skill_activation"]
    assert [item["status"] for item in skill_events] == ["running", "succeeded"]
    assert skill_events[-1]["resource"] == "SKILL.md"
    assert skill_events[-1]["skill_name"] == "demo-skill"
    assert skill_events[-1]["skill_id"].startswith("skill_run-skill-load_demo-skill")
    assert run.tool_invocations[-1]["kind"] == "skill"
    assert record.context.skill_references[-1]["name"] == "demo-skill"


def test_activated_skill_executes_in_sandbox_without_hitl_or_script_tool(
    tmp_path,
    monkeypatch,
):
    skill_root = tmp_path / "skills"
    skill = skill_root / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Execute this package in the sandbox.\n---\n\n"
        "Run `python /skills/demo-skill/scripts/run.py`.\n",
        encoding="utf-8",
    )
    (skill / "scripts").mkdir()
    (skill / "scripts" / "run.py").write_text("print('package result')\n", encoding="utf-8")
    step = 0
    observed_tool_names: list[set[str]] = []

    def model_turn(record, messages, requested_model, tools):
        nonlocal step
        del record, requested_model, messages
        observed_tool_names.append(_model_tool_names(tools))
        step += 1
        if step == 1:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-activate-skill",
                        name="read_file",
                        arguments={"file_path": "/skills/demo-skill/SKILL.md", "limit": 1000},
                    )
                ],
            )
            return
        if step == 2:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-execute-skill",
                        name="execute",
                        arguments={
                            "command": "python /skills/demo-skill/scripts/run.py",
                            "timeout": 30,
                        },
                    )
                ],
            )
            return
        yield ModelStreamEvent(kind="content", content="Package completed.")
        yield ModelStreamEvent(kind="completed")

    sandbox = _FakeSandboxBackend()
    monkeypatch.setattr("app.studio.graph_runtime.SYSTEM_SKILLS_ROOT", skill_root)
    _configure_runtime_dependencies(
        monkeypatch,
        [],
        lambda capability, record, arguments: _result(),
        sandbox=sandbox,
    )
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "skill-execute-ledger.sqlite3"),
        model_turn=model_turn,
    )
    record = _business_record()
    run = _run(run_id="run-skill-sandbox")

    events = list(
        runtime.stream(
            record,
            run,
            requested_model=None,
            user_prompt="Use the complete demo Skill package",
            include_history=False,
        )
    )

    assert observed_tool_names
    assert all("execute" in names for names in observed_tool_names)
    assert all("run_skill_script" not in names for names in observed_tool_names)
    assert all(not any(name.startswith("skill__") for name in names) for names in observed_tool_names)
    activation = next(
        item
        for item in events
        if item.get("type") == "skill_activation" and item.get("status") == "succeeded"
    )
    sandbox_events = [item for item in events if item.get("call_id") == "call-execute-skill"]
    assert [item["type"] for item in sandbox_events] == ["sandbox_command", "sandbox_command"]
    assert [item["status"] for item in sandbox_events] == ["running", "succeeded"]
    assert all(item["skill_name"] == "demo-skill" for item in sandbox_events)
    assert all(item["parent_skill_id"] == activation["skill_id"] for item in sandbox_events)
    assert not any(item.get("type") in {"question", "waiting_for_user"} for item in events)
    assert sandbox.commands == ["python /skills/demo-skill/scripts/run.py"]
    sandbox_invocation = next(item for item in run.tool_invocations if item["kind"] == "sandbox")
    assert sandbox_invocation["skill_name"] == "demo-skill"
    assert sandbox_invocation["parent_skill_id"] == activation["skill_id"]


def test_skill_catalog_reloads_for_an_existing_checkpoint_thread(tmp_path, monkeypatch):
    skill_root = tmp_path / "skills"
    first = skill_root / "first-skill"
    first.mkdir(parents=True)
    (first / "SKILL.md").write_text(
        "---\nname: first-skill\ndescription: First Skill.\n---\n",
        encoding="utf-8",
    )
    observed_system: list[str] = []

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model, tools
        observed_system.append(
            "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        )
        yield ModelStreamEvent(kind="content", content="done")
        yield ModelStreamEvent(kind="completed")

    monkeypatch.setattr("app.studio.graph_runtime.SYSTEM_SKILLS_ROOT", skill_root)
    _configure_runtime_dependencies(monkeypatch, [], lambda capability, record, arguments: _result())
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "skill-refresh-ledger.sqlite3"),
        model_turn=model_turn,
    )
    record = _business_record()
    list(
        runtime.stream(
            record,
            _run(run_id="run-skill-refresh-1"),
            requested_model=None,
            user_prompt="first turn",
            include_history=False,
        )
    )
    second = skill_root / "second-skill"
    second.mkdir()
    (second / "SKILL.md").write_text(
        "---\nname: second-skill\ndescription: Installed between turns.\n---\n",
        encoding="utf-8",
    )
    list(
        runtime.stream(
            record,
            _run(run_id="run-skill-refresh-2"),
            requested_model=None,
            user_prompt="second turn",
            include_history=False,
        )
    )

    assert "second-skill" not in observed_system[0]
    assert "second-skill" in observed_system[1]


def test_graph_runtime_maps_a_complete_tool_loop_to_stable_studio_events(
    tmp_path,
    monkeypatch,
):
    capability = Capability(
        function_name="read_business_context",
        display_name="read_business_context",
        kind="tool",
        description="Read the current Business Context.",
        input_schema={
            "type": "object",
            "properties": {"include_evidence": {"type": "boolean"}},
            "required": ["include_evidence"],
            "additionalProperties": False,
        },
        retry_safe=True,
    )
    executions: list[dict] = []
    observed_tool_messages: list[dict] = []

    def execute(capability_arg, record, arguments):
        assert capability_arg is capability
        executions.append(arguments)
        return CapabilityResult(
            output={"name": record.context.name},
            summary="Context loaded",
            emitted_events=[{"type": "context_read", "title": "Business Context"}],
        )

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            yield ModelStreamEvent(kind="reasoning", content="choose the context tool")
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-context-1",
                        name="read_business_context",
                        arguments={"include_evidence": True},
                    )
                ],
            )
            return
        observed_tool_messages.extend(tool_messages)
        yield ModelStreamEvent(kind="reasoning", content="ground the answer")
        yield ModelStreamEvent(kind="content", content="The context is loaded.")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(monkeypatch, [capability], execute)
    run = _run()
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )

    events = list(
        runtime.stream(
            _business_record(),
            run,
            requested_model="test-provider-model",
            user_prompt="Inspect the context",
            include_history=False,
        )
    )

    assert executions == [{"include_evidence": True}]
    assert json.loads(observed_tool_messages[-1]["content"]) == {
        "name": "Runtime contract"
    }
    tool_events = [item for item in events if item.get("call_id") == "call-context-1"]
    assert [item["status"] for item in tool_events] == ["running", "succeeded"]
    assert tool_events[-1]["output"] == "Context loaded"
    assert tool_events[-1]["replayed"] is False
    assert {item["type"] for item in events} >= {"tool_call", "context_read", "token"}
    assert not {"model_call", "reasoning"} & {item["type"] for item in events}
    assert "".join(
        str(item.get("content") or "") for item in events if item["type"] == "token"
    ) == "The context is loaded."
    assert run.tool_invocations == [
        {
            "call_id": "call-context-1",
            "name": "read_business_context",
            "kind": "tool",
            "status": "succeeded",
            "summary": "Context loaded",
        }
    ]


def test_graph_runtime_isolates_each_agent_run_from_previous_tool_context(
    tmp_path,
    monkeypatch,
):
    observed_messages: list[list[str]] = []

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model, tools
        observed_messages.append(
            [
                str(item.get("content") or "")
                for item in messages
                if item.get("role") != "system"
            ]
        )
        yield ModelStreamEvent(kind="content", content="turn complete")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(
        monkeypatch,
        [],
        lambda capability, record, arguments: _result(),
    )
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )
    record = _business_record()

    list(
        runtime.stream(
            record,
            _run(run_id="run-turn-1"),
            requested_model=None,
            user_prompt="first request",
            include_history=False,
        )
    )
    list(
        runtime.stream(
            record,
            _run(run_id="run-turn-2"),
            requested_model=None,
            user_prompt="second request",
            include_history=False,
        )
    )

    assert observed_messages == [
        ["first request"],
        ["second request"],
    ]


def test_graph_runtime_isolates_runs_with_the_production_sqlite_checkpointer(
    tmp_path,
    monkeypatch,
):
    observed_messages: list[list[str]] = []

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model, tools
        observed_messages.append(
            [str(item.get("content") or "") for item in messages if item.get("role") != "system"]
        )
        yield ModelStreamEvent(kind="content", content="turn complete")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(
        monkeypatch,
        [],
        lambda capability, record, arguments: _result(),
    )
    connection = sqlite3.connect(
        tmp_path / "checkpoints.sqlite3",
        check_same_thread=False,
    )
    checkpointer = SqliteSaver(connection)
    checkpointer.setup()
    runtime = StudioGraphRuntime(
        checkpointer=checkpointer,
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )
    record = _business_record()

    try:
        for index, prompt in enumerate(("first request", "second request"), start=1):
            list(
                runtime.stream(
                    record,
                    _run(run_id=f"run-sqlite-{index}"),
                    requested_model=None,
                    user_prompt=prompt,
                    include_history=False,
                )
            )
    finally:
        connection.close()

    assert observed_messages == [
        ["first request"],
        ["second request"],
    ]


def test_graph_runtime_bounds_user_visible_chat_history_without_tool_transcripts(
    tmp_path,
    monkeypatch,
):
    observed: list[dict] = []

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model, tools
        observed.extend(item for item in messages if item.get("role") != "system")
        yield ModelStreamEvent(kind="content", content="bounded")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(monkeypatch, [], lambda capability, record, arguments: _result())
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )
    record = _business_record()
    record.messages = [
        ChatMessage(
            id=f"message-{index}",
            session_id="session-test",
            role="assistant" if index % 2 else "user",
            content=f"history-{index}:" + (str(index % 10) * 5_000),
            created_at=float(index),
        )
        for index in range(20)
    ]

    list(
        runtime.stream(
            record,
            _run(run_id="run-bounded-history"),
            requested_model=None,
            user_prompt="current objective",
            include_history=True,
        )
    )

    history_content = "".join(str(item.get("content") or "") for item in observed[:-1])
    assert len(history_content) <= 32_000
    assert "Earlier message content omitted by context budget" in history_content
    assert observed[-1]["role"] == "user"
    assert observed[-1]["content"] == "current objective"


def test_context_summarization_emits_one_semantic_compaction_event():
    events: list[dict] = []

    class Runtime:
        @staticmethod
        def stream_writer(event):
            events.append(event)

    middleware = _StudioSummarizationMiddleware(
        FakeListChatModel(responses=["Objective and verified checkpoint preserved."]),
        trigger=("tokens", 200),
        keep=("messages", 2),
    )
    state = {
        "messages": [
            HumanMessage(id=f"message-{index}", content=f"part-{index}:" + ("x" * 1000))
            for index in range(6)
        ]
    }

    update = middleware.before_model(state, Runtime())

    assert update and update["messages"]
    assert len(events) == 1
    assert events[0]["type"] == "context_compaction"
    assert events[0]["status"] == "completed"
    assert events[0]["token_count_before"] >= 200


def test_graph_runtime_returns_tool_failure_to_the_model_for_recovery(
    tmp_path,
    monkeypatch,
):
    capability = Capability(
        function_name="lookup_remote_data",
        display_name="lookup_remote_data",
        kind="tool",
        description="Read remote data.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        retry_safe=True,
    )
    model_observed_error = False

    def failed_execute(capability_arg, record, arguments):
        del capability_arg, record, arguments
        raise TimeoutError("remote lookup timed out")

    def model_turn(record, messages, requested_model, tools):
        nonlocal model_observed_error
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-failing-tool",
                        name="lookup_remote_data",
                        arguments={},
                    )
                ],
            )
            return
        model_observed_error = "timed out" in str(tool_messages[-1].get("content"))
        yield ModelStreamEvent(kind="content", content="The lookup failed; no result was claimed.")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(monkeypatch, [capability], failed_execute)
    run = _run()
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )

    events = list(
        runtime.stream(
            _business_record(),
            run,
            requested_model=None,
            user_prompt="Look up the remote data",
            include_history=False,
        )
    )

    assert model_observed_error is True
    failed = next(
        item
        for item in events
        if item.get("call_id") == "call-failing-tool" and item.get("status") == "failed"
    )
    assert "timed out" in failed["error"]
    assert run.tool_invocations[0]["status"] == "failed"
    assert any(
        item["type"] == "token" and "no result was claimed" in item["content"]
        for item in events
    )


def test_graph_runtime_validates_raw_json_schema_before_executing_a_capability(
    tmp_path,
    monkeypatch,
):
    capability = Capability(
        function_name="read_workspace_file",
        display_name="read_workspace_file",
        kind="tool",
        description="Read one workspace file.",
        input_schema={
            "type": "object",
            "properties": {"file_id": {"type": "string", "minLength": 1}},
            "required": ["file_id"],
            "additionalProperties": False,
        },
        retry_safe=True,
    )
    executions = 0

    def must_not_execute(capability_arg, record, arguments):
        nonlocal executions
        del capability_arg, record, arguments
        executions += 1
        return _result("invalid execution")

    def model_turn(record, messages, requested_model, tools):
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-invalid-arguments",
                        name="read_workspace_file",
                        arguments={},
                    )
                ],
            )
            return
        yield ModelStreamEvent(kind="content", content="The invalid call was rejected.")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(monkeypatch, [capability], must_not_execute)
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )

    events = list(
        runtime.stream(
            _business_record(),
            _run(),
            requested_model=None,
            user_prompt="Read the file",
            include_history=False,
        )
    )

    assert executions == 0
    failed = next(
        item
        for item in events
        if item.get("call_id") == "call-invalid-arguments" and item.get("status") == "failed"
    )
    assert "required property" in failed["error"]


def test_graph_runtime_retries_transient_model_failures_before_succeeding(
    tmp_path,
    monkeypatch,
):
    attempts = 0

    def flaky_model_turn(record, messages, requested_model, tools):
        nonlocal attempts
        del record, messages, requested_model, tools
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary provider failure")
        yield ModelStreamEvent(kind="content", content="Recovered after retry.")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(
        monkeypatch,
        [],
        lambda capability, record, arguments: _result(),
    )
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=flaky_model_turn,
    )

    events = list(
        runtime.stream(
            _business_record(),
            _run(),
            requested_model=None,
            user_prompt="Run with retry",
            include_history=False,
        )
    )

    assert attempts == 3
    assert any(
        item["type"] == "token" and item["content"] == "Recovered after retry."
        for item in events
    )


def test_graph_runtime_propagates_model_failure_after_retry_budget_is_exhausted(
    tmp_path,
    monkeypatch,
):
    attempts = 0

    def failed_model_turn(record, messages, requested_model, tools):
        nonlocal attempts
        del record, messages, requested_model, tools
        attempts += 1
        raise ConnectionError("provider remains unavailable")
        yield  # pragma: no cover - keep this callable an iterator

    _configure_runtime_dependencies(
        monkeypatch,
        [],
        lambda capability, record, arguments: _result(),
    )
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=failed_model_turn,
    )

    with pytest.raises(ConnectionError, match="remains unavailable"):
        list(
            runtime.stream(
                _business_record(),
                _run(),
                requested_model=None,
                user_prompt="Run until retry is exhausted",
                include_history=False,
            )
        )

    assert attempts == 3


def test_graph_runtime_interrupts_and_resumes_the_same_checkpoint(
    tmp_path,
    monkeypatch,
):
    question_capability = Capability(
        function_name="request_user_input",
        display_name="request_user_input",
        kind="tool",
        description="Ask a contextual question.",
        protocol="user_input",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    )
    deferred_capability = Capability(
        function_name="write_after_confirmation",
        display_name="write_after_confirmation",
        kind="tool",
        description="A mutation that must wait for confirmation.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    deferred_executions = 0
    observed_resume_payload = ""

    def execute(capability_arg, record, arguments):
        nonlocal deferred_executions
        del record, arguments
        if capability_arg.function_name == "write_after_confirmation":
            deferred_executions += 1
        return _result("unexpected")

    def model_turn(record, messages, requested_model, tools):
        nonlocal observed_resume_payload
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-question-1",
                        name="request_user_input",
                        arguments={
                            "question": "Which output format?",
                            "options": [
                                {
                                    "label": "Markdown",
                                    "description": "Readable report",
                                    "recommended": True,
                                },
                                {"label": "JSON", "description": "Machine-readable output"},
                            ],
                        },
                    ),
                    ModelToolCall(
                        id="call-deferred-write",
                        name="write_after_confirmation",
                        arguments={},
                    ),
                ],
            )
            return
        observed_resume_payload = "\n".join(str(item.get("content")) for item in tool_messages)
        yield ModelStreamEvent(kind="content", content="Continued with the confirmed format.")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(
        monkeypatch,
        [question_capability, deferred_capability],
        execute,
    )
    record = _business_record()
    source_run = _run(run_id="run-source")
    checkpointer = InMemorySaver()
    runtime = StudioGraphRuntime(
        checkpointer=checkpointer,
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )

    waiting_events = list(
        runtime.stream(
            record,
            source_run,
            requested_model=None,
            user_prompt="Ask before writing",
            include_history=False,
        )
    )

    question = next(item["question"] for item in waiting_events if item["type"] == "question")
    waiting = next(item for item in waiting_events if item["type"] == "waiting_for_user")
    assert waiting["question_ids"] == [question["id"]]
    assert source_run.status == "waiting_for_user"
    assert deferred_executions == 0
    assert len(record.context.questions) == 1

    question["status"] = "answered"
    question["answer"] = "Markdown"
    continuation_run = _run(run_id="run-continuation")
    continuation_run.resumed_from_run_id = source_run.id
    resumed_events = list(
        runtime.stream(
            record,
            continuation_run,
            requested_model=None,
            user_prompt=None,
            include_history=True,
            resume_payload={question["id"]: "Markdown"},
        )
    )

    assert deferred_executions == 0
    assert len(record.context.questions) == 1
    assert question["id"] in observed_resume_payload
    assert "Markdown" in observed_resume_payload
    assert not any(item["type"] == "waiting_for_user" for item in resumed_events)
    assert not any(item["type"] == "question" for item in resumed_events)
    assert any(
        item["type"] == "token" and "Continued" in item["content"]
        for item in resumed_events
    )


def test_failed_continuation_can_retry_from_its_durable_graph_checkpoint(
    tmp_path,
    monkeypatch,
):
    capability = Capability(
        function_name="request_user_input",
        display_name="request_user_input",
        kind="tool",
        description="Ask before continuing.",
        protocol="user_input",
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    )
    continuation_attempts = 0

    def model_turn(record, messages, requested_model, tools):
        nonlocal continuation_attempts
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call-retry-question",
                        name="request_user_input",
                        arguments={"question": "Continue?"},
                    )
                ],
            )
            return
        continuation_attempts += 1
        if continuation_attempts <= 3:
            raise ConnectionError("continuation provider failure")
        yield ModelStreamEvent(kind="content", content="Continuation recovered.")
        yield ModelStreamEvent(kind="completed")

    _configure_runtime_dependencies(
        monkeypatch,
        [capability],
        lambda capability_arg, record, arguments: _result(),
    )
    record = _business_record()
    runtime = StudioGraphRuntime(
        checkpointer=InMemorySaver(),
        ledger=ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3"),
        model_turn=model_turn,
    )
    waiting_events = list(
        runtime.stream(
            record,
            _run(run_id="run-retry-source"),
            requested_model=None,
            user_prompt="Wait for confirmation",
            include_history=False,
        )
    )
    question = next(item["question"] for item in waiting_events if item["type"] == "question")
    question["status"] = "answered"
    question["answer"] = "Continue"
    resume_payload = {question["id"]: "Continue"}

    failed_continuation = _run(run_id="run-retry-failed")
    failed_continuation.resumed_from_run_id = "run-retry-source"
    with pytest.raises(ConnectionError, match="continuation provider failure"):
        list(
            runtime.stream(
                record,
                failed_continuation,
                requested_model=None,
                user_prompt=None,
                resume_payload=resume_payload,
            )
        )
    assert continuation_attempts == 3

    retry_continuation = _run(run_id="run-retry-success")
    retry_continuation.resumed_from_run_id = "run-retry-source"
    retry_events = list(
        runtime.stream(
            record,
            retry_continuation,
            requested_model=None,
            user_prompt=None,
            resume_payload=resume_payload,
        )
    )

    assert continuation_attempts == 4
    assert not any(item["type"] in {"question", "waiting_for_user"} for item in retry_events)
    assert any(
        item["type"] == "token" and item["content"] == "Continuation recovered."
        for item in retry_events
    )


def test_studio_chat_model_preserves_reasoning_content_and_tool_calls():
    captured: dict = {}

    def model_turn(record, messages, requested_model, tools):
        captured.update(
            record=record,
            messages=messages,
            requested_model=requested_model,
            tools=tools,
        )
        yield ModelStreamEvent(kind="reasoning", content="inspect the workspace")
        yield ModelStreamEvent(kind="content", content="I need one tool.")
        yield ModelStreamEvent(
            kind="completed",
            tool_calls=[
                ModelToolCall(
                    id="call-context-1",
                    name="read_business_context",
                    arguments={"include_evidence": True},
                )
            ],
        )

    record = _business_record()
    model = StudioChatModel(
        record=record,
        requested_model="test-provider-model",
        model_turn=model_turn,
    )
    bound = model.bind_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "read_business_context",
                    "description": "Read the current context.",
                    "parameters": {
                        "type": "object",
                        "properties": {"include_evidence": {"type": "boolean"}},
                    },
                },
            }
        ]
    )

    response = bound.invoke([HumanMessage(content="Inspect the business")])

    assert isinstance(response, AIMessage)
    assert response.content == "I need one tool."
    assert response.additional_kwargs["reasoning_content"] == "inspect the workspace"
    assert response.tool_calls == [
        {
            "name": "read_business_context",
            "args": {"include_evidence": True},
            "id": "call-context-1",
            "type": "tool_call",
        }
    ]
    assert captured["record"] is record
    assert captured["requested_model"] == "test-provider-model"
    assert captured["messages"][-1]["role"] == "user"
    assert captured["messages"][-1]["content"] == "Inspect the business"
    assert captured["tools"][0]["function"]["name"] == "read_business_context"


def test_studio_chat_model_does_not_turn_transport_failure_into_success():
    def failed_turn(record, messages, requested_model, tools):
        del record, messages, requested_model, tools
        raise ConnectionError("provider unavailable")
        yield  # pragma: no cover - keep this callable an iterator

    model = StudioChatModel(record=_business_record(), model_turn=failed_turn)

    with pytest.raises(ConnectionError, match="provider unavailable"):
        model.invoke([HumanMessage(content="Run the task")])


def test_tool_execution_ledger_replays_durable_success_across_instances(tmp_path):
    path = tmp_path / "tool-ledger.sqlite3"
    calls = 0

    def execute() -> CapabilityResult:
        nonlocal calls
        calls += 1
        return _result("first")

    first, first_replayed = ToolExecutionLedger(path).execute_once(
        scope="business:session",
        call_id="call-1",
        capability_name="read_business_context",
        arguments={"nested": {"b": 2, "a": 1}},
        retry_safe=True,
        executor=execute,
    )
    replayed, was_replayed = ToolExecutionLedger(path).execute_once(
        scope="business:session",
        call_id="call-1",
        capability_name="read_business_context",
        arguments={"nested": {"a": 1, "b": 2}},
        retry_safe=True,
        executor=execute,
    )

    assert calls == 1
    assert first_replayed is False
    assert was_replayed is True
    assert replayed == first


def test_tool_execution_ledger_rejects_reused_call_id_with_different_input(tmp_path):
    ledger = ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3")
    ledger.execute_once(
        scope="business:session",
        call_id="call-collision",
        capability_name="workspace_lookup",
        arguments={"query": "orders"},
        retry_safe=True,
        executor=lambda: _result(),
    )

    with pytest.raises(ToolExecutionCollision, match="reused"):
        ledger.execute_once(
            scope="business:session",
            call_id="call-collision",
            capability_name="workspace_lookup",
            arguments={"query": "customers"},
            retry_safe=True,
            executor=lambda: _result("must-not-run"),
        )

    with pytest.raises(ToolExecutionCollision, match="reused"):
        ledger.execute_once(
            scope="business:session",
            call_id="call-collision",
            capability_name="different_capability",
            arguments={"query": "orders"},
            retry_safe=True,
            executor=lambda: _result("must-not-run"),
        )


def test_retry_safe_failure_can_retry_but_non_idempotent_failure_is_blocked(tmp_path):
    ledger = ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3")
    safe_attempts = 0

    def flaky_read() -> CapabilityResult:
        nonlocal safe_attempts
        safe_attempts += 1
        if safe_attempts == 1:
            raise TimeoutError("temporary read timeout")
        return _result("recovered")

    with pytest.raises(TimeoutError, match="temporary"):
        ledger.execute_once(
            scope="business:session",
            call_id="call-safe-retry",
            capability_name="read_remote_catalog",
            arguments={},
            retry_safe=True,
            executor=flaky_read,
        )

    result, replayed = ledger.execute_once(
        scope="business:session",
        call_id="call-safe-retry",
        capability_name="read_remote_catalog",
        arguments={},
        retry_safe=True,
        executor=flaky_read,
    )
    assert safe_attempts == 2
    assert result.output == {"value": "recovered"}
    assert replayed is False

    mutation_attempts = 0

    def uncertain_mutation() -> CapabilityResult:
        nonlocal mutation_attempts
        mutation_attempts += 1
        raise ConnectionError("connection dropped after request was sent")

    with pytest.raises(ConnectionError, match="dropped"):
        ledger.execute_once(
            scope="business:session",
            call_id="call-unsafe",
            capability_name="external_create_order",
            arguments={"amount": 10},
            retry_safe=False,
            executor=uncertain_mutation,
        )

    with pytest.raises(ToolExecutionUncertain, match="dropped"):
        ledger.execute_once(
            scope="business:session",
            call_id="call-unsafe",
            capability_name="external_create_order",
            arguments={"amount": 10},
            retry_safe=False,
            executor=uncertain_mutation,
        )
    assert mutation_attempts == 1


def test_nested_execution_of_the_same_call_is_rejected_in_process(tmp_path):
    ledger = ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3")
    nested_executions = 0

    def outer_executor() -> CapabilityResult:
        nonlocal nested_executions

        def nested_executor() -> CapabilityResult:
            nonlocal nested_executions
            nested_executions += 1
            return _result("nested")

        with pytest.raises(ToolExecutionInProgress, match="already running"):
            ledger.execute_once(
                scope="business:session",
                call_id="call-running",
                capability_name="read_workspace_file",
                arguments={"file_id": "file-1"},
                retry_safe=True,
                executor=nested_executor,
            )
        return _result("outer")

    result, replayed = ledger.execute_once(
        scope="business:session",
        call_id="call-running",
        capability_name="read_workspace_file",
        arguments={"file_id": "file-1"},
        retry_safe=True,
        executor=outer_executor,
    )

    assert nested_executions == 0
    assert result.output == {"value": "outer"}
    assert replayed is False


def test_stale_non_idempotent_running_call_is_uncertain(tmp_path):
    path = tmp_path / "tool-ledger.sqlite3"
    ledger = ToolExecutionLedger(path)

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO tool_execution_ledger (
                scope, call_id, capability_name, arguments_hash, arguments_json,
                status, result_json, error, owner_id, attempts, created_at, updated_at
            ) VALUES (
                'business:session', 'call-running', 'external_write',
                ?,
                '{}', 'running', '', '', 'stale-process', 1, 1, 1
            )
            """,
            (_arguments_hash({}),),
        )
        connection.commit()

    executions = 0

    def must_not_run() -> CapabilityResult:
        nonlocal executions
        executions += 1
        return _result("unexpected")

    with pytest.raises(ToolExecutionUncertain, match="uncertain"):
        ledger.execute_once(
            scope="business:session",
            call_id="call-running",
            capability_name="external_write",
            arguments={},
            retry_safe=False,
            executor=must_not_run,
        )
    assert executions == 0


def test_failed_retry_safe_call_can_be_retried(tmp_path):
    path = tmp_path / "tool-ledger.sqlite3"
    ledger = ToolExecutionLedger(path)

    with pytest.raises(RuntimeError, match="simulated crash"):
        ledger.execute_once(
            scope="business:session",
            call_id="call-recoverable",
            capability_name="read_workspace_file",
            arguments={"file_id": "file-1"},
            retry_safe=True,
            executor=lambda: (_ for _ in ()).throw(RuntimeError("simulated crash")),
        )

    # A recorded failure is retried rather than replayed as a success.
    result, replayed = ToolExecutionLedger(path).execute_once(
        scope="business:session",
        call_id="call-recoverable",
        capability_name="read_workspace_file",
        arguments={"file_id": "file-1"},
        retry_safe=True,
        executor=lambda: _result("recovered"),
    )

    assert result.output == {"value": "recovered"}
    assert replayed is False


def test_stale_retry_safe_running_call_can_be_recovered_after_process_loss(tmp_path):
    path = tmp_path / "tool-ledger.sqlite3"
    ledger = ToolExecutionLedger(path)
    arguments = {"file_id": "file-1"}

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO tool_execution_ledger (
                scope, call_id, capability_name, arguments_hash, arguments_json,
                status, result_json, error, owner_id, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'running', '', '', 'dead-process', 1, 1, 1)
            """,
            (
                "business:session",
                "call-stale-read",
                "read_workspace_file",
                _arguments_hash(arguments),
                json.dumps(arguments, sort_keys=True),
            ),
        )
        connection.commit()

    executions = 0

    def recover() -> CapabilityResult:
        nonlocal executions
        executions += 1
        return _result("recovered-after-crash")

    result, replayed = ledger.execute_once(
        scope="business:session",
        call_id="call-stale-read",
        capability_name="read_workspace_file",
        arguments=arguments,
        retry_safe=True,
        executor=recover,
    )

    assert executions == 1
    assert result.output == {"value": "recovered-after-crash"}
    assert replayed is False
    with sqlite3.connect(path) as connection:
        status, attempts = connection.execute(
            "SELECT status, attempts FROM tool_execution_ledger WHERE call_id = ?",
            ("call-stale-read",),
        ).fetchone()
    assert (status, attempts) == ("succeeded", 2)


def test_same_call_id_is_isolated_by_execution_scope(tmp_path):
    ledger = ToolExecutionLedger(tmp_path / "tool-ledger.sqlite3")
    calls: list[str] = []

    for scope in ("business-a:session", "business-b:session"):
        result, replayed = ledger.execute_once(
            scope=scope,
            call_id="provider-call-1",
            capability_name="read_business_context",
            arguments={},
            retry_safe=True,
            executor=lambda scope=scope: (calls.append(scope) or _result(scope)),
        )
        assert result.output == {"value": scope}
        assert replayed is False

    assert calls == ["business-a:session", "business-b:session"]
