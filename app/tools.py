"""Agent 工具集（v1.0.1 三层架构的「桥梁」）。

这些工具是 AI 复刻业务能力的唯一手脚，严格贯彻三层分离：

* 元数据/采样层（Python，无 AI）：`extract_metadata` / `get_field_sample` / `parse_rules`
  —— 只把表结构、少量样本、规则摘要交给 AI，**绝不**把全量原始数据喂给 AI。
* AI 推理层：读元数据报告与规则摘要，推导 ER、构造规则模板、生成 SQL/pandas 逻辑候选
  （`deduce_relations` / `define_audit_logic`）。
* 验证/执行层（Python）：`execute_and_compare` 在完整原始数据上执行 AI 逻辑、与历史结果
  对照，只回传差异摘要。

所有工具绑定到具体业务场景（通过工厂闭包注入 scenario_id），并把结论持久化。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import StructuredTool

from . import executor, inference, metadata, process, rule_parser, sql_builder, table_io
from .models import RuleLibrary, Scenario, ScenarioStatus
from .skill_builder import materialize_skills
from .storage import store

# 工具调用产生的「副作用资源」标记，供对话服务推送刷新事件
TOOL_REFRESH_MAP = {
    "discover_business_process": "business_process",
    "deduce_relations": "relations",
    "parse_rules": "rules",
    "define_audit_sql": "rules",
    "generalize_rules": "rules",
    "deduce_flow": "flow",
    "execute_and_compare": "validations",
    "generate_skills": "skills",
}


def _require(scenario_id: str) -> Scenario:
    scenario = store.get(scenario_id)
    if scenario is None:
        raise ValueError(f"业务场景 {scenario_id} 不存在")
    return scenario


def _phase0_gate(scenario: Scenario) -> str | None:
    """Phase 0 审批 Gate：未生成或未批准业务流程文档时，阻止进入后续阶段。

    返回阻止原因（字符串）；已通过则返回 None。
    """
    bp = scenario.business_process
    if bp is None:
        return ("⛔ Phase 0 未完成：尚未生成业务流程文档。请先调用 `discover_business_process` "
                "梳理业务流程，呈现给用户并取得批准后，才能进入关联/规则/校验/技能等后续阶段。")
    if not bp.approved:
        return ("⛔ Phase 0 待审批：业务流程文档（business_process.md）尚未获用户批准。"
                "请把文档呈现给用户，待其确认无误后再继续后续阶段。")
    return None


def build_tools(scenario_id: str) -> list[StructuredTool]:
    """构建绑定到指定业务场景的工具列表。"""

    # ===================================================== 元数据/采样层（无 AI）
    def extract_metadata() -> str:
        """读取业务场景的「元数据报告」：所有表的结构、字段类型、规模估计、每表 1~3 条样本，
        以及规则库摘要。这是你了解数据的**唯一蓝图**——绝不逐行翻看全量数据。
        在任何推导之前先调用本工具。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "当前业务场景尚未上传任何业务表。请提示用户先上传数据表（含业务表、规则表、历史结果表）。"
        return metadata.build_metadata_report(scenario)

    def get_field_sample(table_name: str, field_name: str) -> str:
        """获取某表某字段的若干「去重样本值」，用于在推理时核对取值空间（桥梁工具，绝不返回全量数据）。

        参数：table_name 表名；field_name 字段名。"""
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

    # ===================================================== Phase 0：业务流程发现
    def discover_business_process(description: str = "") -> str:
        """【Phase 0｜流程发现，最先执行】梳理「这个业务到底在做什么」，生成业务流程文档
        business_process.md：白话描述 + 处理步骤 + 输入表/规则表/结果表识别 + 流程图。

        参数：description（可选）—— 用户对业务背景的白话描述；缺省时用场景描述/表名启发式生成。
        生成后状态置为「待审批」。你必须把该文档原样呈现给用户，并以结构化交互请求其批准；
        **用户批准前，关联/规则/校验/技能等后续阶段一律被 Gate 阻止。**"""
        scenario = _require(scenario_id)
        bp = process.discover_process(scenario, description=description)
        store.write_business_process(scenario_id, bp.markdown)
        scenario.business_process = bp
        if scenario.status in (ScenarioStatus.CREATED, ScenarioStatus.TABLES_UPLOADED):
            scenario.status = ScenarioStatus.PROCESS_DRAFTED
        store.save(scenario)
        return (bp.markdown
                + "\n\n———\n（✅ 已保存为 business_process.md，状态=待审批。"
                "请把以上文档完整呈现给用户，并询问是否准确、可否批准。"
                "用户批准后才能进入 Phase 1 及后续阶段。）")

    def parse_rules() -> str:
        """解析规则表（领域知识库），生成结构化的「规则模板库」：每条规则含违规类型、关键词、
        逻辑描述、政策依据、案例示例。这是把规则表变成可参数化审核能力的第一步。无需传参。"""
        scenario = _require(scenario_id)
        gate = _phase0_gate(scenario)
        if gate:
            return gate
        rule_table = rule_parser.find_rule_table(scenario)
        if rule_table is None:
            return ("未识别到规则表。规则表应是包含「违规类型/规则情形/政策依据/示例」等列的知识库表，"
                    "表名通常含「规则/清单」。请提示用户上传规则表。")
        library = rule_parser.parse_rule_table(rule_table)
        scenario.rule_library = library
        # Phase 1+2：构建领域知识（数据字典 + 结果契约）并为每个违规类型生成 SQL 模板
        domain = sql_builder.build_domain_knowledge(scenario)
        scenario.domain_knowledge = domain
        sql_builder.build_rule_sql_library(scenario, domain)
        if scenario.status in (ScenarioStatus.CREATED, ScenarioStatus.TABLES_UPLOADED,
                               ScenarioStatus.RELATIONS_DEDUCED, ScenarioStatus.PROCESS_APPROVED):
            scenario.status = ScenarioStatus.RULES_PARSED
        store.save(scenario)
        vtypes = library.violation_types
        preview = "、".join(vtypes[:12]) + ("…" if len(vtypes) > 12 else "")
        return (f"{library.summary}\n违规类型（前若干）：{preview}\n"
                "每个违规类型均已生成可执行 SQL 模板（DuckDB）。"
                "可用 list_audit_types 查看全部，describe_audit_type 看具体 SQL，"
                "execute_and_compare 对照历史结果校验，define_audit_sql 手工细化某类型 SQL。")

    # ===================================================== AI 推理层
    def deduce_relations() -> str:
        """基于元数据报告推导所有表之间的关联关系（ER 模型），保存并生成关系图谱。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "尚未上传任何业务表，无法推导。"
        gate = _phase0_gate(scenario)
        if gate:
            return gate
        result = inference.infer_relations(scenario)
        scenario.relations = result
        # Phase 1：刷新领域知识（把 ER 关系并入数据字典）；若规则库已存在则同步刷新 SQL
        domain = sql_builder.build_domain_knowledge(scenario)
        scenario.domain_knowledge = domain
        if scenario.rule_library and scenario.rule_library.templates:
            sql_builder.build_rule_sql_library(scenario, domain)
        if scenario.status in (ScenarioStatus.TABLES_UPLOADED, ScenarioStatus.PROCESS_APPROVED):
            scenario.status = ScenarioStatus.RELATIONS_DEDUCED
        store.save(scenario)
        lines = "；".join(
            f"{r.from_table}.{r.from_column}→{r.to_table}.{r.to_column}({r.confidence:.0%})"
            for r in result.relations
        )
        q = ("\n待确认：" + "；".join(result.ambiguous_questions)) if result.ambiguous_questions else ""
        return f"已保存 {len(result.relations)} 条关联关系并生成关系图谱。{result.summary}\n{lines}{q}"

    def list_audit_types() -> str:
        """列出规则库中所有可用的违规类型（审核类型），即本业务能力可执行的全部审核项。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.rule_library or not scenario.rule_library.templates:
            return "规则库尚未解析。请先调用 parse_rules。"
        groups = rule_parser.violation_type_groups(scenario.rule_library)
        lines = []
        for vt, tmpls in groups.items():
            statuses = {t.status for t in tmpls}
            tag = "已校验✓" if "verified" in statuses else (
                "可执行(未校验)" if "unverified" in statuses else (
                    "缺数据/口径" if "blocked" in statuses else "未细化"))
            lines.append(f"- {vt}（{len(tmpls)} 条细则，{tag}）")
        return f"共 {len(groups)} 种违规类型：\n" + "\n".join(lines)

    def describe_audit_type(violation_type: str) -> str:
        """查看某违规类型的细则：逻辑描述、政策依据、案例示例、已定义的执行口径与状态。

        参数：violation_type 违规类型（来自 list_audit_types）。"""
        scenario = _require(scenario_id)
        tmpls = _templates_of(scenario, violation_type)
        if not tmpls:
            return f"规则库中未找到违规类型「{violation_type}」。请用 list_audit_types 查看可用类型。"
        out = [f"违规类型「{violation_type}」共 {len(tmpls)} 条细则："]
        for t in tmpls[:6]:
            out.append(
                f"\n• [{t.rule_id}] 状态={t.status}（策略：{t.strategy or '—'}）\n  逻辑：{t.logic_description[:120]}\n"
                f"  政策依据：{t.policy_basis or '（无）'}\n  示例：{t.example[:120] or '（无）'}"
                + (f"\n  所需表：{t.required_tables}" if t.required_tables else "")
                + (f"\n  缺外部数据：{t.external_data_needed}" if t.external_data_needed else "")
                + (f"\n  SQL：\n{t.sql}" if t.sql else "\n  （暂无可执行 SQL）")
            )
        # 提示是否存在历史结果表可供校验
        hist = executor.find_historical_table(scenario, violation_type)
        out.append(f"\n历史结果表：{hist.table_name if hist else '（无，无法对照校验，可直接在新数据执行）'}")
        return "\n".join(out)

    def define_audit_sql(
        violation_type: str,
        sql: str = "",
        required_tables: str = "",
        external_data_needed: str = "",
    ) -> str:
        """为某违规类型手工定义/覆盖可执行 SQL（DuckDB 方言），用于细化或修正确定性模板。

        参数：
            violation_type: 违规类型。
            sql: 可执行 SQL。表名=注册视图名（即原表名），标识符用双引号，例如：
                 SELECT *, '重复收费' AS "违规类型" FROM "项目明细表" d
                 JOIN (SELECT "就诊ID","医保目录编码" FROM "项目明细表"
                       GROUP BY 1,2 HAVING COUNT(*)>1) k
                 ON d."就诊ID"=k."就诊ID" AND d."医保目录编码"=k."医保目录编码"
            required_tables: 逗号分隔的所需表名（SQL 引用的表）。
            external_data_needed: 逗号分隔的缺失外部数据；非空则标记为 blocked（运行时拒绝执行）。
        """
        scenario = _require(scenario_id)
        gate = _phase0_gate(scenario)
        if gate:
            return gate
        tmpls = _templates_of(scenario, violation_type)
        if not tmpls:
            return f"未找到违规类型「{violation_type}」，无法定义 SQL。"

        def _split(s: str) -> list[str]:
            return [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]

        ext = _split(external_data_needed)
        # output_columns 对齐结果契约（沿用已生成的；缺失时按结果结构补齐）
        out_cols = next((t.output_columns for t in tmpls if t.output_columns), [])
        for t in tmpls:
            if required_tables:
                t.required_tables = _split(required_tables)
            t.external_data_needed = ext
            if ext:
                t.status = "blocked"
                t.sql = ""
                t.strategy = "blocked"
            elif sql:
                t.sql = sql
                t.strategy = "manual"
                t.status = "unverified"
                if not t.output_columns:
                    t.output_columns = out_cols
        store.save(scenario)
        if ext:
            return (f"已为「{violation_type}」声明缺失外部数据 {ext}，标记为 blocked，运行时将拒绝执行并说明缺什么。")
        if sql:
            return (f"已为「{violation_type}」写入可执行 SQL（{len(tmpls)} 条细则）。"
                    "下一步可调用 execute_and_compare 在完整数据上执行并对照历史结果校验。")
        return f"未提供 sql 或 external_data_needed，未做改动。"

    def generalize_rules() -> str:
        """【Phase 4｜泛化】为规则库中所有违规类型批量（重新）生成 SQL 模板（确定性，幂等）。

        确保每个类型都有可执行 SQL 或被标记 blocked（缺数据）。无需传参。"""
        scenario = _require(scenario_id)
        gate = _phase0_gate(scenario)
        if gate:
            return gate
        if not scenario.rule_library or not scenario.rule_library.templates:
            return "规则库尚未解析。请先 parse_rules。"
        domain = sql_builder.build_domain_knowledge(scenario)
        scenario.domain_knowledge = domain
        sql_builder.build_rule_sql_library(scenario, domain)
        store.save(scenario)
        lib = scenario.rule_library
        n_exec = sum(1 for t in lib.templates if t.sql)
        n_blocked = sum(1 for t in lib.templates if t.status == "blocked")
        n_verified = sum(1 for t in lib.templates if t.status == "verified")
        return (f"已为全部 {len(lib.violation_types)} 种违规类型生成 SQL 模板："
                f"可执行 {n_exec} 条、已校验 {n_verified} 条、缺数据 {n_blocked} 条。"
                "下一步可 generate_skills 固化为单一参数化技能。")

    # ===================================================== 验证/执行层（Python）
    def execute_and_compare(violation_type: str) -> str:
        """在**完整原始数据**上执行某违规类型的审核逻辑，并与历史结果表对照，返回差异摘要
        （命中/缺失/多出 + 少量样本）。你只会看到摘要，看不到全量数据。

        参数：violation_type 违规类型。"""
        scenario = _require(scenario_id)
        gate = _phase0_gate(scenario)
        if gate:
            return gate
        tmpls = _templates_of(scenario, violation_type)
        if not tmpls:
            return f"未找到违规类型「{violation_type}」。"
        target = next((t for t in tmpls if t.code), tmpls[0])
        report = executor.execute_and_compare(scenario, target)
        # 回写校验结论
        for t in tmpls:
            t.match_rate = report.match_rate
            if report.passed:
                t.status = "verified"
        scenario.validations = [v for v in scenario.validations
                                if v.violation_type != violation_type] + [report]
        if report.passed and scenario.status in (ScenarioStatus.RULES_PARSED,
                                                  ScenarioStatus.RELATIONS_DEDUCED,
                                                  ScenarioStatus.FLOW_DEDUCED):
            scenario.status = ScenarioStatus.VALIDATED
        store.save(scenario)
        extra = ""
        if report.sample_missing:
            extra += f"\n缺失样本(≤3)：{report.sample_missing}"
        if report.sample_extra:
            extra += f"\n多出样本(≤3)：{report.sample_extra}"
        nxt = ("\n✅ 已校验通过（标记 verified）。可 generalize_rules 泛化其余类型，"
               "或 generate_skills 固化为单一参数化技能。"
               if report.passed else "\n⚠️ 未达标，请据差异样本用 define_audit_sql 调整该类型 SQL 后重试。")
        return report.message + extra + nxt

    def deduce_flow() -> str:
        """以历史结果表为终点，逆向推导某审核能力的处理流程链（过滤→关联→规则→聚合→计算），
        保存并生成流程图谱。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "尚未上传业务表，无法推导流程。"
        gate = _phase0_gate(scenario)
        if gate:
            return gate
        result = inference.infer_flow(scenario)
        scenario.flow = result
        if scenario.status == ScenarioStatus.RULES_PARSED:
            scenario.status = ScenarioStatus.FLOW_DEDUCED
        store.save(scenario)
        steps = "；".join(f"步骤{s.step_id} {s.step_name}({s.operation})" for s in result.flow_steps)
        q = ("\n待确认：" + "；".join(result.ambiguous_questions)) if result.ambiguous_questions else ""
        return f"已保存 {len(result.flow_steps)} 个流程步骤并生成流程图谱。{result.summary}\n{steps}{q}"

    # ===================================================== 技能固化
    def generate_skills() -> str:
        """把规则库 + 已校验逻辑固化为**参数化审核技能**：暴露 list_audit_types() 与
        execute_audit(violation_type, data_sources)，可在新数据上动态执行任意违规类型的审核，
        而非写死某一种。落盘为 SKILL.md + scripts/。无需传参。"""
        scenario = _require(scenario_id)
        gate = _phase0_gate(scenario)
        if gate:
            return gate
        if not scenario.rule_library or not scenario.rule_library.templates:
            return "规则库尚未解析，无法固化技能。请先 parse_rules。"
        materialized = materialize_skills(scenario)
        scenario.skills = materialized
        scenario.status = ScenarioStatus.SKILLS_GENERATED
        store.save(scenario)
        names = "、".join(s.name for s in materialized)
        return (f"已生成 {len(materialized)} 个技能：{names}。"
                "核心是参数化审核技能（list_audit_types / execute_audit），可对新数据执行任意违规类型审核。")

    def list_skills() -> str:
        """列出当前业务场景已落盘的技能库。无需传参。"""
        scenario = _require(scenario_id)
        if not scenario.skills:
            return "尚未生成技能库。请先 parse_rules 再 generate_skills。"
        lines = []
        for s in scenario.skills:
            tag = "［审核总技能］" if s.is_main else ("［进化技能］" if s.is_evolved else "")
            lines.append(f"- {s.skill_id}{tag}：{s.name}（{s.operation}）")
        return (f"已落盘技能共 {len(scenario.skills)} 个，保存在 {store.skills_dir(scenario_id)}：\n"
                + "\n".join(lines))

    def read_skill(skill_id: str) -> str:
        """读取某个已落盘技能的 SKILL.md 与 scripts/run.py。参数：skill_id。"""
        scenario = _require(scenario_id)
        skill_dir = Path(store.skills_dir(scenario_id)) / skill_id
        md_file = skill_dir / "SKILL.md"
        run_file = skill_dir / "scripts" / "run.py"
        if not md_file.exists():
            ids = "、".join(s.skill_id for s in scenario.skills) or "（无）"
            return f"未找到技能「{skill_id}」。可用技能：{ids}"
        parts = [f"# {skill_id}/SKILL.md\n" + md_file.read_text(encoding="utf-8")]
        if run_file.exists():
            parts.append(f"# {skill_id}/scripts/run.py\n" + run_file.read_text(encoding="utf-8"))
        text = "\n\n".join(parts)
        return text[:4000] + ("…（已截断）" if len(text) > 4000 else "")

    return [
        StructuredTool.from_function(extract_metadata),
        StructuredTool.from_function(get_field_sample),
        StructuredTool.from_function(discover_business_process),
        StructuredTool.from_function(parse_rules),
        StructuredTool.from_function(deduce_relations),
        StructuredTool.from_function(list_audit_types),
        StructuredTool.from_function(describe_audit_type),
        StructuredTool.from_function(define_audit_sql),
        StructuredTool.from_function(generalize_rules),
        StructuredTool.from_function(execute_and_compare),
        StructuredTool.from_function(deduce_flow),
        StructuredTool.from_function(generate_skills),
        StructuredTool.from_function(list_skills),
        StructuredTool.from_function(read_skill),
    ]


# ---------------------------------------------------------------------------
def _templates_of(scenario: Scenario, violation_type: str):
    lib: RuleLibrary | None = scenario.rule_library
    if not lib:
        return []
    vt = violation_type.strip()
    exact = [t for t in lib.templates if t.violation_type == vt]
    if exact:
        return exact
    # 容错：模糊包含匹配
    return [t for t in lib.templates if vt and (vt in t.violation_type or t.violation_type in vt)]
