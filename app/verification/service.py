"""验证对话服务（v1.1.0）。

与蒸馏对话服务（chat_service.py）完全分离：
- 只使用 verification_agent（无平台工具访问权限）
- 独立的消息历史（存储 key 带 _verify 后缀）
- 只有 Skill 包工具可被调用

本轮次生命周期保障（针对历史版本"慢/卡/无反馈/任务边界混乱"的修复）：
1. **每一轮都必然落盘一条助手消息**——包括异常、超时、客户端断开、空回复。
   历史版本失败轮次不落盘，导致 verify_chat.jsonl 里出现连续多条 user 消息，
   下一轮 LLM 会把旧规则和新规则当成一个批量任务"合并执行"，这正是
   "输入新规则后 AI 以为要同时执行多条规则"的直接原因。
2. **心跳**：Agent 超过 verify_heartbeat_interval 秒无事件产出时，向前端推送
   heartbeat 帧（带当前工具名与已执行时长），用户不再面对完全静默的界面。
3. **总超时**：单轮超过 verify_turn_timeout 秒强制终止并明确告知，不再无限挂死。
4. **执行轨迹**：每轮的工具调用（名称/参数/结果摘要/耗时）随助手消息持久化，
   并在轮末通过 trace 帧推给前端，让用户知道系统执行了什么、基于什么数据。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

from app.core.config import settings
from app.core.llm import get_llm
from app.domain.models import ChatMessage, ChatRole, Scenario, ScenarioStatus, ToolTrace
from app.domain.storage import store
from app.core.streaming import ThinkParser, sse, sse_done
from app.verification.agent import build_verification_agent
from app.verification.state import response_marks_verified

# 失败/中断轮次落盘的占位回复：既保证 user/assistant 交替，也直接告诉下一轮的
# LLM"这个任务已作废"，不要续跑或与新任务合并。
_ABORTED_NOTE = "（本轮执行被中断，未产出正式回复；该任务已终止作废，后续消息将作为全新任务独立处理。）"


def _msg_id() -> str:
    return f"vmsg_{uuid.uuid4().hex[:12]}"


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


def _persist_assistant(
    scenario_id: str,
    content: str,
    thinking: str = "",
    tools: list[ToolTrace] | None = None,
) -> None:
    """落盘助手消息；内容为空时写入"任务作废"占位，保证 user/assistant 严格交替。"""
    store.append_verify_message(
        scenario_id,
        ChatMessage(
            id=_msg_id(),
            role=ChatRole.ASSISTANT,
            content=content.strip() or _ABORTED_NOTE,
            thinking=thinking.strip(),
            tools=tools or [],
        ),
    )


def _build_lc_history(scenario_id: str) -> list:
    """构建喂给 Agent 的历史消息（不含刚追加的本轮用户消息）。

    - 只保留最近 20 条，避免上下文无限膨胀；
    - 对"用户消息后没有任何助手回复"的悬挂轮次（历史版本失败不落盘留下的残留），
      就地补一条"任务已作废"的合成回复——否则 LLM 会把几条悬挂的旧请求
      和本轮新请求视为一个待办清单一起执行。
    """
    history = store.get_verify_messages(scenario_id)[:-1]  # 最后一条是本轮用户消息
    history = history[-20:]

    lc_history: list = []
    for m in history:
        if m.role == ChatRole.USER:
            if lc_history and isinstance(lc_history[-1], HumanMessage):
                lc_history.append(AIMessage(content=_ABORTED_NOTE))
            lc_history.append(HumanMessage(content=m.content))
        elif m.role == ChatRole.ASSISTANT and m.content:
            lc_history.append(AIMessage(content=m.content))
    if lc_history and isinstance(lc_history[-1], HumanMessage):
        lc_history.append(AIMessage(content=_ABORTED_NOTE))
    return lc_history


async def stream_verify(scenario: Scenario, user_message: str) -> AsyncIterator[str]:
    """验证通道流式对话入口。产出 SSE 文本帧。"""
    store.append_verify_message(
        scenario.id,
        ChatMessage(id=_msg_id(), role=ChatRole.USER, content=user_message),
    )

    if get_llm() is None:
        msg = "❌ LLM 未配置，无法使用验证通道。请先配置 LLM。"
        _persist_assistant(scenario.id, msg)
        yield sse("content", delta=msg)
        yield sse_done()
        return

    if not scenario.skills:
        msg = ("❌ 当前场景尚未生成技能包。\n\n请先在**蒸馏通道**完成以下步骤：\n"
               "1. 数据链路追踪\n2. 推导关联关系\n3. 推导业务流程\n4. 生成技能\n\n"
               "完成后再切换到验证通道。")
        _persist_assistant(scenario.id, msg)
        yield sse("content", delta=msg)
        yield sse_done()
        return

    async for frame in _stream_with_verify_agent(scenario, user_message):
        yield frame
    yield sse_done()


async def _stream_with_verify_agent(
    scenario: Scenario, user_message: str
) -> AsyncIterator[str]:
    try:
        agent = build_verification_agent(scenario)
    except RuntimeError as exc:
        msg = f"❌ 无法构建验证 Agent：{exc}"
        _persist_assistant(scenario.id, msg)
        yield sse("content", delta=msg)
        return

    lc_history = _build_lc_history(scenario.id)

    parser = ThinkParser()
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_traces: list[ToolTrace] = []
    tool_started_at: dict[str, float] = {}  # run_id -> 开始时刻
    current_tool = ""
    turn_start = time.monotonic()
    saved = False

    def _persist_once() -> None:
        nonlocal saved
        if saved:
            return
        saved = True
        _persist_assistant(
            scenario.id,
            "".join(content_parts),
            "".join(thinking_parts),
            tool_traces,
        )

    agen = agent.astream_events(
        {"messages": lc_history + [HumanMessage(content=user_message)]},
        version="v2",
        config={"recursion_limit": settings.verify_recursion_limit},
    )
    ait = agen.__aiter__()
    next_task: asyncio.Task | None = None

    try:
        while True:
            if next_task is None:
                next_task = asyncio.ensure_future(anext(ait))
            done, _ = await asyncio.wait(
                {next_task}, timeout=settings.verify_heartbeat_interval
            )
            elapsed = time.monotonic() - turn_start

            if not done:
                # Agent 仍在执行（大概率是慢工具）：推心跳，别让用户面对死界面
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
                yield sse(
                    "tool_result",
                    name=tool_name,
                    result=summary,
                    elapsed=tool_elapsed,
                )

        for ev_type, ev_text in parser.flush():
            if ev_type == "thinking":
                thinking_parts.append(ev_text)
                yield sse("thinking", delta=ev_text)
            else:
                content_parts.append(ev_text)
                yield sse("content", delta=ev_text)

    except TimeoutError as exc:
        note = (
            f"\n\n⏱️ {exc}。\n本轮任务**已终止作废**，不会在后台继续执行。\n"
            "建议：缩小执行范围（如只验证一条规则、给 execute_skill 加更精确的 params），"
            "或分批执行后再继续。"
        )
        content_parts.append(note)
        yield sse("error", message=str(exc))
        yield sse("content", delta=note)

    except asyncio.CancelledError:
        # 客户端断开/主动停止：照样落盘"任务作废"，防止悬挂 user 消息污染下一轮
        content_parts.append(f"\n\n{_ABORTED_NOTE}")
        raise

    except Exception as exc:  # noqa: BLE001
        exc_name = type(exc).__name__
        if "Recursion" in exc_name or "recursion" in str(exc).lower():
            note = (
                "\n\n❌ 本轮工具调用步数超过上限，已终止（任务过大）。\n"
                "该任务**已作废**。建议：一次只验证一条规则，或明确指定范围分批执行。"
            )
        else:
            note = f"\n\n❌ 验证 Agent 执行出错：{exc_name}: {exc}\n本轮任务已终止作废。"
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

    # 轮末推送执行轨迹：用户由此知道本轮调了什么工具、参数、结果、耗时
    if tool_traces:
        yield sse(
            "trace",
            steps=[
                {"name": t.name, "args": t.args_summary, "result": t.result_summary}
                for t in tool_traces
            ],
            total_elapsed=int(time.monotonic() - turn_start),
        )

    # 若产出成功，更新场景状态
    full_response = "".join(content_parts)
    if response_marks_verified(full_response) and scenario.status == ScenarioStatus.SKILLS_GENERATED:
        scenario.status = ScenarioStatus.ACTIVE
        store.save(scenario)
