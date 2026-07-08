"""deepagents 内置工具屏蔽（v1.0.6）。

背景（真实复现的 bug）：`deepagents.create_deep_agent` 默认会给模型额外挂载一套
与我们业务无关的内置工具：`write_todos`（待办清单）、`ls`/`read_file`/`write_file`/
`edit_file`/`glob`/`grep`（沙盒虚拟文件系统）、`execute`（shell）、`task`（子代理委派）。
这些工具是 additive 的（deepagents 文档原文："Passing tools here is additive — it
never removes a built-in"），且不会主动在 system_prompt 里说明。

实测现象：模型会调用 `write_todos` 把"生成技能"标记为已完成，或者把内容"写"进沙盒
虚拟文件系统里的 `write_file`，然后照着这个假动作的结果，用与真调用 `generate_skills`
一模一样的语气汇报"✅ 技能库生成完成"——但平台真实的 `store.skills_dir()` 目录下
什么都没有。用户看到的"AI 说完成了，但界面和后端都没有文件"正是这个原因，不是
LLM 偶然瞎编，而是它手上真的有一套可以自欺欺人的工具。

修复：用一个 wrap_model_call 中间件，在请求发给模型之前，把这些内置工具从
`request.tools` 里过滤掉，让模型除了我们显式注册的业务工具外无路可走——
无法通过写虚拟文件或勾选待办来伪造"已完成"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

# deepagents 0.6.x 默认注入的内置工具名（与业务无关，一律屏蔽）
DEEPAGENTS_BUILTIN_TOOLS = frozenset({
    "write_todos",
    "ls", "read_file", "write_file", "edit_file", "glob", "grep",
    "execute",
    "task",
})


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class ExcludeBuiltinToolsMiddleware(AgentMiddleware):
    """请求发给模型前，剥离 deepagents 自动挂载的内置工具。"""

    def __init__(self, allow: set[str] | None = None) -> None:
        super().__init__()
        self.allow = allow or set()

    def _blocked(self, tool: Any) -> bool:
        name = _tool_name(tool)
        return bool(name and name in DEEPAGENTS_BUILTIN_TOOLS and name not in self.allow)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        filtered = [t for t in request.tools if not self._blocked(t)]
        return handler(request.override(tools=filtered))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        filtered = [t for t in request.tools if not self._blocked(t)]
        return await handler(request.override(tools=filtered))
