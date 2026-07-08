"""通用 Agent 事件流 → SSE 帧（心跳 / 超时 / 工具轨迹 / 落盘）。

从 Agent 流式对话沉淀出的可复用流式内核：任何「deepagents Agent + 历史消息」
都可用它跑一轮流式对话，并获得同样的健壮性保障（每轮必落盘、慢工具心跳、单轮总超时、
工具调用轨迹）。playground（通用 Agent 平台）复用它，避免重复实现这套生命周期逻辑。

调用方通过 `persist(content, thinking, tools)` 回调决定把本轮助手消息落到哪里。
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Callable

from langchain_core.messages import HumanMessage

from app.core.config import settings
from app.domain.models import ToolTrace
from app.core.streaming import ThinkParser, sse


def _coerce_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else item.get("text", "")
            for item in content
            if isinstance(item, (str, dict))
        )
    return str(content or "")


async def stream_agent(
    agent,
    lc_history: list,
    user_message: str,
    persist: Callable[[str, str, list[ToolTrace]], None],
    *,
    recursion_limit: int | None = None,
    aborted_note: str = "（本轮执行被中断，任务已终止作废。）",
) -> AsyncIterator[str]:
    """跑一轮 Agent 流式对话，产出 SSE 文本帧。轮末调用 persist 落盘并推送 trace。"""
    recursion_limit = recursion_limit or settings.verify_recursion_limit

    parser = ThinkParser()
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_traces: list[ToolTrace] = []
    tool_started_at: dict[str, float] = {}
    current_tool = ""
    turn_start = time.monotonic()
    saved = False

    def _persist_once() -> None:
        nonlocal saved
        if saved:
            return
        saved = True
        persist("".join(content_parts), "".join(thinking_parts), tool_traces)

    agen = agent.astream_events(
        {"messages": lc_history + [HumanMessage(content=user_message)]},
        version="v2",
        config={"recursion_limit": recursion_limit},
    )
    ait = agen.__aiter__()
    next_task: asyncio.Task | None = None

    try:
        while True:
            if next_task is None:
                next_task = asyncio.ensure_future(anext(ait))
            done, _ = await asyncio.wait({next_task}, timeout=settings.verify_heartbeat_interval)
            elapsed = time.monotonic() - turn_start

            if not done:
                if elapsed > settings.verify_turn_timeout:
                    raise TimeoutError(
                        f"本轮执行超过 {settings.verify_turn_timeout}s 上限，已强制终止"
                    )
                yield sse(
                    "heartbeat",
                    tool=current_tool,
                    elapsed=int(elapsed),
                    message=(f"仍在执行 {current_tool}…" if current_tool else "AI 推理中…")
                            + f"（本轮已 {int(elapsed)}s）",
                )
                continue

            task, next_task = next_task, None
            try:
                event = task.result()
            except StopAsyncIteration:
                break

            kind = event.get("event", "")

            if kind == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk:
                    text = _coerce_text(getattr(chunk, "content", ""))
                    if text:
                        for ev_type, ev_text in parser.feed(text):
                            if ev_type == "thinking":
                                thinking_parts.append(ev_text)
                                yield sse("thinking", delta=ev_text)
                            else:
                                content_parts.append(ev_text)
                                yield sse("content", delta=ev_text)

            elif kind == "on_tool_start":
                tool_name = event.get("name", "")
                run_id = str(event.get("run_id", ""))
                tool_input = event.get("data", {}).get("input", {})
                args_str = str(tool_input)[:300] if tool_input else ""
                current_tool = tool_name
                tool_started_at[run_id] = time.monotonic()
                tool_traces.append(ToolTrace(name=tool_name, args_summary=args_str))
                yield sse("tool_call", name=tool_name, args=args_str)

            elif kind == "on_tool_end":
                tool_name = event.get("name", "")
                run_id = str(event.get("run_id", ""))
                output = event.get("data", {}).get("output", "")
                output_str = _coerce_text(output) if not isinstance(output, str) else output
                t_start = tool_started_at.pop(run_id, None)
                tool_elapsed = round(time.monotonic() - t_start, 1) if t_start else None
                current_tool = ""
                summary = output_str[:600]
                if tool_traces and tool_traces[-1].name == tool_name and not tool_traces[-1].result_summary:
                    tool_traces[-1].result_summary = (
                        (f"[{tool_elapsed}s] " if tool_elapsed is not None else "") + summary
                    )
                yield sse("tool_result", name=tool_name, result=summary, elapsed=tool_elapsed)

        for ev_type, ev_text in parser.flush():
            if ev_type == "thinking":
                thinking_parts.append(ev_text)
                yield sse("thinking", delta=ev_text)
            else:
                content_parts.append(ev_text)
                yield sse("content", delta=ev_text)

    except TimeoutError as exc:
        note = (f"\n\n⏱️ {exc}。本轮任务已终止作废。"
                "建议缩小范围（如只处理一条规则、给 execute 更精确的 params）后再试。")
        content_parts.append(note)
        yield sse("error", message=str(exc))
        yield sse("content", delta=note)

    except asyncio.CancelledError:
        content_parts.append(f"\n\n{aborted_note}")
        raise

    except Exception as exc:  # noqa: BLE001
        exc_name = type(exc).__name__
        if "Recursion" in exc_name or "recursion" in str(exc).lower():
            note = ("\n\n❌ 本轮工具调用步数超过上限，已终止（任务过大）。"
                    "建议一次只处理一条诉求，或明确指定范围分批执行。")
        else:
            note = f"\n\n❌ Agent 执行出错：{exc_name}: {exc}\n本轮任务已终止作废。"
        content_parts.append(note)
        yield sse("error", message=f"{exc_name}: {exc}")
        yield sse("content", delta=note)

    finally:
        if next_task is not None:
            next_task.cancel()
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001
            pass
        _persist_once()

    if tool_traces:
        yield sse(
            "trace",
            steps=[
                {"name": t.name, "args": t.args_summary, "result": t.result_summary}
                for t in tool_traces
            ],
            total_elapsed=int(time.monotonic() - turn_start),
        )
