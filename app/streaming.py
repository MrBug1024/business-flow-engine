"""流式响应基础设施。

* `ThinkParser`：将模型输出流中的 `<think>...</think>` 思考内容与正式回答实时分离
  （MiniMax-M2 等模型把思考过程内联在 content 里，需要在 token 级别拆分）。
* `sse`：把事件序列化为 SSE 数据帧。

SSE 事件协议（每帧一个 JSON，含 `type` 字段）：
    thinking  {delta}      思考过程增量
    content   {delta}      正式回答增量
    tool_call {name,args}  开始调用工具/技能
    tool_result {name,result} 工具返回
    refresh   {resource}   某资源已更新（business_process/relations/flow/skills），前端据此刷新
    status    {status}     业务场景状态变更
    interaction {interaction} 结构化交互块（规范 Section 5），前端渲染为表单（Phase 0 审批等）
    error     {message}    出错
    done      {}           本轮结束
"""

from __future__ import annotations

import json
from typing import Any, Iterator

# 思考标签
_OPEN = "<think>"
_CLOSE = "</think>"
_MAX_TAG = max(len(_OPEN), len(_CLOSE))


class ThinkParser:
    """增量解析 `<think>` 标签，输出 ('thinking'|'content', text) 事件。

    通过保留尾部若干字符，避免标签被切分在两个 token 之间而漏判。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buffer += text
        events: list[tuple[str, str]] = []
        while True:
            if self._in_think:
                idx = self._buffer.find(_CLOSE)
                if idx == -1:
                    # 未见闭合标签：安全输出（保留可能构成标签前缀的尾部）
                    safe = self._buffer[: max(0, len(self._buffer) - (_MAX_TAG - 1))]
                    if safe:
                        events.append(("thinking", safe))
                        self._buffer = self._buffer[len(safe):]
                    break
                if idx > 0:
                    events.append(("thinking", self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(_CLOSE):]
                self._in_think = False
            else:
                idx = self._buffer.find(_OPEN)
                if idx == -1:
                    safe = self._buffer[: max(0, len(self._buffer) - (_MAX_TAG - 1))]
                    if safe:
                        events.append(("content", safe))
                        self._buffer = self._buffer[len(safe):]
                    break
                if idx > 0:
                    events.append(("content", self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(_OPEN):]
                self._in_think = True
        return events

    def flush(self) -> list[tuple[str, str]]:
        """流结束时输出残留缓冲。"""
        if not self._buffer:
            return []
        kind = "thinking" if self._in_think else "content"
        events = [(kind, self._buffer)]
        self._buffer = ""
        return events


def sse(event_type: str, **payload: Any) -> str:
    """将一个事件序列化为 SSE 数据帧。"""
    data = {"type": event_type, **payload}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_done() -> str:
    return sse("done")
