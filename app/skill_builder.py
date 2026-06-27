"""技能落盘。

将推导出的业务能力固化为符合规范的 Skill 目录（需求 Note 1）：

    skills/<skill_id>/
        SKILL.md            技能元信息 + 接口规范 + 能力说明
        scripts/run.py      可执行逻辑（对新数据复刻业务流程）

设计理念（需求 Note 3/4）：技能不是「写死某次查询」，而是像一名工程师那样
掌握表结构、字段、关联键与处理流程，从而具备对新数据「增删改查 + 业务计算」的通用能力。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import Scenario, Skill
from .storage import store

_MAIN_RUNNER_TEMPLATE = '''"""业务流程总执行器（自动生成）。

对「与历史数据同结构」的新业务表，按推导出的流程顺序执行，产出同逻辑的结果。
本脚本读取同目录下的 flow_spec.json 作为流程定义，便于持续迭代而无需改代码。
"""

import json
from pathlib import Path

import pandas as pd

SPEC_PATH = Path(__file__).resolve().parent.parent / "flow_spec.json"


def load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def run(table_paths: dict[str, str]) -> pd.DataFrame:
    """执行业务流程。

    参数:
        table_paths: {表名: 新数据文件路径}
    返回:
        结果数据帧（结构与历史结果表一致）。

    说明: 此处给出可运行骨架，关联键 / 过滤条件 / 聚合维度等业务参数
          由 flow_spec.json 描述，可在确认业务口径后逐步细化为精确实现。
    """
    spec = load_spec()
    frames = {name: pd.read_csv(p) if str(p).endswith((".csv", ".tsv")) else pd.read_excel(p)
              for name, p in table_paths.items()}
    # 按 flow_spec 中的 steps 顺序执行（骨架：默认透传主表，待业务参数细化）
    primary = next(iter(frames.values())) if frames else pd.DataFrame()
    result = primary
    print(f"已加载 {len(frames)} 张表；流程包含 {len(spec.get('flow_steps', []))} 个步骤。")
    return result


if __name__ == "__main__":
    import sys

    mapping = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    print(run(mapping).head())
'''

_STEP_RUNNER_TEMPLATE = '''"""{step_name}（{operation}）—— 自动生成的子技能。

{description}
"""

import pandas as pd


def run(df: pd.DataFrame, **params) -> pd.DataFrame:
    """对输入数据帧执行「{step_name}」操作。

    伪逻辑: {logic}
    伪 SQL: {pseudo_sql}
    """
    # TODO: 依据确认后的业务口径细化为精确实现
    return df
'''


def _write_skill_md(skill_dir: Path, skill: Skill, scenario: Scenario) -> None:
    columns_hint = ""
    if skill.is_main:
        columns_hint = "\n".join(
            f"- `{t.table_name}`：{t.row_count} 行 / {t.col_count} 列；"
            f"字段 {', '.join(c.name for c in t.columns)}"
            for t in scenario.tables_meta
        )
    md = f"""---
name: {skill.skill_id}
operation: {skill.operation}
scenario: {scenario.name}
is_main: {str(skill.is_main).lower()}
---

# {skill.name}

{skill.description}

## 适用业务场景
{scenario.name} —— {scenario.description or "（无描述）"}

## 能力说明
本技能掌握以下业务结构与处理逻辑，可对**新传入的同结构数据**复刻业务过程：

{columns_hint or f"承担业务流程中「{skill.name}」环节（操作类型：{skill.operation}）。"}

## 接口规范
- 入口脚本：`scripts/run.py`
- 调用方式：`run(df, **params)`（子技能）或 `run(table_paths)`（总执行器）
- 输入：与历史数据同结构的业务表
- 输出：符合业务逻辑的中间结果或最终结果表

## 迭代说明
关联键、过滤条件、聚合维度等业务参数记录于场景的流程定义中，
确认业务口径后可在 `scripts/run.py` 内逐步细化为精确实现。
"""
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")


def materialize_skills(scenario: Scenario, skills: list[Skill]) -> list[Skill]:
    """将技能规格写入磁盘，返回带 `path` 的技能列表。

    重新生成前会清空旧的技能目录，避免上一次推导遗留的陈旧技能堆积、与元数据不一致。
    （进化技能通过 `materialize_evolved_skill` 单独追加，不受此清空影响——
    它在本批次落盘之后才被调用。）
    """
    base = store.skills_dir(scenario.id)
    # 清空旧技能目录（仅清 skills/ 下的内容，flow_spec.json 在上级目录不受影响）
    for child in base.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)

    # 落盘流程定义，供总执行器读取
    if scenario.flow is not None:
        (base.parent / "flow_spec.json").write_text(
            scenario.flow.model_dump_json(indent=2), encoding="utf-8"
        )

    materialized: list[Skill] = []
    step_map = {s.step_id: s for s in (scenario.flow.flow_steps if scenario.flow else [])}

    for skill in skills:
        skill_dir = base / skill.skill_id
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        if skill.is_main:
            (scripts_dir / "run.py").write_text(_MAIN_RUNNER_TEMPLATE, encoding="utf-8")
        else:
            step = step_map.get(skill.step_id or -1)
            (scripts_dir / "run.py").write_text(
                _STEP_RUNNER_TEMPLATE.format(
                    step_name=skill.name,
                    operation=skill.operation,
                    description=skill.description,
                    logic=step.logic if step else "",
                    pseudo_sql=step.pseudo_sql if step else "",
                ),
                encoding="utf-8",
            )

        _write_skill_md(skill_dir, skill, scenario)
        skill.path = str(skill_dir)
        materialized.append(skill)

    return materialized


def materialize_evolved_skill(scenario: Scenario, skill: Skill) -> Skill:
    """落盘一个「进化技能」（用户手动新增的能力）。"""
    skill_dir = store.skills_dir(scenario.id) / skill.skill_id
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "run.py").write_text(
        _STEP_RUNNER_TEMPLATE.format(
            step_name=skill.name,
            operation=skill.operation or "EVOLVED",
            description=skill.description,
            logic="（进化技能：由用户描述定义）",
            pseudo_sql="",
        ),
        encoding="utf-8",
    )
    _write_skill_md(skill_dir, skill, scenario)
    skill.path = str(skill_dir)
    return skill
