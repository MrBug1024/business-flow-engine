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

from time import time

from . import executor, inference, process, rule_parser, sql_builder
from .agent import build_agent
from .llm import get_llm
from .models import (
    ChatMessage,
    ChatRole,
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
    "discover_business_process": ScenarioStatus.PROCESS_DRAFTED,
    "deduce_relations": ScenarioStatus.RELATIONS_DEDUCED,
    "parse_rules": ScenarioStatus.RULES_PARSED,
    "deduce_flow": ScenarioStatus.FLOW_DEDUCED,
    "execute_and_compare": ScenarioStatus.VALIDATED,
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


# ===========================================================================
# Phase 0：业务流程审批（结构化交互 + 审批 Gate）
# ===========================================================================
_APPROVE_KEYWORDS = ("批准", "通过", "同意", "确认无误", "没问题", "正确", "approve", "确认")
_REJECT_KEYWORDS = ("打回", "不对", "有误", "修改", "重新", "不准确", "reject")


def _phase0_interaction() -> dict:
    """Phase 0 审批的结构化交互块（规范 Section 5）。"""
    return {
        "type": "confirm",
        "question": "以上业务流程理解是否准确？批准后才能进入后续推导阶段。",
        "options": ["a) 准确，批准并继续", "b) 需要修改（我来补充说明）"],
        "allow_custom": True,
        "context": "phase0_approval",
    }


def _detect_phase0_approval(message: str) -> bool:
    """是否为「显式批准」。出现否定/修改类词则不算批准（交由常规路径处理重新梳理）。"""
    low = message.lower()
    if any(k in message or k in low for k in _REJECT_KEYWORDS):
        return False
    # 含「梳理/生成」等再加工意图的，不当作批准（应去重新生成文档）
    if any(k in message for k in ("梳理", "生成", "重做")):
        return False
    return any(k in message or k in low for k in _APPROVE_KEYWORDS)


async def _stream_phase0_approval(scenario: Scenario) -> AsyncIterator[str]:
    """用户显式批准业务流程文档：翻转 Gate 并放行后续阶段（确定性，无 AI）。"""
    bp = scenario.business_process
    bp.approved = True
    bp.approved_at = time()
    scenario.business_process = bp
    if scenario.status in (ScenarioStatus.TABLES_UPLOADED, ScenarioStatus.PROCESS_DRAFTED,
                           ScenarioStatus.CREATED):
        scenario.status = ScenarioStatus.PROCESS_APPROVED
    store.save(scenario)
    note = ("✅ 业务流程文档已批准（Phase 0 完成）。后续阶段已放行：\n"
            "下一步可说「推导关联关系」进入 Phase 1（Schema 与关系提取）。")
    yield sse("status", status=scenario.status.value)
    yield sse("refresh", resource="business_process")
    yield sse("content", delta=note)
    _persist_assistant(scenario, [note])


async def stream_chat(scenario: Scenario, user_message: str) -> AsyncIterator[str]:
    """对外的统一入口：产出 SSE 文本帧。"""
    # 先持久化用户消息
    store.append_message(
        scenario.id,
        ChatMessage(id=_new_msg_id(), role=ChatRole.USER, content=user_message),
    )

    # Phase 0 审批是「用户的决定」，由确定性逻辑处理，不交给 AI 自行批准。
    # 仅在「已有待批准文档」且消息为「显式批准」时拦截；其余（重新梳理/修改意见）走常规路径。
    fresh = store.get(scenario.id) or scenario
    bp = fresh.business_process
    if bp is not None and not bp.approved and _detect_phase0_approval(user_message):
        async for frame in _stream_phase0_approval(fresh):
            yield frame
        yield sse_done()
        return

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
                # Phase 0：流程文档生成后，以结构化交互向用户请求批准（规范 Section 5）
                if name == "discover_business_process":
                    yield sse("interaction", interaction=_phase0_interaction())

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

    依赖链：技能 ← 规则库 ←（关联关系）。按链条逐级补齐。
    产出 (SSE帧, 追加到回答的提示文本或None) 二元组。
    """
    intent = _detect_intent(user_message)
    if intent not in {"relations", "rules", "skills"}:
        return
    sc = store.get(scenario_id)
    if sc is None or not sc.tables_meta:
        return
    # Phase 0 Gate：未批准业务流程文档前，不自动补齐后续阶段产物。
    if sc.business_process is None or not sc.business_process.approved:
        return

    need_rules = intent in {"rules", "skills"}
    need_skills = intent == "skills"

    # 1) 关联关系
    if intent == "relations" and not sc.relations and "deduce_relations" not in called:
        result = inference.infer_relations(sc)
        sc.relations = result
        if sc.status == ScenarioStatus.TABLES_UPLOADED:
            sc.status = ScenarioStatus.RELATIONS_DEDUCED
        store.save(sc)
        yield sse("tool_call", name="deduce_relations", args="系统自动补齐"), None
        yield sse("tool_result", name="deduce_relations", result=result.summary), None
        yield sse("refresh", resource="relations"), None
        yield sse("status", status=sc.status.value), None
        yield sse("content", delta="\n\n✅ 已自动完成并保存关联关系推导。"), "\n\n✅ 已自动完成并保存关联关系推导。"

    # 2) 规则库
    if need_rules and not (sc.rule_library and sc.rule_library.templates) and "parse_rules" not in called:
        sc = store.get(scenario_id)
        rule_table = rule_parser.find_rule_table(sc)
        if rule_table is not None:
            library = rule_parser.parse_rule_table(rule_table)
            sc.rule_library = library
            domain = sql_builder.build_domain_knowledge(sc)
            sc.domain_knowledge = domain
            sql_builder.build_rule_sql_library(sc, domain)
            if sc.status in (ScenarioStatus.TABLES_UPLOADED, ScenarioStatus.RELATIONS_DEDUCED,
                             ScenarioStatus.PROCESS_APPROVED):
                sc.status = ScenarioStatus.RULES_PARSED
            store.save(sc)
            note = f"\n\n✅ 已解析规则库：{library.summary}"
            yield sse("tool_call", name="parse_rules", args="系统自动补齐"), None
            yield sse("tool_result", name="parse_rules", result=library.summary), None
            yield sse("refresh", resource="rules"), None
            yield sse("status", status=sc.status.value), None
            yield sse("content", delta=note), note

    # 3) 技能库
    if need_skills and "generate_skills" not in called:
        sc = store.get(scenario_id)
        if sc.rule_library and sc.rule_library.templates and not sc.skills:
            materialized = materialize_skills(sc)
            sc.skills = materialized
            sc.status = ScenarioStatus.SKILLS_GENERATED
            store.save(sc)
            note = f"\n\n✅ 已生成并保存 {len(materialized)} 个技能（参数化审核技能）到业务场景目录。"
            yield sse("tool_call", name="generate_skills", args="系统自动补齐"), None
            yield sse("tool_result", name="generate_skills", result=f"生成 {len(materialized)} 个技能"), None
            yield sse("refresh", resource="skills"), None
            yield sse("status", status=sc.status.value), None
            yield sse("content", delta=note), note


# ===========================================================================
# 启发式降级路径（无 LLM）
# ===========================================================================
_INTENT_KEYWORDS = {
    "discovery": ("业务流程", "流程梳理", "梳理", "业务背景", "流程发现", "流程文档",
                  "phase0", "phase 0", "业务理解"),
    "skills": ("技能", "skill", "技能库", "固化", "参数化"),
    "audit": ("执行审核", "审核", "校验", "验证", "execute", "重复收费", "对照", "比对"),
    "rules": ("规则", "规则库", "解析规则", "违规类型", "审核类型", "审核能力"),
    "relations": ("关联", "关系", "推导关系", "表关系", "er"),
    "metadata": ("元数据", "结构", "扫描", "蓝图", "概览"),
    "flow": ("流程", "推导流程", "业务流程"),
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
        yield emit_content("当前业务场景尚未上传业务表，请先上传业务表、规则表与历史结果表再进行推导。")
        _persist_assistant(scenario, content_buf)
        return

    # ---- Phase 0：业务流程发现 ----
    if intent == "discovery":
        yield sse("thinking", delta="梳理业务流程：识别输入表/规则表/结果表，组织处理步骤与流程图……")
        yield sse("tool_call", name="discover_business_process", args="Phase 0 业务流程梳理")
        bp = process.discover_process(scenario)
        store.write_business_process(scenario.id, bp.markdown)
        scenario.business_process = bp
        if scenario.status in (ScenarioStatus.CREATED, ScenarioStatus.TABLES_UPLOADED):
            scenario.status = ScenarioStatus.PROCESS_DRAFTED
        store.save(scenario)
        yield sse("tool_result", name="discover_business_process",
                  result="已生成 business_process.md（状态=待审批）")
        yield sse("refresh", resource="business_process")
        yield sse("status", status=scenario.status.value)
        yield emit_content(bp.markdown)
        yield sse("interaction", interaction=_phase0_interaction())
        _persist_assistant(scenario, content_buf)
        return

    # ---- Phase 0 Gate：后续阶段需先批准业务流程文档 ----
    if intent in {"relations", "rules", "metadata", "audit", "skills"}:
        bp = scenario.business_process
        if bp is None or not bp.approved:
            tip = ("⛔ 请先完成 Phase 0：对我说「梳理业务流程」生成业务流程文档，"
                   "确认无误后批准，方可进入后续阶段。"
                   if bp is None else
                   "⛔ 业务流程文档尚未批准。请在「业务流程」标签确认无误后批准（或回复「批准」），再继续。")
            yield emit_content(tip)
            _persist_assistant(scenario, content_buf)
            return

    if intent == "relations":
        yield sse("thinking", delta="比对各表字段名语义、数据类型与样本值重叠率，识别关联键（ER 模型）……")
        yield sse("tool_call", name="deduce_relations", args="启发式关联推导")
        result: RelationResult = inference.infer_relations(scenario)
        scenario.relations = result
        if scenario.status == ScenarioStatus.TABLES_UPLOADED:
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
        text = f"已完成关联关系（ER）推导。\n\n{result.summary}\n\n{lines or '（未发现明显关联）'}"
        if result.ambiguous_questions:
            text += "\n\n需确认：\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(result.ambiguous_questions))
        text += "\n\n接着可说「解析规则库」。"
        yield emit_content(text)

    elif intent in {"rules", "metadata"}:
        rule_table = rule_parser.find_rule_table(scenario)
        if rule_table is None:
            yield emit_content("未识别到规则表。请上传含「违规类型/规则情形/政策依据/示例」等列的规则表（表名含「规则/清单」）。")
        else:
            yield sse("thinking", delta="解析规则表为结构化规则模板库：逐条抽取违规类型、关键词、逻辑描述、示例……")
            yield sse("tool_call", name="parse_rules", args="解析规则库")
            library = rule_parser.parse_rule_table(rule_table)
            scenario.rule_library = library
            domain = sql_builder.build_domain_knowledge(scenario)
            scenario.domain_knowledge = domain
            sql_builder.build_rule_sql_library(scenario, domain)
            if scenario.status in (ScenarioStatus.TABLES_UPLOADED, ScenarioStatus.RELATIONS_DEDUCED,
                                   ScenarioStatus.PROCESS_APPROVED):
                scenario.status = ScenarioStatus.RULES_PARSED
            store.save(scenario)
            yield sse("tool_result", name="parse_rules", result=library.summary)
            yield sse("refresh", resource="rules")
            yield sse("status", status=scenario.status.value)
            vtypes = library.violation_types
            preview = "\n".join(f"- {vt}" for vt in vtypes[:15])
            more = f"\n…等共 {len(vtypes)} 种" if len(vtypes) > 15 else ""
            yield emit_content(
                f"已解析规则库。{library.summary}\n\n可执行的违规类型（审核能力）：\n{preview}{more}\n\n"
                "接着可说「执行审核（重复收费）」对照历史结果校验，或「生成技能库」固化为参数化审核技能。"
            )

    elif intent == "audit":
        if not (scenario.rule_library and scenario.rule_library.templates):
            yield emit_content("规则库尚未解析，无法执行审核。请先说「解析规则库」。")
        else:
            vt = _pick_violation_type(scenario, user_message)
            tmpls = rule_parser.violation_type_groups(scenario.rule_library).get(vt, [])
            yield sse("thinking", delta=f"在完整原始数据上执行「{vt}」审核逻辑，并与历史结果表对照……")
            yield sse("tool_call", name="execute_and_compare", args=f"violation_type={vt}")
            report = executor.execute_and_compare(scenario, tmpls[0]) if tmpls else None
            if report is None:
                yield emit_content(f"未找到违规类型「{vt}」。")
            else:
                for t in tmpls:
                    t.match_rate = report.match_rate
                    if report.passed:
                        t.status = "verified"
                scenario.validations = [v for v in scenario.validations
                                        if v.violation_type != vt] + [report]
                if report.passed and scenario.status in (ScenarioStatus.RULES_PARSED,
                                                          ScenarioStatus.RELATIONS_DEDUCED,
                                                          ScenarioStatus.FLOW_DEDUCED):
                    scenario.status = ScenarioStatus.VALIDATED
                store.save(scenario)
                yield sse("tool_result", name="execute_and_compare", result=report.message)
                yield sse("refresh", resource="validations")
                yield sse("status", status=scenario.status.value)
                yield emit_content(
                    f"已执行「{vt}」审核并与历史结果对照：\n\n{report.message}\n\n"
                    "（启发式模式用通用重复检测兜底；配置 LLM 后可由 AI 为每条规则生成精确逻辑。）\n"
                    "确认后可说「生成技能库」固化为参数化审核技能。"
                )

    elif intent == "skills":
        if not (scenario.rule_library and scenario.rule_library.templates):
            yield emit_content("规则库尚未解析，无法生成技能。请先说「解析规则库」。")
        else:
            yield sse("thinking", delta="将规则库固化为参数化审核技能（list_audit_types / execute_audit）……")
            yield sse("tool_call", name="generate_skills", args="生成技能库")
            materialized = materialize_skills(scenario)
            scenario.skills = materialized
            scenario.status = ScenarioStatus.SKILLS_GENERATED
            store.save(scenario)
            yield sse("tool_result", name="generate_skills", result=f"生成 {len(materialized)} 个技能")
            yield sse("refresh", resource="skills")
            yield sse("status", status=scenario.status.value)
            names = "\n".join(f"- {s.name}（{s.operation}）" for s in materialized)
            yield emit_content(
                f"已生成 {len(materialized)} 个技能：\n\n{names}\n\n"
                "核心是参数化审核技能：`list_audit_types()` 列出全部违规类型，"
                "`execute_audit(violation_type, data_sources)` 对新数据执行任意类型审核。"
            )
    else:
        n_types = len(scenario.rule_library.violation_types) if scenario.rule_library else 0
        yield emit_content(
            f"收到：「{user_message}」。\n\n当前业务场景「{scenario.name}」状态：{scenario.status.value}"
            + (f"，规则库含 {n_types} 种违规类型。" if n_types else "。") + "\n\n"
            "（当前为无 LLM 的启发式模式）建议流程：\n"
            "① 上传业务表 + 规则表 + 历史结果表\n② 说「推导关联关系」\n③ 说「解析规则库」\n"
            "④ 说「执行审核（重复收费）」校验\n⑤ 说「生成技能库」固化为参数化审核能力"
        )

    _persist_assistant(scenario, content_buf)


def _pick_violation_type(scenario: Scenario, message: str) -> str:
    """从用户消息或规则库中选定一个违规类型（优先消息中提到的、其次有历史结果表的）。"""
    lib = scenario.rule_library
    vtypes = lib.violation_types if lib else []
    for vt in vtypes:
        if vt and vt in message:
            return vt
    # 选一个存在历史结果表的类型（如「重复收费」）
    for vt in vtypes:
        if executor.find_historical_table(scenario, vt) is not None:
            return vt
    return vtypes[0] if vtypes else ""


def _persist_assistant(scenario: Scenario, content_buf: list[str]) -> None:
    store.append_message(
        scenario.id,
        ChatMessage(
            id=_new_msg_id(),
            role=ChatRole.ASSISTANT,
            content="".join(content_buf).strip(),
        ),
    )
