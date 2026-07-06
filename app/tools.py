"""蒸馏工具集（v1.0.6）。

蒸馏阶段工具（不包含执行/验证工具，那些属于验证通道）：
    extract_metadata     读取元数据蓝图
    get_field_sample     字段去重样本
    set_table_role       修正表角色
    trace_data_links     数据链路追踪
    deduce_relations     推导表关联 + 字段语义
    correct_relation     用户明确修正某条关联时必须调用，强制固化 + 刷新因果链样本
    deduce_flow          推导业务流程节点
    describe_flow_step   查看节点详情
    refine_flow_step     细化节点参数/SQL
    generate_skills      固化技能包
    list_skills          查看已落盘技能
    read_skill           读取某技能内容
    get_trace_sample     追踪驱动采样

v1.0.6：移除 list_patterns / suggest_patterns_for_table——不再引导 AI 从一个固定的
处理模式目录里挑选算子。节点逻辑（含知识表驱动节点的执行 SQL）现在由 AI 依据这个
场景的真实 schema 直接写 SQL、并在真实数据上验证，不是从模板库里选一个形状套上去。
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import StructuredTool

from . import inference, metadata, scenario_state, strategies, table_io, transform_builder
from . import validators as _val
from . import trace_sampling as _ts
from .models import Scenario, ScenarioStatus, TableRole
from .skill_builder import materialize_skills
from .storage import store

# 蒸馏工具调用产生的「副作用资源」标记，供对话服务推送刷新事件
TOOL_REFRESH_MAP = {
    "set_table_role": "tables",
    "trace_data_links": "trace",
    "deduce_relations": "relations",
    "correct_relation": "relations",
    "deduce_flow": "flow",
    "refine_flow_step": "flow",
    "generate_skills": "skills",
}


def _require(scenario_id: str) -> Scenario:
    scenario = store.get(scenario_id)
    if scenario is None:
        raise ValueError(f"业务场景 {scenario_id} 不存在")
    return scenario


def _refresh_domain_and_outputs(scenario: Scenario) -> None:
    """刷新数据字典与产出规格（确定性，幂等）。"""
    domain = transform_builder.build_domain_knowledge(scenario)
    scenario.domain_knowledge = domain
    if scenario.flow or any(t.role == TableRole.RESULT.value for t in scenario.tables_meta):
        scenario.outputs = transform_builder.build_outputs(scenario, domain)


def build_tools(scenario_id: str) -> list[StructuredTool]:

    # ---------------------------------------------------------------- 元数据
    def extract_metadata() -> str:
        """读取业务场景的「元数据蓝图」：表结构、字段语义、表角色、关联、知识结构、流程节点。
        这是你了解数据的**唯一蓝图**，绝不逐行翻看全量数据。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "尚未上传任何业务表。请提示用户先上传表格并选择角色（业务/知识/结果）。"
        return metadata.build_metadata_report(scenario)

    def get_field_sample(table_name: str, field_name: str) -> str:
        """获取某表某字段的若干「去重样本值」。参数：table_name 表名；field_name 字段名。"""
        scenario = _require(scenario_id)
        meta = next((t for t in scenario.tables_meta if t.table_name == table_name), None)
        if meta is None:
            names = "、".join(t.table_name for t in scenario.tables_meta) or "（无）"
            return f"未找到表「{table_name}」。可用表：{names}"
        try:
            values = list(table_io.column_value_set(meta.file_path, field_name))[:15]
        except Exception as exc:  # noqa: BLE001
            return f"读取失败：{exc}"
        if not values:
            cols = "、".join(c.name for c in meta.columns)
            return f"字段「{field_name}」无样本或不存在。该表字段：{cols}"
        return f"表「{table_name}」.字段「{field_name}」去重样本（≤15）：{values}"

    def set_table_role(table_name: str, role: str) -> str:
        """修正某张表的角色（一般用户在上传时已选；本工具用于事后修正）。
        参数：table_name；role ∈ input / knowledge / rule / result。
        注：knowledge 是 v1.0.4 的新术语，rule 为向后兼容的旧称，两者等效。"""
        scenario = _require(scenario_id)
        role = (role or "").strip().lower()
        valid_roles = (
            TableRole.INPUT.value, TableRole.KNOWLEDGE.value,
            TableRole.RULE.value, TableRole.RESULT.value,
        )
        if role not in valid_roles:
            return "role 取值须为 input / knowledge / rule / result 之一。"
        meta = next((t for t in scenario.tables_meta if t.table_name == table_name), None)
        if meta is None:
            names = "、".join(t.table_name for t in scenario.tables_meta) or "（无）"
            return f"未找到表「{table_name}」。可用表：{names}"
        changed = meta.role != role
        meta.role = role
        meta.role_confirmed = True
        if changed:
            scenario_state.invalidate_from_tables(scenario)
        _refresh_domain_and_outputs(scenario)
        store.save(scenario)
        return f"已将表「{table_name}」角色修正为「{role}」。"

    # ---------------------------------------------------------------- 步骤 2：数据链路追踪
    def trace_data_links(result_table_name: str = "") -> str:
        """【步骤 2】执行数据链路追踪。

        上传阶段只解析表结构；本步骤才真正以结果表某条记录为锚点，追踪各业务表/知识表
        的对应样本行，并保存为后续推导关联和业务流程使用的链路样本。可选参数
        result_table_name 指定追踪入口结果表，不指定时自动选择 role=result 的表。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "🛑 当前场景未上传任何表。"
        trace_report = _ts.trace_sampling(
            scenario,
            result_table_name=result_table_name or None,
        )
        val = _val.validate_trace_connectivity(trace_report)
        scenario.trace_chain = trace_report
        scenario_state.invalidate_after_trace(scenario)
        store.save(scenario)

        level_emoji = {"pass": "✅", "warning": "⚠️", "fail": "❌"}.get(val.level, "")
        traced = []
        for tbl, info in (trace_report.get("trace_map") or {}).items():
            by = info.get("matched_by", "")
            rows = info.get("matched_rows", [])
            if by and by != "random":
                traced.append(f"{tbl}({by}, {len(rows)}行)")
        traced_text = "；".join(traced) or "未追踪到稳定链路"
        return (
            f"✅ 数据链路追踪完成。\n"
            f"{level_emoji} {val.message}\n"
            f"追踪摘要：{trace_report.get('trace_summary', '')}\n"
            f"链路样本：{traced_text}\n\n"
            "🛑 STOP：本步骤完成。请把追踪摘要和有问题的表呈现给用户，"
            "等用户确认或修正后，再让用户明确说『推导关联关系』。"
        )

    # ---------------------------------------------------------------- 步骤 3：关联 + 字段语义
    def deduce_relations() -> str:
        """【步骤 3】推导表关联（ER）+ 字段业务语义。
        必须先完成 trace_data_links；字段语义在此步骤一并完成，作为推流程、推知识结构的基础。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "尚未上传业务表。"
        if not scenario.trace_chain:
            return (
                "🛑 STOP：尚未完成数据链路追踪（步骤 2）。"
                "请提示用户先说『数据链路追踪』，不要直接推导关联关系。"
            )
        result = inference.infer_relations(scenario)
        scenario.relations = result
        scenario_state.invalidate_after_relations(scenario)
        _refresh_domain_and_outputs(scenario)
        store.save(scenario)
        lines = "；".join(
            f"{r.from_table}.{r.from_column}→{r.to_table}.{r.to_column}({r.confidence:.0%})"
            for r in result.relations
        )
        n_sem = sum(len(v) for v in (result.field_semantics or {}).values())
        tail = (
            "\n\n🛑 STOP：本步骤完成。请把上述结果呈现给用户，**绝不**自动调用 deduce_flow。"
            "等用户明确说『推导业务流程』再继续。"
        )
        if result.ambiguous_questions:
            tail = (
                "\n\n🛑 STOP（重要）：有待确认事项，**必须**先让用户回答，再做任何后续推导。"
                "\n待确认：\n- " + "\n- ".join(result.ambiguous_questions)
                + tail
            )
        return (f"✅ 已推导 {len(result.relations)} 条关联 + 为 {n_sem} 个字段标注语义。"
                f"\n{result.summary}\n{lines}{tail}")

    # ---------------------------------------------------------------- 人工修正关联关系
    def correct_relation(
        from_table: str, from_column: str, to_table: str, to_column: str,
        from_columns: str = "", to_columns: str = "",
    ) -> str:
        """【硬性规则：用户对某条关联提出明确修正意见时必须调用，不能只在文字里回复"已采纳"】

        用户可能说"这条关联不对，应该是A表的X字段对应B表的Y字段"，或者更具体地指出
        "B表第N行才是对的"——不管哪种说法，只要用户给出了明确的字段对应关系，就调用
        本工具，把它强制固化为人工确认的关联，并立刻用真实数据重新验证/生成因果链样本。
        系统、启发式、LLM 都可能出错，但用户明确指认过的关联必须无条件采纳，不能置之不理。

        参数：
            from_table/from_column   关联左侧的表名和字段名
            to_table/to_column       关联右侧的表名和字段名
            from_columns/to_columns  可选，逗号分隔——若用户说的是"要同时用两个字段才能
                                      唯一确定对应关系"（复合键），把完整字段列表填在这里，
                                      两侧**按位置一一对应**（第1列对第1列、第2列对第2列），
                                      比如 from_columns="违规类型,违规说明"、
                                      to_columns="违规类型,国家问题清单"，此时
                                      from_column/to_column 填复合键中的第一个即可。
                                      追踪采样会把复合键的**所有列同时相等**作为匹配条件
                                      （AND），不是只用第一列。

        调用后会：① 把这条关联标记为人工确认（其后 deduce_relations 重新推导也不会
        覆盖它）；② 强制按这条关联在真实数据里重新搜一遍，更新保存的追踪链样本——
        之前如果链路样本是错的，这一步之后会被替换成基于用户指定关联键追出来的真链路；
        ③ deduce_flow 会直接复用这份修正后的链路，不会重新瞎猜。
        """
        scenario = _require(scenario_id)
        known_tables = {t.table_name for t in scenario.tables_meta}
        if from_table not in known_tables or to_table not in known_tables:
            names = "、".join(known_tables)
            return f"❌ 表名不存在。可用表：{names}"

        from_cols = [c.strip() for c in from_columns.split(",") if c.strip()] or None
        to_cols = [c.strip() for c in to_columns.split(",") if c.strip()] or None

        if scenario.relations is None:
            from .models import RelationResult
            scenario.relations = RelationResult()

        relation = _val.upsert_confirmed_relation(
            scenario.relations, from_table, from_column, to_table, to_column,
            from_columns=from_cols, to_columns=to_cols,
        )

        # 强制用这条人工确认的关联重新搜一遍真实数据，刷新保存的因果链样本
        try:
            scenario.trace_chain = _ts.trace_sampling(scenario)
            scenario.relations.trace_chain = scenario.trace_chain
            refresh_note = "已用这条关联在真实数据中重新生成因果链样本。"
        except Exception as exc:  # noqa: BLE001
            refresh_note = f"⚠️ 因果链刷新失败（{exc}），关联本身已保存为人工确认。"

        scenario_state.invalidate_after_relations(scenario)
        store.save(scenario)
        cols_note = (
            f"（复合键：{relation.from_columns} ↔ {relation.to_columns}）"
            if len(relation.from_columns) > 1 else ""
        )
        return (
            f"✅ 已采纳您的修正：{from_table}.{from_column} → {to_table}.{to_column}"
            f"{cols_note}，已标记为人工确认，后续推导不会再覆盖它。{refresh_note}"
        )

    # ---------------------------------------------------------------- 步骤 4：业务流程（含知识结构）
    def deduce_flow() -> str:
        """【步骤 4】推导业务流程节点链。

        每个节点带「该做什么/能做什么/数据怎么变化/模板算子」描述。**若有知识表**，同时蒸馏出
        知识结构映射（dispatch key、条目编号、条件列等），知识条目在运行时按行迭代。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.relations:
            return ("🛑 STOP：尚未完成关联+字段语义推导（步骤 3）。"
                    "请提示用户先说『推导关联关系』，**不要**继续做后续推导。")
        result = inference.infer_flow(scenario)
        if not result.flow_steps:
            return (
                "🛑 STOP：业务流程推导未生成任何流程节点，未保存为已推导流程。"
                f"\n原因：{result.summary or '模型没有返回可用节点，启发式兜底也未能构造流程。'}"
                "\n请先检查表角色、追踪链路样本和关联关系；必要时让用户修正表角色或关联键后再重试。"
            )
        scenario.flow = result
        scenario_state.invalidate_after_flow(scenario)
        _refresh_domain_and_outputs(scenario)
        store.save(scenario)
        head = "\n".join(
            f"  · 步骤{s.step_id} {s.step_name}（{s.template_kind or s.operation}）→ {s.capability[:40]}"
            for s in result.flow_steps
        )
        rs_line = ""
        if result.knowledge_schema:
            rs_line = f"\n知识结构映射：{result.knowledge_schema.summary}"
        elif result.rule_schema:
            rs_line = f"\n知识结构映射：{result.rule_schema.summary}"
        tail = (
            "\n\n🛑 STOP：本步骤完成。请把节点列表呈现给用户，**绝不**自动调用 generate_skills。"
            "等用户明确说『生成技能』再继续。"
        )
        if result.ambiguous_questions:
            tail = (
                "\n\n🛑 STOP（重要）：有待确认事项，**必须**先让用户回答，再做任何后续操作。"
                "\n待确认：\n- " + "\n- ".join(result.ambiguous_questions)
                + tail
            )
        return (f"✅ 已推导 {len(result.flow_steps)} 个流程节点：\n{head}{rs_line}\n"
                f"{result.summary}{tail}")

    def describe_flow_step(step_id: int) -> str:
        """查看某个流程节点的细节（能力描述、模板算子、参数、SQL）。"""
        scenario = _require(scenario_id)
        if not scenario.flow:
            return "尚未推导业务流程。请先 deduce_flow。"
        s = next((x for x in scenario.flow.flow_steps if x.step_id == step_id), None)
        if s is None:
            ids = "、".join(str(x.step_id) for x in scenario.flow.flow_steps)
            return f"未找到节点 {step_id}。可用：{ids}"
        return (
            f"步骤{s.step_id}：{s.step_name}（{s.operation}/{s.template_kind}）\n"
            f"- 该做什么：{s.purpose}\n- 能做什么：{s.capability}\n"
            f"- 数据输入：{s.data_in}\n- 数据输出：{s.data_out}\n"
            f"- 模板算子：{s.template_kind}\n- 参数：{json.dumps(s.params, ensure_ascii=False)}\n"
            f"- 状态：{s.status}\n"
            + (f"- 缺：{s.external_data_needed}\n" if s.external_data_needed else "")
            + (f"- SQL：\n{s.sql}" if s.sql else "- （暂无可执行 SQL，待 refine_flow_step 细化）")
        )

    def refine_flow_step(
        step_id: int,
        template_kind: str = "",
        params_json: str = "",
        sql: str = "",
        purpose: str = "",
        capability: str = "",
    ) -> str:
        """细化/修正某个流程节点。
        二选一：
        * `template_kind` + `params_json`（用模板算子+参数生成 SQL，推荐）；
        * `sql`（直接给 DuckDB SQL，表名=注册视图名，标识符用双引号）。
        可选：`purpose` / `capability` 覆盖节点描述。"""
        scenario = _require(scenario_id)
        if not scenario.flow:
            return "尚未推导业务流程。请先 deduce_flow。"
        s = next((x for x in scenario.flow.flow_steps if x.step_id == step_id), None)
        if s is None:
            return f"未找到节点 {step_id}。"
        if purpose:
            s.purpose = purpose
        if capability:
            s.capability = capability
        if sql:
            s.template_kind = "sql"
            s.strategy = "manual"
            s.sql = sql
            s.status = "executable"
        elif template_kind:
            params: dict = {}
            if params_json:
                try:
                    params = json.loads(params_json)
                except Exception as exc:  # noqa: BLE001
                    return f"params_json 解析失败：{exc}"
            s.template_kind = template_kind
            s.strategy = template_kind
            s.params = params
            built = strategies.build_sql(template_kind, params)
            if not built:
                return (f"模板「{template_kind}」+ 给定参数未能生成 SQL，请检查参数。"
                        "可参考 strategies 模块的各算子参数约定。")
            s.sql = built
            s.status = "executable"
            s.external_data_needed = []
        _refresh_domain_and_outputs(scenario)
        store.save(scenario)
        return f"已细化节点 {step_id}（{s.template_kind}）。状态：{s.status}。"

    # ---------------------------------------------------------------- 步骤 5：生成技能
    def generate_skills() -> str:
        """【步骤 5】按业务流程节点固化技能：
        - 每个流程节点 → 一个技能（节点级能力）
        - 另加一个「主技能」串联整条管线对外产出（list_outputs / produce）。
        无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.flow or not scenario.flow.flow_steps:
            return ("🛑 STOP：尚未完成业务流程推导（步骤 4）。"
                    "请提示用户先说『推导业务流程』，**不要**继续生成技能。")
        try:
            materialized = materialize_skills(scenario)
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            return (
                f"❌ 技能生成失败（{type(exc).__name__}：{str(exc)[:400]}）。\n\n"
                f"详细堆栈（前 800 字）：\n{tb[:800]}\n\n"
                "请将以上错误信息告知用户，不要声称技能已生成。\n"
                "🛑 STOP：生成失败，勿继续后续操作。"
            )
        scenario.skills = materialized
        scenario.status = ScenarioStatus.SKILLS_GENERATED
        store.save(scenario)
        names = "、".join(s.name for s in materialized)
        return (f"✅ 已生成 {len(materialized)} 个技能：{names}。"
                "\n\n🛑 STOP：本步骤完成。技能包已落盘，蒸馏工作到此结束。"
                "请提示用户切换到**验证通道**执行产出，**不要**在本通道尝试执行。")

    def list_skills() -> str:
        """列出当前业务场景已落盘的技能库（含每个流程节点对应的子技能）。"""
        scenario = _require(scenario_id)
        if not scenario.skills:
            return "尚未生成技能库。请先 deduce_flow + generate_skills。"
        lines = []
        for s in scenario.skills:
            tag = "（主）" if s.is_main else ("（进化）" if s.is_evolved else "")
            cap = f" — {s.capability[:60]}" if s.capability else ""
            lines.append(f"- {s.skill_id}{tag}：{s.name}{cap}")
        return (f"已落盘 {len(scenario.skills)} 个技能，目录 {store.skills_dir(scenario_id)}：\n"
                + "\n".join(lines))

    def read_skill(skill_id: str) -> str:
        """读取某个已落盘技能的 SKILL.md 与执行脚本。参数：skill_id。"""
        scenario = _require(scenario_id)
        skill_dir = Path(store.skills_dir(scenario_id)) / skill_id
        md_file = skill_dir / "SKILL.md"
        run_file = skill_dir / "scripts" / "skill_executor.py"
        if not run_file.exists():
            run_file = skill_dir / "scripts" / "run.py"
        if not md_file.exists():
            ids = "、".join(s.skill_id for s in scenario.skills) or "（无）"
            return f"未找到技能「{skill_id}」。可用技能：{ids}"
        parts = [f"# {skill_id}/SKILL.md\n" + md_file.read_text(encoding="utf-8")]
        if run_file.exists():
            parts.append(f"# {skill_id}/{run_file.name}\n" + run_file.read_text(encoding="utf-8"))
        text = "\n\n".join(parts)
        return text[:4000] + ("…（已截断）" if len(text) > 4000 else "")

    def get_trace_sample(result_table_name: str = "") -> str:
        """读取已保存的追踪驱动采样结果，不会现场重跑大表追踪。
        如尚未追踪，请先调用 trace_data_links。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "🛑 当前场景未上传任何表。"
        trace_report = scenario.trace_chain or (
            scenario.relations.trace_chain if scenario.relations else {}
        )
        if not trace_report:
            return "尚未执行数据链路追踪。请先调用 trace_data_links。"
        if result_table_name and trace_report.get("result_table") != result_table_name:
            return f"已保存链路入口为「{trace_report.get('result_table', '')}」，不是「{result_table_name}」。"
        val = _val.validate_trace_connectivity(trace_report)
        level_emoji = {"pass": "✅", "warning": "⚠️", "fail": "❌"}.get(val.level, "")
        lines = [
            f"追踪驱动采样结果：",
            f"{level_emoji} {val.message}",
            f"追踪摘要：{trace_report.get('trace_summary', '')}",
            f"总样本行数：{trace_report.get('total_rows', 0)}",
        ]
        trace_map = trace_report.get("trace_map", {})
        for tbl, info in trace_map.items():
            rows = info.get("matched_rows", [])
            by = info.get("matched_by", "")
            conf = info.get("trace_confidence", "?")
            n = len(rows)
            lines.append(
                f"\n【{tbl}】通过「{by}」追踪 {n} 行（置信度:{conf}）"
                if by != "random"
                else f"\n【{tbl}】随机采样 {n} 行（⚠️ 未找到关联路径）"
            )
            if rows:
                import json as _json
                lines.append(_json.dumps(rows[:2], ensure_ascii=False)[:500])
        return "\n".join(lines)

    # 蒸馏工具集（仅蒸馏阶段使用，不包含执行/验证工具）
    return [
        StructuredTool.from_function(extract_metadata),
        StructuredTool.from_function(get_field_sample),
        StructuredTool.from_function(set_table_role),
        StructuredTool.from_function(trace_data_links),
        StructuredTool.from_function(deduce_relations),
        StructuredTool.from_function(correct_relation),
        StructuredTool.from_function(deduce_flow),
        StructuredTool.from_function(describe_flow_step),
        StructuredTool.from_function(refine_flow_step),
        StructuredTool.from_function(generate_skills),
        StructuredTool.from_function(list_skills),
        StructuredTool.from_function(read_skill),
        StructuredTool.from_function(get_trace_sample),
    ]


# 别名，供外部使用
build_distillation_tools = build_tools
