"""对话服务（流式）。

把「用户一句指令」转化为一段 SSE 事件流：思考过程、正式回答、工具调用、
资源刷新、状态变更。两条路径：

* LLM 路径：驱动 deep agent，实时转发其思考 / 工具调用 / 回答。
* 启发式降级路径：未配置 LLM 时，用确定性推导给出同样形态的交互体验。

对话结束后，把用户消息与助手消息（含思考与工具轨迹）持久化到存储层。
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

from . import heuristics, inference
from .agent import build_agent
from .llm import get_llm
from .models import (
    ChatMessage,
    ChatRole,
    FlowResult,
    RelationResult,
    Scenario,
    ScenarioStatus,
    ToolTrace,
)
from .skill_builder import materialize_skills
from .storage import store
from .streaming import ThinkParser, sse, sse_done
from .tools import TOOL_REFRESH_MAP

# 工具名 → 状态，用于在工具完成后推送状态变更
_TOOL_STATUS_MAP = {
    "deduce_relations": ScenarioStatus.RELATIONS_DEDUCED,
    "deduce_flow": ScenarioStatus.FLOW_DEDUCED,
    "generate_skills": ScenarioStatus.SKILLS_GENERATED,
}


def _new_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


def _coerce_text(content) -> str:
    """把 chunk.content 统一成字符串（兼容 str / 分块 list 两种返回形态）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content or "")


async def stream_chat(scenario: Scenario, user_message: str) -> AsyncIterator[str]:
    """对外的统一入口：产出 SSE 文本帧。"""
    # 先持久化用户消息
    store.append_message(
        scenario.id,
        ChatMessage(id=_new_msg_id(), role=ChatRole.USER, content=user_message),
    )

    if get_llm() is not None:
        gen = _stream_with_agent(scenario, user_message)
    else:
        gen = _stream_heuristic(scenario, user_message)

    async for frame in gen:
        yield frame
    yield sse_done()


# ===========================================================================
# LLM 路径
# ===========================================================================
async def _stream_with_agent(scenario: Scenario, user_message: str) -> AsyncIterator[str]:
    parser = ThinkParser()
    final_content: list[str] = []
    final_thinking: list[str] = []
    tool_traces: list[ToolTrace] = []

    # 组装历史消息（仅取角色与正式回答，保持上下文干净）
    history = store.get_messages(scenario.id)
    lc_messages = []
    for m in history[-20:]:  # 控制上下文长度
        if m.role == ChatRole.USER:
            lc_messages.append(HumanMessage(content=m.content))
        elif m.content:
            lc_messages.append(AIMessage(content=m.content))
    # 最后一条用户消息已在 history 中（刚持久化），无需重复追加

    try:
        agent = build_agent(scenario)
        async for event in agent.astream_events(
            {"messages": lc_messages},
            version="v2",
            config={"recursion_limit": 60},
        ):
            kind = event.get("event")

            if kind == "on_chat_model_stream":
                chunk = event["data"].get("chunk")
                text = _coerce_text(getattr(chunk, "content", "")) if chunk else ""
                if not text:
                    continue
                for ev_type, ev_text in parser.feed(text):
                    if ev_type == "thinking":
                        final_thinking.append(ev_text)
                    else:
                        final_content.append(ev_text)
                    yield sse(ev_type, delta=ev_text)

            elif kind == "on_tool_start":
                name = event.get("name", "")
                args = event["data"].get("input", {})
                args_summary = _summarize_args(args)
                yield sse("tool_call", name=name, args=args_summary)
                tool_traces.append(ToolTrace(name=name, args_summary=args_summary))

            elif kind == "on_tool_end":
                name = event.get("name", "")
                output = event["data"].get("output", "")
                result_summary = _summarize_result(output)
                yield sse("tool_result", name=name, result=result_summary)
                if tool_traces and tool_traces[-1].name == name:
                    tool_traces[-1].result_summary = result_summary
                # 工具产生副作用：通知前端刷新对应资源 + 状态
                if name in TOOL_REFRESH_MAP:
                    yield sse("refresh", resource=TOOL_REFRESH_MAP[name])
                if name in _TOOL_STATUS_MAP:
                    yield sse("status", status=_TOOL_STATUS_MAP[name].value)

        # 输出残留思考/正文
        for ev_type, ev_text in parser.flush():
            (final_thinking if ev_type == "thinking" else final_content).append(ev_text)
            yield sse(ev_type, delta=ev_text)

        # 安全网：模型有时会「口头声称成功」却没真正调用工具落盘。
        # 据用户意图核对产物是否真的存在，缺失则用确定性推导补齐并落盘。
        called = {t.name for t in tool_traces}
        async for frame, note in _ensure_artifacts(scenario.id, user_message, called):
            if note:
                final_content.append(note)
            yield frame

    except Exception as exc:  # noqa: BLE001
        yield sse("error", message=f"AI 处理出错：{exc}")
        final_content.append(f"（处理出错：{exc}）")

    # 持久化助手消息
    store.append_message(
        scenario.id,
        ChatMessage(
            id=_new_msg_id(),
            role=ChatRole.ASSISTANT,
            content="".join(final_content).strip(),
            thinking="".join(final_thinking).strip(),
            tools=tool_traces,
        ),
    )


def _summarize_args(args) -> str:
    if isinstance(args, dict):
        parts = []
        for k, v in args.items():
            sval = str(v)
            parts.append(f"{k}={sval[:80]}{'…' if len(sval) > 80 else ''}")
        return "，".join(parts)
    return str(args)[:160]


def _summarize_result(output) -> str:
    # 工具节点返回可能是 ToolMessage / str / 其它
    text = getattr(output, "content", None)
    if text is None:
        text = str(output)
    text = str(text)
    return text[:300] + ("…" if len(text) > 300 else "")


# ===========================================================================
# 安全网：确保用户意图对应的产物真正落盘（防止模型「只说不做」）
# ===========================================================================
async def _ensure_artifacts(scenario_id: str, user_message: str, called: set[str]):
    """核对用户意图对应的产物是否已落盘，缺失则用确定性推导补齐。

    产物有依赖链：技能 ← 流程 ← 关联关系。按链条逐级补齐。
    产出 (SSE帧, 追加到回答的提示文本或None) 二元组。
    """
    intent = _detect_intent(user_message)
    if intent not in {"relations", "flow", "skills"}:
        return
    sc = store.get(scenario_id)
    if sc is None or not sc.tables_meta:
        return

    need_flow = intent in {"flow", "skills"}
    need_skills = intent == "skills"

    # 1) 关联关系（流程/技能也以此为前提）
    if not sc.relations and "deduce_relations" not in called:
        result = inference.infer_relations(sc)
        sc.relations = result
        sc.status = ScenarioStatus.RELATIONS_DEDUCED
        store.save(sc)
        yield sse("tool_call", name="deduce_relations", args="系统自动补齐"), None
        yield sse("tool_result", name="deduce_relations", result=result.summary), None
        yield sse("refresh", resource="relations"), None
        yield sse("status", status=sc.status.value), None
        yield sse("content", delta="\n\n✅ 已自动完成并保存关联关系推导。"), "\n\n✅ 已自动完成并保存关联关系推导。"

    # 2) 业务流程
    if need_flow and not sc.flow and "deduce_flow" not in called:
        sc = store.get(scenario_id)
        result = inference.infer_flow(sc)
        sc.flow = result
        sc.status = ScenarioStatus.FLOW_DEDUCED
        store.save(sc)
        yield sse("tool_call", name="deduce_flow", args="系统自动补齐"), None
        yield sse("tool_result", name="deduce_flow", result=result.summary), None
        yield sse("refresh", resource="flow"), None
        yield sse("status", status=sc.status.value), None
        yield sse("content", delta="\n\n✅ 已自动完成并保存业务流程推导。"), "\n\n✅ 已自动完成并保存业务流程推导。"

    # 3) 技能库
    if need_skills and "generate_skills" not in called:
        sc = store.get(scenario_id)
        if sc.flow and sc.flow.flow_steps and not sc.skills:
            specs = heuristics.build_skill_specs(sc)
            materialized = materialize_skills(sc, specs)
            sc.skills = materialized
            sc.status = ScenarioStatus.SKILLS_GENERATED
            store.save(sc)
            note = f"\n\n✅ 已生成并保存 {len(materialized)} 个技能到业务场景目录。"
            yield sse("tool_call", name="generate_skills", args="系统自动补齐"), None
            yield sse("tool_result", name="generate_skills", result=f"生成 {len(materialized)} 个技能"), None
            yield sse("refresh", resource="skills"), None
            yield sse("status", status=sc.status.value), None
            yield sse("content", delta=note), note


# ===========================================================================
# 启发式降级路径（无 LLM）
# ===========================================================================
_INTENT_KEYWORDS = {
    "relations": ("关联", "关系", "推导关系", "表关系"),
    "flow": ("流程", "推导流程", "业务流程"),
    "skills": ("技能", "skill", "技能库"),
}


def _detect_intent(message: str) -> str:
    low = message.lower()
    for intent, kws in _INTENT_KEYWORDS.items():
        if any(kw in message or kw in low for kw in kws):
            return intent
    return "general"


async def _stream_heuristic(scenario: Scenario, user_message: str) -> AsyncIterator[str]:
    intent = _detect_intent(user_message)
    content_buf: list[str] = []

    def emit_content(text: str):
        content_buf.append(text)
        return sse("content", delta=text)

    if not scenario.tables_meta and intent in {"relations", "flow", "skills"}:
        yield emit_content("当前业务场景尚未上传业务表，请先上传数据表再进行推导。")
        _persist_assistant(scenario, content_buf)
        return

    if intent == "relations":
        yield sse("thinking", delta="正在比对各表字段名语义、数据类型与样本值重叠率，识别关联键……")
        yield sse("tool_call", name="deduce_relations", args="启发式关联推导")
        result: RelationResult = heuristics.deduce_relations(scenario)
        scenario.relations = result
        scenario.status = ScenarioStatus.RELATIONS_DEDUCED
        store.save(scenario)
        yield sse("tool_result", name="deduce_relations", result=result.summary)
        yield sse("refresh", resource="relations")
        yield sse("status", status=scenario.status.value)
        lines = "\n".join(
            f"- {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}"
            f"（{r.relation_type}，置信度 {r.confidence:.0%}）"
            for r in result.relations
        )
        text = f"已完成关联关系推导。\n\n{result.summary}\n\n{lines or '（未发现明显关联）'}"
        if result.ambiguous_questions:
            text += "\n\n需确认：\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(result.ambiguous_questions))
        text += "\n\n确认无误后可说「推导业务流程」。"
        yield emit_content(text)

    elif intent == "flow":
        yield sse("thinking", delta="以结果表为终点逆向追溯数据流向，识别过滤/关联/聚合/计算等环节……")
        yield sse("tool_call", name="deduce_flow", args="启发式流程推导")
        result: FlowResult = heuristics.deduce_flow(scenario)
        scenario.flow = result
        scenario.status = ScenarioStatus.FLOW_DEDUCED
        store.save(scenario)
        yield sse("tool_result", name="deduce_flow", result=result.summary)
        yield sse("refresh", resource="flow")
        yield sse("status", status=scenario.status.value)
        lines = "\n".join(
            f"- 步骤{s.step_id}：{s.step_name}（{s.operation}）—— {s.description}"
            for s in result.flow_steps
        )
        text = f"已完成业务流程推导。\n\n{result.summary}\n\n{lines}"
        if result.ambiguous_questions:
            text += "\n\n需确认：\n" + "\n".join(f"- {q}" for q in result.ambiguous_questions)
        text += "\n\n确认无误后可说「生成技能库」。"
        yield emit_content(text)

    elif intent == "skills":
        if not scenario.flow or not scenario.flow.flow_steps:
            yield emit_content("尚未推导业务流程，无法生成技能。请先说「推导业务流程」。")
        else:
            yield sse("thinking", delta="将每个业务流程步骤固化为可复用 Skill（SKILL.md + scripts/run.py）……")
            yield sse("tool_call", name="generate_skills", args="生成技能库")
            specs = heuristics.build_skill_specs(scenario)
            materialized = materialize_skills(scenario, specs)
            scenario.skills = materialized
            scenario.status = ScenarioStatus.SKILLS_GENERATED
            store.save(scenario)
            yield sse("tool_result", name="generate_skills", result=f"生成 {len(materialized)} 个技能")
            yield sse("refresh", resource="skills")
            yield sse("status", status=scenario.status.value)
            names = "\n".join(f"- {s.name}（{s.operation}）" for s in materialized)
            yield emit_content(f"已生成 {len(materialized)} 个技能：\n\n{names}\n\n"
                               "现在可以上传新数据并下达业务指令，或在「技能」标签进化新能力。")
    else:
        status_label = scenario.status.value
        yield emit_content(
            f"收到：「{user_message}」。\n\n当前业务场景「{scenario.name}」状态：{status_label}。\n\n"
            "（当前为无 LLM 的启发式模式）你可以：\n"
            "① 上传业务表格\n② 说「推导关联关系」\n③ 说「推导业务流程」\n④ 说「生成技能库」"
        )

    _persist_assistant(scenario, content_buf)


def _persist_assistant(scenario: Scenario, content_buf: list[str]) -> None:
    store.append_message(
        scenario.id,
        ChatMessage(
            id=_new_msg_id(),
            role=ChatRole.ASSISTANT,
            content="".join(content_buf).strip(),
        ),
    )
