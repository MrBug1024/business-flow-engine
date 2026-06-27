"""Agent 工具集。

这些工具是 AI 复刻业务流程的「手脚」：读取表结构、保存推导结论、生成技能、查询数据。
所有工具都绑定到具体业务场景（通过工厂函数闭包注入 scenario_id），
并将结论持久化到存储层；对话服务据此向前端推送「图谱/技能已更新」事件。

关键约束：`inspect_table` 仅返回表头与少量样本，工具层面杜绝整表遍历。
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import StructuredTool

from . import heuristics, inference, table_io
from .models import (
    Scenario,
    ScenarioStatus,
)
from .skill_builder import materialize_skills
from .storage import store

# 工具调用产生的「副作用资源」标记，供对话服务推送刷新事件
TOOL_REFRESH_MAP = {
    "deduce_relations": "relations",
    "deduce_flow": "flow",
    "generate_skills": "skills",
}


def _require(scenario_id: str) -> Scenario:
    scenario = store.get(scenario_id)
    if scenario is None:
        raise ValueError(f"业务场景 {scenario_id} 不存在")
    return scenario


def build_tools(scenario_id: str) -> list[StructuredTool]:
    """构建绑定到指定业务场景的工具列表。"""

    # ------------------------------------------------------------- 读取结构
    def list_tables() -> str:
        """列出当前业务场景已上传的所有业务表及其行列规模、字段清单。
        在分析关联关系或业务流程前，应先调用本工具了解整体情况。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "当前业务场景尚未上传任何业务表。请提示用户先上传数据表。"
        lines = []
        for t in scenario.tables_meta:
            cols = ", ".join(c.name for c in t.columns)
            lines.append(
                f"- 表「{t.table_name}」：{t.row_count} 行 × {t.col_count} 列；字段：{cols}"
            )
        return "已上传业务表：\n" + "\n".join(lines)

    def inspect_table(table_name: str) -> str:
        """查看指定业务表的详细结构：字段名、数据类型、空值率，以及 1~3 条随机样本。
        这是了解表内容的唯一途径——绝不整表读取，只看表头和少量样本。

        参数 table_name: 业务表名称（来自 list_tables 的结果）。"""
        scenario = _require(scenario_id)
        meta = next(
            (t for t in scenario.tables_meta if t.table_name == table_name), None
        )
        if meta is None:
            names = "、".join(t.table_name for t in scenario.tables_meta) or "（无）"
            return f"未找到表「{table_name}」。可用表：{names}"
        # 重新轻量扫描，确保样本是随机的、最新的
        try:
            fresh = table_io.inspect_table(meta.file_path, table_name)
        except Exception as exc:  # noqa: BLE001
            fresh = meta  # 读取失败时回退到已存元信息
            note = f"（注意：实时扫描失败，展示已存元信息：{exc}）\n"
        else:
            note = ""
        cols = [
            {
                "字段": c.name,
                "类型": c.dtype,
                "空值率": f"{c.null_rate:.0%}",
                "样本值": c.sample_values,
            }
            for c in fresh.columns
        ]
        payload = {
            "表名": fresh.table_name,
            "行数": fresh.row_count,
            "列数": fresh.col_count,
            "表头所在行": fresh.header_row,  # >0 表示已自动跳过上方的标题/空行
            "字段详情": cols,
            "随机样本行": fresh.sample_rows,
        }
        if fresh.header_row > 0:
            note += f"（提示：已自动识别表头位于第 {fresh.header_row + 1} 行，上方 {fresh.header_row} 行标题/空行已跳过）\n"
        return note + json.dumps(payload, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------- 推导并保存
    def deduce_relations() -> str:
        """推导所有已上传表之间的关联关系，保存结论并生成关系图谱（前端据此渲染）。
        当用户要求「推导关联关系/表关系」时调用本工具——无需传参，工具会基于表头与样本
        自动完成结构化推导与持久化。返回推导摘要与待用户确认的问题。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "当前业务场景尚未上传任何业务表，无法推导。请提示用户先上传数据表。"
        result = inference.infer_relations(scenario)
        scenario.relations = result
        scenario.status = ScenarioStatus.RELATIONS_DEDUCED
        store.save(scenario)
        lines = "；".join(
            f"{r.from_table}.{r.from_column}→{r.to_table}.{r.to_column}({r.confidence:.0%})"
            for r in result.relations
        )
        q = ("\n待确认：" + "；".join(result.ambiguous_questions)) if result.ambiguous_questions else ""
        return f"已保存 {len(result.relations)} 条关联关系并生成关系图谱。{result.summary}\n{lines}{q}"

    def deduce_flow() -> str:
        """以结果表为终点逆向推导完整业务流程，保存并生成流程图谱。
        当用户要求「推导业务流程」时调用——无需传参，工具会基于表结构与已推导的关联关系
        自动完成结构化推导与持久化。建议先有关联关系。返回流程摘要与待确认问题。"""
        scenario = _require(scenario_id)
        if not scenario.tables_meta:
            return "尚未上传业务表，无法推导流程。"
        result = inference.infer_flow(scenario)
        scenario.flow = result
        scenario.status = ScenarioStatus.FLOW_DEDUCED
        store.save(scenario)
        steps = "；".join(f"步骤{s.step_id} {s.step_name}({s.operation})" for s in result.flow_steps)
        q = ("\n待确认：" + "；".join(result.ambiguous_questions)) if result.ambiguous_questions else ""
        return f"已保存 {len(result.flow_steps)} 个流程步骤并生成流程图谱。{result.summary}\n{steps}{q}"

    def generate_skills() -> str:
        """基于已推导的业务流程，生成可复用的技能库（每个流程步骤一个 Skill，
        外加一个总执行器）。技能会落盘为 SKILL.md + scripts/run.py。
        调用前应确保业务流程已推导并经用户确认。"""
        scenario = _require(scenario_id)
        if not scenario.flow or not scenario.flow.flow_steps:
            return "尚未推导业务流程，无法生成技能。请先完成业务流程推导。"
        specs = heuristics.build_skill_specs(scenario)
        materialized = materialize_skills(scenario, specs)
        scenario.skills = materialized
        scenario.status = ScenarioStatus.SKILLS_GENERATED
        store.save(scenario)
        names = "、".join(s.name for s in materialized)
        return f"已生成 {len(materialized)} 个技能：{names}。"

    # ------------------------------------------------------------- 查看技能
    def list_skills() -> str:
        """列出当前业务场景**已生成并落盘**的技能库。
        在「执行业务/验证流程」之前应先调用本工具确认已有哪些技能可用——
        技能确实保存在场景目录的 skills/ 下，不要臆断其不存在。"""
        scenario = _require(scenario_id)
        if not scenario.skills:
            return "当前业务场景尚未生成技能库。请先完成业务流程推导并调用 generate_skills。"
        lines = []
        for s in scenario.skills:
            tag = "［总执行器］" if s.is_main else ("［进化技能］" if s.is_evolved else "")
            lines.append(f"- {s.skill_id}{tag}：{s.name}（{s.operation}）")
        return (
            f"已落盘技能共 {len(scenario.skills)} 个，保存在 {store.skills_dir(scenario_id)}：\n"
            + "\n".join(lines)
            + "\n可用 read_skill 查看某个技能的 SKILL.md 与脚本。"
        )

    def read_skill(skill_id: str) -> str:
        """读取某个已落盘技能的内容（SKILL.md + scripts/run.py），
        以便理解其能力并用于执行业务或验证。

        参数 skill_id: 技能标识（来自 list_skills 的结果）。"""
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

    # ------------------------------------------------------------- 执行业务
    def query_data(table_name: str, pandas_expression: str) -> str:
        """对某张业务表执行一段 pandas 表达式以回答数据问题或执行业务计算。
        变量 `df` 即该表的数据帧。例如：
            "df[df['status']=='已完成']['amount'].sum()"
            "df.groupby('category')['amount'].sum().head(10)"

        参数:
            table_name: 目标业务表名称。
            pandas_expression: 在 df 上求值的 pandas 表达式（只读，勿做写操作）。
        """
        scenario = _require(scenario_id)
        meta = next(
            (t for t in scenario.tables_meta if t.table_name == table_name), None
        )
        if meta is None:
            return f"未找到表「{table_name}」。"
        try:
            import pandas as pd  # noqa: F401  供表达式使用

            df = table_io.load_full_frame(meta.file_path)
            result = eval(pandas_expression, {"pd": pd, "__builtins__": {}}, {"df": df})  # noqa: S307
        except Exception as exc:  # noqa: BLE001
            return f"执行失败：{exc}"
        text = str(result)
        return text[:2000] + ("…（已截断）" if len(text) > 2000 else "")

    # 用 StructuredTool 显式包装，便于统一管理与命名
    return [
        StructuredTool.from_function(list_tables),
        StructuredTool.from_function(inspect_table),
        StructuredTool.from_function(deduce_relations),
        StructuredTool.from_function(deduce_flow),
        StructuredTool.from_function(generate_skills),
        StructuredTool.from_function(list_skills),
        StructuredTool.from_function(read_skill),
        StructuredTool.from_function(query_data),
    ]
