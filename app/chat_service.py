"""蒸馏对话服务（v1.0.5）。

职责：蒸馏阶段的流式对话（推导关联/流程/生成技能）。
与验证通道（verify_service.py）完全分离，不提供执行/验证功能。
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

from . import executor, inference, transform_builder
from .agent import build_agent
from .llm import get_llm
from .models import (
    ChatMessage,
    ChatRole,
    Scenario,
    ScenarioStatus,
    TableRole,
    ToolTrace,
)
from .skill_builder import materialize_skills
from .storage import store
from .streaming import ThinkParser, sse, sse_done
from .tools import TOOL_REFRESH_MAP

# 蒸馏工具名 → 完成后应推送的状态
_TOOL_STATUS_MAP = {
    "deduce_relations": ScenarioStatus.RELATIONS_DEDUCED,
    "deduce_flow": ScenarioStatus.FLOW_DEDUCED,
    "generate_skills": ScenarioStatus.SKILLS_GENERATED,
}


def _new_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


def _coerce_text(content) -> str:
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

    history = store.get_messages(scenario.id)
    lc_messages = []
    for m in history[-20:]:
        if m.role == ChatRole.USER:
            lc_messages.append(HumanMessage(content=m.content))
        elif m.content:
            lc_messages.append(AIMessage(content=m.content))

    try:
        agent = build_agent(scenario)
        async for event in agent.astream_events(
            {"messages": lc_messages},
            version="v2",
            config={"recursion_limit": 120},
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
                if name in TOOL_REFRESH_MAP:
                    yield sse("refresh", resource=TOOL_REFRESH_MAP[name])
                if name in _TOOL_STATUS_MAP:
                    # 只在工具明确返回 ✅ 时才推送状态变更，避免失败时误报
                    tool_output_str = str(output)
                    if tool_output_str.lstrip().startswith("✅"):
                        yield sse("status", status=_TOOL_STATUS_MAP[name].value)
                # 关联/流程推导后若有不确定项，主动弹出结构化交互（让前端渲染表单）
                if name in ("deduce_relations", "deduce_flow"):
                    sc = store.get(scenario.id)
                    qs = []
                    if name == "deduce_relations" and sc and sc.relations:
                        qs = list(sc.relations.ambiguous_questions or [])
                    elif name == "deduce_flow" and sc and sc.flow:
                        qs = list(sc.flow.ambiguous_questions or [])
                    if qs:
                        yield sse("interaction", interaction=_step_interaction(name, qs))

        for ev_type, ev_text in parser.flush():
            (final_thinking if ev_type == "thinking" else final_content).append(ev_text)
            yield sse(ev_type, delta=ev_text)

    except Exception as exc:  # noqa: BLE001
        err_str = str(exc)
        # 检测常见的上下文溢出错误（MiniMax/OpenAI/Claude 各自的错误码）
        is_context_overflow = (
            "999" in err_str
            or "context_length_exceeded" in err_str
            or "too long" in err_str.lower()
            or "maximum context" in err_str.lower()
            or "token" in err_str.lower() and "limit" in err_str.lower()
        )
        if is_context_overflow:
            user_hint = (
                "AI 处理出错：对话上下文过长。建议：\n"
                "① 减少一次性提问的内容长度\n"
                "② 新建会话后从当前步骤继续\n"
                "③ 或先运行「提取元数据蓝图」，让 AI 重新聚焦场景信息"
            )
            yield sse("error", message=user_hint)
            final_content.append(f"（上下文溢出：{exc}）")
        else:
            yield sse("error", message=f"AI 处理出错：{exc}")
            final_content.append(f"（处理出错：{exc}）")

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
    text = getattr(output, "content", None)
    if text is None:
        text = str(output)
    text = str(text)
    return text[:300] + ("…" if len(text) > 300 else "")


def _step_interaction(tool_name: str, questions: list[str]) -> dict:
    """通用结构化交互：把工具的待确认事项渲染为表单。

    严格要求：用户没回答前，agent **不应**进入下一步。
    """
    qs = questions[:4]
    label = "关联推导" if tool_name == "deduce_relations" else "业务流程推导"
    return {
        "type": "confirm",
        "question": f"🧩 {label}有以下待确认事项，请逐条确认后我再继续：\n" + "\n".join(f"  · {q}" for q in qs),
        "options": ["全部确认无误，可继续", "我有修正意见（在下方输入）"],
        "allow_custom": True,
        "context": f"{tool_name}_confirm",
    }


# ===========================================================================
# 启发式降级路径
# ===========================================================================
_INTENT_KEYWORDS = {
    "relations": ("关联", "关系", "ER", "字段语义", "字段含义", "推导关联"),
    "flow":      ("流程", "推导流程", "业务流程", "节点", "管线"),
    "skills":    ("技能", "skill", "技能库", "固化", "生成技能"),
    "execute":   ("执行", "复刻", "产出结果", "跑一下", "运行", "校验", "验证", "对照", "比对"),
    "metadata":  ("元数据", "结构", "扫描", "蓝图", "概览"),
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

    if not scenario.tables_meta and intent != "general":
        yield emit_content("当前业务场景尚未上传业务表，请先上传业务表/规则表/历史结果表（上传时选择角色）。")
        _persist_assistant(scenario, content_buf)
        return

    if intent == "relations":
        yield sse("thinking", delta="推导字段业务语义 + 表关联……")
        yield sse("tool_call", name="deduce_relations", args="启发式：字段语义+关联推导")
        result = inference.infer_relations(scenario)
        scenario.relations = result
        domain = transform_builder.build_domain_knowledge(scenario)
        scenario.domain_knowledge = domain
        if scenario.status in (ScenarioStatus.CREATED, ScenarioStatus.TABLES_UPLOADED):
            scenario.status = ScenarioStatus.RELATIONS_DEDUCED
        store.save(scenario)
        yield sse("tool_result", name="deduce_relations", result=result.summary)
        yield sse("refresh", resource="relations")
        yield sse("status", status=scenario.status.value)
        n_sem = sum(len(v) for v in (result.field_semantics or {}).values())
        lines = "\n".join(
            f"- {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}（{r.confidence:.0%}）"
            for r in result.relations
        )
        yield emit_content(
            f"已完成关联推导 + 为 {n_sem} 个字段标注业务语义。\n\n{result.summary}\n\n{lines or '（未发现明显关联）'}\n\n"
            "下一步：对我说「推导业务流程」。"
        )

    elif intent == "flow":
        if not scenario.relations:
            yield emit_content("⚠️ 请先「推导关联关系」（含字段语义），流程节点的能力描述与模板分派依赖这两份产物。")
        else:
            yield sse("thinking", delta="基于（表角色+字段语义+关联+规则结构）反推业务流程节点……")
            yield sse("tool_call", name="deduce_flow", args="启发式：业务流程推导")
            result = inference.infer_flow(scenario)
            scenario.flow = result
            domain = transform_builder.build_domain_knowledge(scenario)
            scenario.domain_knowledge = domain
            scenario.outputs = transform_builder.build_outputs(scenario, domain)
            if scenario.status in (ScenarioStatus.RELATIONS_DEDUCED, ScenarioStatus.TABLES_UPLOADED):
                scenario.status = ScenarioStatus.FLOW_DEDUCED
            store.save(scenario)
            yield sse("tool_result", name="deduce_flow", result=result.summary)
            yield sse("refresh", resource="flow")
            yield sse("status", status=scenario.status.value)
            steps_text = "\n".join(
                f"- 步骤{s.step_id} {s.step_name}（{s.template_kind or s.operation}）\n"
                f"  • 该做什么：{s.purpose}\n  • 能做什么：{s.capability}"
                for s in result.flow_steps
            )
            yield emit_content(
                f"已推导 {len(result.flow_steps)} 个流程节点：\n\n{steps_text}\n\n"
                "下一步：对我说「生成技能库」。"
            )

    elif intent == "skills":
        if not scenario.flow or not scenario.flow.flow_steps:
            yield emit_content("⚠️ 尚未推导业务流程。请先「推导业务流程」。")
        else:
            yield sse("thinking", delta="按流程节点固化为技能：每个节点一个 skill，外加主技能串联……")
            yield sse("tool_call", name="generate_skills", args="启发式：生成技能")
            try:
                materialized = materialize_skills(scenario)
            except Exception as exc:  # noqa: BLE001
                import traceback
                tb = traceback.format_exc()
                err_msg = f"❌ 技能生成失败（{type(exc).__name__}：{str(exc)[:300]}）"
                yield sse("tool_result", name="generate_skills", result=err_msg)
                yield emit_content(
                    f"{err_msg}\n\n<details><summary>堆栈（点击展开）</summary>\n\n```\n{tb[:1000]}\n```\n</details>\n\n"
                    "请检查后端日志后重试。"
                )
                _persist_assistant(scenario, content_buf)
                return
            scenario.skills = materialized
            scenario.status = ScenarioStatus.SKILLS_GENERATED
            store.save(scenario)
            yield sse("tool_result", name="generate_skills", result=f"✅ 生成 {len(materialized)} 个技能")
            yield sse("refresh", resource="skills")
            yield sse("status", status=scenario.status.value)
            names = "\n".join(f"- {s.name}（{s.operation}）" for s in materialized)
            yield emit_content(
                f"已生成 {len(materialized)} 个技能：\n\n{names}\n\n"
                "下一步：对我说「执行」对新数据复刻并对照历史结果。"
            )

    elif intent == "execute":
        if not scenario.skills:
            yield emit_content("⚠️ 尚未生成技能包。请先「生成技能库」。")
        else:
            yield emit_content(
                "✅ 技能包已生成，蒸馏完成。\n\n"
                "执行和验证请切换到**验证通道**（/verify），该通道与本平台代码完全隔离，\n"
                "只能调用 Skill 包中的工具，确保验证结果真实反映 Skill 包的独立能力。\n\n"
                f"直接访问：[验证通道 → {scenario.name}](/verify)"
            )

    elif intent == "metadata":
        from . import metadata
        report = metadata.build_metadata_report(scenario)
        yield emit_content(report[:3000] + ("…（已截断）" if len(report) > 3000 else ""))

    else:
        roles = "；".join(f"{t.table_name}={t.role}" for t in scenario.tables_meta) or "（无表）"
        yield emit_content(
            f"收到：「{user_message}」。\n\n"
            f"当前场景「{scenario.name}」状态：{scenario.status.value}\n表角色：{roles}\n\n"
            "（无 LLM 模式）5 步工作流：\n"
            "① 上传业务表 + 规则表(可选) + 历史结果表（上传时选角色）\n"
            "② 对我说「推导关联关系」（含字段语义）\n"
            "③ 对我说「推导业务流程」（节点带能力描述 + 规则结构映射）\n"
            "④ 对我说「生成技能库」（按节点固化）\n"
            "⑤ 对我说「执行」（含校验）"
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
