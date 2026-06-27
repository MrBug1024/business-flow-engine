"""图谱与技能接口（专用 REST）。

关系图谱、流程图谱、技能库的读取，以及「进化技能」的新增，都走这里，
与流式对话接口解耦。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from ..models import (
    EvolveSkillRequest,
    FlowResult,
    RelationResult,
    Skill,
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
