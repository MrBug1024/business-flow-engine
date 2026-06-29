"""图谱与技能接口（专用 REST）。

关系图谱、流程图谱、技能库的读取，以及「进化技能」的新增，都走这里，
与流式对话接口解耦。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from .. import executor, rule_parser
from ..models import (
    EvolveSkillRequest,
    ExecuteAuditRequest,
    FlowResult,
    RelationResult,
    RuleLibrary,
    Skill,
    ValidationReport,
)
from ..skill_builder import materialize_evolved_skill
from ..storage import store
from .deps import get_scenario_or_404

router = APIRouter(tags=["graph"])


@router.get("/scenarios/{scenario_id}/relations", response_model=RelationResult)
def get_relations(scenario_id: str) -> RelationResult:
    """获取关联关系及其图谱数据。"""
    scenario = get_scenario_or_404(scenario_id)
    return scenario.relations or RelationResult(summary="尚未推导关联关系。")


@router.get("/scenarios/{scenario_id}/flow", response_model=FlowResult)
def get_flow(scenario_id: str) -> FlowResult:
    """获取业务流程及其图谱数据。"""
    scenario = get_scenario_or_404(scenario_id)
    return scenario.flow or FlowResult(summary="尚未推导业务流程。")


@router.get("/scenarios/{scenario_id}/rules", response_model=RuleLibrary)
def get_rules(scenario_id: str) -> RuleLibrary:
    """获取规则模板库（领域知识库的结构化解析结果）。"""
    scenario = get_scenario_or_404(scenario_id)
    return scenario.rule_library or RuleLibrary(summary="尚未解析规则库。")


@router.get("/scenarios/{scenario_id}/audit-types")
def get_audit_types(scenario_id: str) -> dict:
    """列出全部可审核的违规类型及其状态（参数化审核能力的入口）。"""
    scenario = get_scenario_or_404(scenario_id)
    lib = scenario.rule_library
    if not lib or not lib.templates:
        return {"violation_types": [], "summary": "尚未解析规则库。"}
    groups = rule_parser.violation_type_groups(lib)
    validated = {v.violation_type for v in scenario.validations if v.passed}
    types = []
    for vt, tmpls in groups.items():
        statuses = {t.status for t in tmpls}
        if vt in validated or "verified" in statuses:
            state = "verified"
        elif "unverified" in statuses:
            state = "unverified"
        elif "blocked" in statuses:
            state = "blocked"
        else:
            state = "parsed"
        types.append({
            "violation_type": vt, "rule_count": len(tmpls), "state": state,
            "has_sql": any(t.sql for t in tmpls),
            "has_historical": executor.find_historical_table(scenario, vt) is not None,
        })
    return {"violation_types": types, "summary": lib.summary}


@router.get("/scenarios/{scenario_id}/domain-knowledge")
def get_domain_knowledge(scenario_id: str) -> dict:
    """获取领域知识（数据字典 + ER 关系 + 结果表结构契约）。"""
    scenario = get_scenario_or_404(scenario_id)
    if scenario.domain_knowledge is None:
        return {"scenario": scenario.name, "tables": [], "relations": [], "result_schema": {}}
    return scenario.domain_knowledge.model_dump()


@router.get("/scenarios/{scenario_id}/validations", response_model=list[ValidationReport])
def get_validations(scenario_id: str) -> list[ValidationReport]:
    """获取历次审核校验报告（与历史结果表的差异摘要）。"""
    return get_scenario_or_404(scenario_id).validations


@router.post("/scenarios/{scenario_id}/audit", response_model=ValidationReport)
def execute_audit(scenario_id: str, req: ExecuteAuditRequest) -> ValidationReport:
    """对指定违规类型执行审核并与历史结果对照（验证层，REST 直调）。"""
    scenario = get_scenario_or_404(scenario_id)
    lib = scenario.rule_library
    if not lib or not lib.templates:
        raise HTTPException(status_code=400, detail="规则库尚未解析，请先解析规则库。")
    tmpls = rule_parser.violation_type_groups(lib).get(req.violation_type.strip())
    if not tmpls:
        raise HTTPException(status_code=404, detail=f"未知违规类型：{req.violation_type}")
    target = next((t for t in tmpls if t.code), tmpls[0])
    report = executor.execute_and_compare(scenario, target, req.data_sources or None)
    for t in tmpls:
        t.match_rate = report.match_rate
        if report.passed:
            t.status = "verified"
    scenario.validations = [v for v in scenario.validations
                            if v.violation_type != req.violation_type] + [report]
    store.save(scenario)
    return report


@router.get("/scenarios/{scenario_id}/skills", response_model=list[Skill])
def get_skills(scenario_id: str) -> list[Skill]:
    """获取技能库。"""
    return get_scenario_or_404(scenario_id).skills


@router.post("/scenarios/{scenario_id}/skills/evolve", response_model=Skill, status_code=201)
def evolve_skill(scenario_id: str, req: EvolveSkillRequest) -> Skill:
    """新增一个「进化技能」：用户手动为业务场景扩展能力。"""
    scenario = get_scenario_or_404(scenario_id)
    if not scenario.skills:
        raise HTTPException(
            status_code=400, detail="请先生成基础技能库，再添加进化技能。"
        )
    skill = Skill(
        skill_id=f"skill_evolved_{uuid.uuid4().hex[:8]}",
        name=req.name.strip(),
        operation="EVOLVED",
        description=req.description.strip(),
        is_evolved=True,
        status="evolved",
    )
    skill = materialize_evolved_skill(scenario, skill)
    scenario.skills.append(skill)
    store.save(scenario)
    return skill
