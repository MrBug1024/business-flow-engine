"""蒸馏对话服务（v1.0.5）。

职责：蒸馏阶段的流式对话（链路追踪/推导关联/流程/生成技能）。
与 Agent 平台/旧验证服务完全分离，不提供执行/验证功能。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import AsyncIterator, Optional

from langchain_core.messages import AIMessage, HumanMessage

from app.distillation import clarifications, inference, transform_builder
from app.distillation.agent import build_agent
from app.core.config import settings
from app.core.llm import get_llm
from app.domain import scenario_state
from app.domain.models import (
    ChatMessage,
    ChatRole,
    Scenario,
    ScenarioStatus,
    TableRole,
    ToolTrace,
)
from app.distillation.skill_builder import materialize_skills
from app.domain.storage import store
from app.runtime import executor
from app.core.streaming import ThinkParser, sse, sse_done
from app.distillation.tools import TOOL_REFRESH_MAP

# 蒸馏工具名 → 完成后应推送的状态
_TOOL_STATUS_MAP = {
    "trace_data_links": ScenarioStatus.TRACE_SAMPLED,
    "deduce_relations": ScenarioStatus.RELATIONS_DEDUCED,
    "deduce_flow": ScenarioStatus.FLOW_DEDUCED,
    "generate_skills": ScenarioStatus.SKILLS_GENERATED,
}

_DETERMINISTIC_WORKFLOW_KEYWORDS = {
    "trace": (
        "数据链路追踪",
        "链路追踪",
        "追踪链路",
        "追踪样本",
        "步骤2",
        "步骤二",
        "第二步",
        "trace_data_links",
    ),
    "relations": (
        "推导关联关系",
        "推导表关联",
        "推导关系",
        "推导er",
        "步骤3",
        "步骤三",
        "第三步",
        "deduce_relations",
    ),
    "flow": (
        "推导业务流程",
        "推导流程",
        "步骤4",
        "步骤四",
        "第四步",
        "deduce_flow",
    ),
    "skills": (
        "生成技能",
        "生成技能库",
        "固化技能",
        "步骤5",
        "步骤五",
        "第五步",
        "generate_skills",
    ),
    "metadata": (
        "提取元数据",
        "扫描结构",
        "元数据蓝图",
        "extract_metadata",
    ),
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

    if _should_use_deterministic_workflow(user_message):
        gen = _stream_heuristic(scenario, user_message)
    elif get_llm() is not None:
        gen = _stream_with_agent(scenario, user_message)
    else:
        gen = _stream_heuristic(scenario, user_message)

    try:
        async for frame in gen:
            yield frame
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        message = f"AI 处理出错：{exc}"
        yield sse("error", message=message)
        store.append_message(
            scenario.id,
            ChatMessage(
                id=_new_msg_id(),
                role=ChatRole.ASSISTANT,
                content=f"⚠️ {message}",
            ),
        )
    yield sse_done()


# ===========================================================================
# LLM 路径
# ===========================================================================
async def _stream_with_agent(scenario: Scenario, user_message: str) -> AsyncIterator[str]:
    parser = ThinkParser()
    final_content: list[str] = []
    final_thinking: list[str] = []
    tool_traces: list[ToolTrace] = []
    sent_interaction = False

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
                refresh_targets = TOOL_REFRESH_MAP.get(name)
                if refresh_targets:
                    if isinstance(refresh_targets, str):
                        refresh_targets = [refresh_targets]
                    for resource in refresh_targets:
                        yield sse("refresh", resource=resource)
                if name in _TOOL_STATUS_MAP:
                    # 只在工具明确返回 ✅ 时才推送状态变更，避免失败时误报
                    tool_output_str = str(output)
                    if tool_output_str.lstrip().startswith("✅"):
                        yield sse("status", status=_TOOL_STATUS_MAP[name].value)
                # 关联/流程推导后若有不确定项，主动弹出结构化交互（让前端渲染逐题问答面板）
                if name in ("deduce_relations", "deduce_flow"):
                    sc = store.get(scenario.id)
                    result_obj = None
                    if name == "deduce_relations" and sc:
                        result_obj = sc.relations
                    elif name == "deduce_flow" and sc:
                        result_obj = sc.flow
                    interaction = _step_interaction(name, result_obj)
                    if interaction:
                        yield sse("interaction", interaction=interaction)
                        sent_interaction = True
                if not sent_interaction:
                    fallback_interaction = _text_interaction(name, str(output))
                    if fallback_interaction:
                        yield sse("interaction", interaction=fallback_interaction)
                        sent_interaction = True

        for ev_type, ev_text in parser.flush():
            (final_thinking if ev_type == "thinking" else final_content).append(ev_text)
            yield sse(ev_type, delta=ev_text)

        if not sent_interaction:
            fallback_interaction = _text_interaction(
                "assistant_confirm", "".join(final_content)
            )
            if fallback_interaction:
                yield sse("interaction", interaction=fallback_interaction)
                sent_interaction = True

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


def _step_interaction(tool_name: str, result_obj) -> Optional[dict]:
    """把工具的待确认事项渲染为**逐题**结构化问答面板（对齐 Claude AskUserQuestion）。

    优先用结果对象里的结构化 `clarifications`（每题自带 options/allow_custom/multi_select）；
    没有则把纯字符串 `ambiguous_questions` 先做去重降噪，再生成带推荐项的可选答案。
    这样用户可**逐题**选择或自行描述，而不是面对一整段正文。

    严格要求：用户没回答前，agent **不应**进入下一步。
    """
    if result_obj is None:
        return None

    label = "关联推导" if tool_name == "deduce_relations" else "业务流程推导"
    questions: list[dict] = []

    clarification_items = list(getattr(result_obj, "clarifications", None) or [])
    if clarification_items:
        for i, c in enumerate(clarification_items[:8]):
            questions.append({
                "id": getattr(c, "id", "") or f"{tool_name}_q{i}",
                "question": c.question,
                "options": list(c.options or []),
                "allow_custom": bool(c.allow_custom),
                "multi_select": bool(c.multi_select),
            })
    else:
        raw = list(getattr(result_obj, "ambiguous_questions", None) or [])
        generated = clarifications.build_clarifications(raw, context=tool_name)
        for i, c in enumerate(generated[:8]):
            questions.append({
                "id": getattr(c, "id", "") or f"{tool_name}_q{i}",
                "question": c.question,
                "options": list(c.options or []),
                "allow_custom": bool(c.allow_custom),
                "multi_select": bool(c.multi_select),
            })

    if not questions:
        return None

    return {
        "type": "confirm",
        "title": f"🧩 {label}有 {len(questions)} 项待你确认，逐题选择或补充后我再继续：",
        "context": f"{tool_name}_confirm",
        "questions": questions,
    }


def _text_interaction(context: str, text: str) -> Optional[dict]:
    clarification_items = clarifications.build_clarifications_from_text(
        text, context=context
    )
    if not clarification_items:
        return None
    questions = []
    for i, c in enumerate(clarification_items[:6]):
        questions.append({
            "id": getattr(c, "id", "") or f"{context}_q{i}",
            "question": c.question,
            "options": list(c.options or []),
            "allow_custom": bool(c.allow_custom),
            "multi_select": bool(c.multi_select),
        })
    return {
        "type": "confirm",
        "title": f"需要你确认 {len(questions)} 项后再继续",
        "context": f"{context}_confirm",
        "questions": questions,
    }


# ===========================================================================
# 启发式降级路径
# ===========================================================================
_INTENT_KEYWORDS = {
    "trace":     ("数据链路追踪", "链路追踪", "追踪链路", "追踪样本", "因果链", "trace", "trace_data_links"),
    "relations": ("关联", "关系", "ER", "推导er", "字段语义", "字段含义", "推导关联", "deduce_relations"),
    "flow":      ("流程", "推导流程", "业务流程", "节点", "管线", "deduce_flow"),
    "skills":    ("技能", "skill", "技能库", "固化", "生成技能", "generate_skills"),
    "execute":   ("执行", "复刻", "产出结果", "跑一下", "运行", "校验", "验证", "对照", "比对"),
    "metadata":  ("元数据", "结构", "扫描", "蓝图", "概览", "extract_metadata"),
}


def _detect_intent(message: str) -> str:
    low = message.lower()
    for intent, kws in _INTENT_KEYWORDS.items():
        if any(kw in message or kw in low for kw in kws):
            return intent
    return "general"


def _compact_workflow_text(text: str) -> str:
    return "".join(
        ch
        for ch in text.lower()
        if not ch.isspace() and ch not in "，。、“”‘’：:；;,./?？!！-—_()（）[]【】"
    )


def _should_use_deterministic_workflow(message: str) -> bool:
    """Route explicit workflow step commands to backend execution, not LLM choice."""
    text = (message or "").strip()
    low = text.lower()
    compact = _compact_workflow_text(text)
    if not text:
        return False
    for kws in _DETERMINISTIC_WORKFLOW_KEYWORDS.values():
        for kw in kws:
            kw_low = kw.lower()
            if kw in text or kw_low in low or _compact_workflow_text(kw) in compact:
                return True
    return False


async def _heartbeat_until(task: asyncio.Task, label: str) -> AsyncIterator[str]:
    start = time.monotonic()
    interval = min(10, max(3, int(settings.verify_heartbeat_interval)))
    timeout = max(interval, int(settings.verify_turn_timeout))

    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                task.cancel()
                raise TimeoutError(
                    f"{label}超过 {timeout} 秒仍未完成，已停止等待。请检查 LLM/网络或缩小数据范围后重试。"
                )
            yield sse(
                "heartbeat",
                elapsed=int(elapsed),
                message=f"{label}仍在执行，已 {int(elapsed)} 秒；不是卡死，正在等待模型或数据处理返回。",
            )


async def _stream_heuristic(scenario: Scenario, user_message: str) -> AsyncIterator[str]:
    intent = _detect_intent(user_message)
    content_buf: list[str] = []

    def emit_content(text: str):
        content_buf.append(text)
        return sse("content", delta=text)

    if not scenario.tables_meta and intent != "general":
        yield emit_content("当前业务场景尚未上传业务表，请先上传业务表/知识表（可选）/历史结果表（上传时选择角色）。")
        _persist_assistant(scenario, content_buf)
        return

    if intent == "trace":
        yield sse("thinking", delta="以结果表样本为锚点追踪业务表/知识表链路……")
        yield sse("tool_call", name="trace_data_links", args="追踪驱动采样：结果表锚点→业务/知识表对应行")
        from app.distillation import trace_sampling  # noqa: PLC0415
        from app.domain import validators  # noqa: PLC0415

        def run_trace():
            traced = trace_sampling.trace_sampling(scenario)
            return traced, validators.validate_trace_connectivity(traced)

        task = asyncio.create_task(asyncio.to_thread(run_trace))
        async for frame in _heartbeat_until(task, "数据链路追踪"):
            yield frame
        report, check = task.result()
        report["connectivity_check"] = check.to_dict()
        scenario.trace_chain = report
        scenario_state.invalidate_after_trace(scenario)
        store.save(scenario)
        result_summary = f"{check.message}；{report.get('trace_summary', '')}"
        yield sse("tool_result", name="trace_data_links", result=result_summary)
        yield sse("refresh", resource="trace")
        yield sse("status", status=scenario.status.value)
        traced_lines = []
        for tbl, info in (report.get("trace_map") or {}).items():
            by = info.get("matched_by", "")
            rows = info.get("matched_rows", [])
            if by and by != "random":
                traced_lines.append(f"- {tbl}：通过「{by}」追踪到 {len(rows)} 行，置信度 {info.get('trace_confidence', '?')}")
            else:
                traced_lines.append(f"- {tbl}：未追踪到稳定因果行，不作为后续推导样本")
        yield emit_content(
            f"已完成数据链路追踪。\n\n{result_summary}\n\n"
            + ("\n".join(traced_lines) or "未形成可展示链路。")
            + "\n\n请在「表格 & 字段」里展开每张表的追踪链路样本核对；确认无误后再说「推导关联关系」。"
        )

    elif intent == "relations":
        if not scenario.trace_chain:
            yield emit_content(
                "⚠️ 请先执行「数据链路追踪」。关联推导需要使用以结果样本为锚点追踪到的链路数据，"
                "否则容易退化成字段名/全量值域猜测，既慢也容易问出无意义问题。"
            )
            _persist_assistant(scenario, content_buf)
            return
        yield sse("thinking", delta="推导字段业务语义 + 表关联……")
        yield sse("tool_call", name="deduce_relations", args="启发式：字段语义+关联推导")
        task = asyncio.create_task(asyncio.to_thread(inference.infer_relations, scenario))
        async for frame in _heartbeat_until(task, "推导关联关系"):
            yield frame
        result = task.result()
        scenario.relations = result
        scenario_state.invalidate_after_relations(scenario)
        try:
            from app.distillation import trace_sampling  # noqa: PLC0415
            refreshed_trace = trace_sampling.trace_sampling(scenario)
            scenario.trace_chain = refreshed_trace
            scenario.relations.trace_chain = refreshed_trace
        except Exception:  # noqa: BLE001
            pass
        domain = transform_builder.build_domain_knowledge(scenario)
        scenario.domain_knowledge = domain
        store.save(scenario)
        yield sse("tool_result", name="deduce_relations", result=result.summary)
        yield sse("refresh", resource="trace")
        yield sse("refresh", resource="relations")
        yield sse("status", status=scenario.status.value)
        interaction = _step_interaction("deduce_relations", result)
        if interaction:
            yield sse("interaction", interaction=interaction)
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
            yield sse("thinking", delta="基于（表角色+字段语义+关联+知识结构）反推业务流程节点……")
            yield sse("tool_call", name="deduce_flow", args="启发式：业务流程推导")
            task = asyncio.create_task(asyncio.to_thread(inference.infer_flow, scenario))
            async for frame in _heartbeat_until(task, "推导业务流程"):
                yield frame
            result = task.result()
            if not result.flow_steps:
                yield sse("tool_result", name="deduce_flow", result="未生成流程节点")
                yield emit_content(
                    "🛑 业务流程推导没有生成任何流程节点，当前不会保存为已推导流程。\n\n"
                    f"{result.summary or '请先检查表角色、追踪链路样本和关联关系后再重试。'}"
                )
                _persist_assistant(scenario, content_buf)
                return
            scenario.flow = result
            scenario_state.invalidate_after_flow(scenario)
            domain = transform_builder.build_domain_knowledge(scenario)
            scenario.domain_knowledge = domain
            scenario.outputs = transform_builder.build_outputs(scenario, domain)
            store.save(scenario)
            yield sse("tool_result", name="deduce_flow", result=result.summary)
            yield sse("refresh", resource="flow")
            yield sse("status", status=scenario.status.value)
            interaction = _step_interaction("deduce_flow", result)
            if interaction:
                yield sse("interaction", interaction=interaction)
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
                task = asyncio.create_task(asyncio.to_thread(materialize_skills, scenario))
                async for frame in _heartbeat_until(task, "生成技能"):
                    yield frame
                materialized = task.result()
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
                "蒸馏通道到此结束。发布配置可在「技能」页查看；执行/验证可到「Agent 平台」。"
            )

    elif intent == "execute":
        if not scenario.skills:
            yield emit_content("⚠️ 尚未生成技能包。请先「生成技能库」。")
        else:
            yield emit_content(
                "✅ 技能包已生成，蒸馏完成。\n\n"
                "发布配置请在当前场景的**技能**页查看；执行/验证请切换到**Agent 平台**（/sandbox），\n"
                "它默认是普通 Agent，只有安装并勾选 Skill/MCP 后才会调用业务能力。\n\n"
                f"直接访问：[Agent 平台 → {scenario.name}](/sandbox)"
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
            "（无 LLM 模式）6 步工作流：\n"
            "① 上传业务表 + 知识表(可选) + 历史结果表（上传时选角色）\n"
            "② 对我说「数据链路追踪」（以结果样本为锚点追业务/知识表）\n"
            "③ 对我说「推导关联关系」（含字段语义）\n"
            "④ 对我说「推导业务流程」（节点带能力描述 + 知识结构映射）\n"
            "⑤ 对我说「生成技能库」（按节点固化）\n"
            "⑥ 到「技能」页查看发布配置，或切换到「Agent 平台」执行和校验"
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
