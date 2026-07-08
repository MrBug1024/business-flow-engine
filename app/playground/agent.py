"""Generic Agent platform runtime.

The playground Agent is intentionally independent from distillation scenarios.
It only sees resources that the user explicitly manages in the Agent platform:
uploaded Agent Skills, connected MCP servers, and configured subagents.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from typing import Any

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.profiles.harness import harness_profiles as hp
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.tools import BaseTool, StructuredTool

from app.core.agent_guard import DEEPAGENTS_BUILTIN_TOOLS, ExcludeBuiltinToolsMiddleware
from app.playground.sandbox import SkillSandboxBackend, reset_active_sandbox_id, set_active_sandbox_id
DEFAULT_SYSTEM_PROMPT = """你是「零号.奇点工坊」中的通用主 Agent。

你默认只是一个普通 AI 助手，可以正常聊天、解释、推理和处理通用问题。你不预设任何蒸馏平台的业务场景，也不假设存在知识表、规则表、结算表或固定业务数据。

当前可用能力只来自 Agent 平台的显式配置：
1. 已绑定给当前 Agent 的 Skill。
2. 已绑定给当前 Agent 的 MCP 工具。
3. 已启用并可由 task 调度的子 Agent。

工作准则：
- 先判断用户请求是否需要某个已绑定资源；不需要时直接用普通助手能力回答。
- 不要声称拥有未绑定给当前 Agent 的 Skill/MCP。
- 如果某个能力只绑定给子 Agent，应通过 task 委派给对应子 Agent，而不是假装主 Agent 可以直接使用。
- 使用 MCP 前，先理解工具名称、描述和输入要求；不要构造缺失依据的参数。
- 如果已绑定业务 action 工具（如 `*_describe_schema`、`*_search_knowledge`、`*_query_data`、`*_execute`），**必须直接调用这些工具**完成业务需求，不要临时创建脚本、不要用 shell 重写业务逻辑。
- 使用 Skill 前，先根据系统提示中的 Skill 名称和描述判断是否适用。**禁止直接读取 Skill 目录下的 JSON/配置/脚本文件来替代调用 action tools**——业务信息必须通过已注册的 action tools 获取。
- 历史附件只是当前会话的通用上下文，不等同于蒸馏阶段业务数据。"""

READ_ONLY_BUILTINS = {"ls", "read_file", "glob", "grep"}
MAIN_ALLOWED_BUILTINS = READ_ONLY_BUILTINS | {"task"}
SUB_ALLOWED_BUILTINS = READ_ONLY_BUILTINS
_PROFILE_LOCK = threading.Lock()
_SENTINEL = object()


class SandboxContextMiddleware(AgentMiddleware):
    """Route built-in execute calls to the sandbox selected for this Agent."""

    def __init__(self, sandbox_id: str) -> None:
        super().__init__()
        self.sandbox_id = str(sandbox_id or "")

    def wrap_tool_call(self, request, handler):
        token = set_active_sandbox_id(self.sandbox_id)
        try:
            return handler(request)
        finally:
            reset_active_sandbox_id(token)

    async def awrap_tool_call(self, request, handler):
        token = set_active_sandbox_id(self.sandbox_id)
        try:
            return await handler(request)
        finally:
            reset_active_sandbox_id(token)


def _clean_agent_name(value: str, fallback: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_-]", "_", value or "").strip("_")
    return (text or fallback)[:64]


def _resource_context(resources: dict | None) -> str:
    if not resources:
        return ""
    return json.dumps(resources, ensure_ascii=False, indent=2)


def _main_config_context(agent_config: dict | None) -> str:
    if not isinstance(agent_config, dict):
        return ""
    main = agent_config.get("main_agent") if isinstance(agent_config.get("main_agent"), dict) else {}
    prompt = str(main.get("system_prompt") or "").strip()
    return prompt


def _subagent_description(sub: dict, resource_summary: dict | None) -> str:
    prompt = str(sub.get("system_prompt") or "").strip()
    first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    if first_line:
        return first_line[:500]
    skills = len((resource_summary or {}).get("skills") or [])
    mcps = len((resource_summary or {}).get("mcps") or [])
    return f"{sub.get('name') or sub.get('id')}: handle delegated tasks with {skills} Skill(s) and {mcps} MCP server(s)."


def _profile_key_for_model(model) -> str:
    identifier = get_model_identifier(model)
    provider = get_model_provider(model)
    if provider and identifier and ":" not in identifier:
        return f"{provider}:{identifier}"
    if identifier and ":" in identifier:
        return identifier
    return provider or ""


@contextmanager
def _playground_harness_profile(model, allowed_builtin_tools: set[str]):
    """Apply playground-wide tool visibility after deepagents injects built-ins."""
    key = _profile_key_for_model(model)
    if not key:
        yield
        return
    override = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        excluded_tools=frozenset(DEEPAGENTS_BUILTIN_TOOLS - set(allowed_builtin_tools)),
    )
    with _PROFILE_LOCK:
        hp._ensure_harness_profiles_loaded()  # noqa: SLF001
        old = hp._HARNESS_PROFILES.get(key, _SENTINEL)  # noqa: SLF001
        hp._HARNESS_PROFILES[key] = (  # noqa: SLF001
            hp._merge_profiles(old, override) if old is not _SENTINEL else override  # noqa: SLF001
        )
        try:
            yield
        finally:
            if old is _SENTINEL:
                hp._HARNESS_PROFILES.pop(key, None)  # noqa: SLF001
            else:
                hp._HARNESS_PROFILES[key] = old  # noqa: SLF001


def build_playground_agent(
    *,
    llm,
    agent_config: dict,
    runtime_root: str,
    main_skill_sources: list[str] | None = None,
    main_tools: list[BaseTool] | None = None,
    subagent_specs: list[dict] | None = None,
    sandbox_map: dict[str, dict] | None = None,
    main_sandbox_id: str = "",
    resources_summary: dict | None = None,
    attachment_context: str = "",
    disable_execute: bool = False,
):
    """Build the generic main Agent from independent platform resources."""
    if llm is None:
        raise RuntimeError("LLM 未配置，无法构建 Agent。")

    main_cfg = agent_config.get("main_agent") or {}
    configured_main_prompt = _main_config_context(agent_config)
    resources_json = _resource_context(resources_summary)
    sandbox_map = sandbox_map or {}

    def sandbox_ready(sandbox_id: str) -> bool:
        row = sandbox_map.get(str(sandbox_id or ""))
        return bool(row and row.get("status") == "ready")

    def allowed_builtins(base: set[str], sandbox_id: str) -> set[str]:
        allow = set(base)
        if not disable_execute and sandbox_ready(sandbox_id):
            allow.add("execute")
        return allow

    def sandbox_middlewares(sandbox_id: str) -> list[AgentMiddleware]:
        return [SandboxContextMiddleware(sandbox_id)] if sandbox_id else []

    subagents: list[dict[str, Any]] = []
    for sub in subagent_specs or []:
        sub_cfg = sub.get("config") or {}
        sub_id = _clean_agent_name(str(sub_cfg.get("id") or ""), "subagent")
        sub_name = str(sub_cfg.get("name") or sub_id)[:80]
        sub_sandbox_id = str(sub.get("sandbox_id") or sub_cfg.get("sandbox_id") or "")
        sub_prompt = str(sub_cfg.get("system_prompt") or "").strip()
        sub_resources = sub.get("resources_summary") or {}
        sub_tool_names = sub_resources.get("action_tools") or []
        sub_system = (
            DEFAULT_SYSTEM_PROMPT
            + f"\n\n# 子 Agent 身份\n你是「{sub_name}」。只处理主 Agent 明确委派给你的任务。"
            + "\n不要使用或声称拥有未绑定给你的 Skill/MCP；能力不足时直接说明缺少什么。"
            + "\n\n# 当前子 Agent 可用资源\n"
            + (json.dumps(sub_resources, ensure_ascii=False, indent=2) if sub_resources else "（无）")
        )
        if disable_execute and sub_tool_names:
            sub_system += (
                "\n\n# 🛑 工具使用纪律（必须遵守）\n"
                "当前已注册可直接调用的业务 action 工具（"
                + ", ".join(sub_tool_names[:10])
                + "）。你必须遵守以下纪律：\n"
                "1. **必须优先调用已注册的 action tools** 完成所有业务操作（describe_schema / "
                "search_knowledge / list_knowledge / execute / query_data）。\n"
                "2. **禁止**使用 `read_file` 读取技能目录下的 JSON/配置/脚本文件来替代 action tools——"
                "所有业务信息（表结构、字段语义、知识条目）必须通过 action tools 获取。\n"
                "3. **禁止**临时创建 Python/SQL 脚本或使用 shell/execute 重写业务逻辑。\n"
                "4. **禁止**直接读取 domain_knowledge.json、output_specs.json、dispatch_config.json "
                "等配置文件——这些数据已通过 action tools 提供。\n"
                "5. 涉及业务数据查询、规则搜索、场景执行时，必须调用对应业务 action tools。\n"
            )
        if sub_prompt:
            sub_system += "\n\n# 子 Agent System Prompt\n" + sub_prompt
        subagents.append(
            {
                "name": sub_id,
                "description": _subagent_description(sub_cfg, sub_resources),
                "system_prompt": sub_system,
                "model": sub.get("model") or llm,
                "tools": list(sub.get("tools") or []),
                "skills": list(sub.get("skill_sources") or []),
                "middleware": [
                    *sandbox_middlewares(sub_sandbox_id),
                    ExcludeBuiltinToolsMiddleware(allow=allowed_builtins(SUB_ALLOWED_BUILTINS, sub_sandbox_id)),
                ],
            }
        )

    def list_agent_resources() -> str:
        """List resources bound to the main Agent and enabled subagents."""
        if not resources_summary:
            return "当前 Agent 未绑定任何 Skill、MCP 或子 Agent。请按普通助手方式回答。"
        return json.dumps(resources_summary, ensure_ascii=False, indent=2)

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            list_agent_resources,
            name="list_agent_resources",
            description="列出当前主 Agent 和已启用子 Agent 绑定的 Skill/MCP 资源。",
        )
    ]
    tools.extend(main_tools or [])

    context_parts = [
        DEFAULT_SYSTEM_PROMPT,
        "\n# 当前主 Agent 名称\n" + str(main_cfg.get("name") or "主 Agent"),
    ]
    if configured_main_prompt:
        context_parts.append("\n# 主 Agent System Prompt\n" + configured_main_prompt)
    if resources_json:
        context_parts.append("\n# 当前已绑定资源\n" + resources_json)
    else:
        context_parts.append("\n# 当前已绑定资源\n（无）")
    if attachment_context:
        context_parts.append("\n" + attachment_context)
    context_parts.append(
        "\n# 文件能力边界\n"
        "你只能读取当前运行目录中的 Skill 与附件文件。内置写文件能力不可用；"
        "只有当前 Agent 已选择且准备好沙箱环境时，才可以使用 execute 执行 Skill 脚本；"
        "需要外部系统能力时应使用已绑定 MCP 工具。"
    )
    if disable_execute:
        context_parts.append(
            "\n# 业务工具优先级\n"
            "当前已提供可直接调用的业务 action 工具。禁止使用 execute/shell 临时创建脚本；"
            "涉及业务数据查询、规则搜索、场景执行时必须调用对应业务工具。"
        )

    backend = SkillSandboxBackend(
        root_dir=runtime_root,
        sandbox=sandbox_map.get(main_sandbox_id),
        sandboxes=sandbox_map,
    )
    base_allow = MAIN_ALLOWED_BUILTINS if subagents else READ_ONLY_BUILTINS
    allow = allowed_builtins(base_allow, main_sandbox_id)

    global_allow = allowed_builtins(base_allow, main_sandbox_id)
    with _playground_harness_profile(llm, global_allow):
        return create_deep_agent(
            model=llm,
            tools=tools,
            system_prompt="\n\n".join(context_parts),
            skills=main_skill_sources or None,
            backend=backend,
            middleware=[
                *sandbox_middlewares(main_sandbox_id),
                ExcludeBuiltinToolsMiddleware(allow=global_allow),
            ],
            subagents=subagents,
        )
