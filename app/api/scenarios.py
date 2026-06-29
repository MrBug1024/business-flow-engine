"""业务场景管理接口（专用 REST）。"""

from __future__ import annotations

from time import time

from fastapi import APIRouter, HTTPException

from .. import process
from ..models import (
    BusinessProcess,
    CreateScenarioRequest,
    ProcessApprovalRequest,
    Scenario,
    ScenarioStatus,
)
from ..storage import store
from .deps import get_scenario_or_404

router = APIRouter(tags=["scenarios"])


@router.get("/scenarios", response_model=list[Scenario])
def list_scenarios() -> list[Scenario]:
    """列出全部业务场景（按创建时间倒序）。"""
    return store.list()


@router.post("/scenarios", response_model=Scenario, status_code=201)
def create_scenario(req: CreateScenarioRequest) -> Scenario:
    """新建业务场景。"""
    return store.create(name=req.name.strip(), description=req.description.strip())


@router.get("/scenarios/{scenario_id}", response_model=Scenario)
def get_scenario(scenario_id: str) -> Scenario:
    """获取单个业务场景详情。"""
    return get_scenario_or_404(scenario_id)


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(scenario_id: str) -> dict:
    """删除业务场景及其全部数据。"""
    get_scenario_or_404(scenario_id)
    store.delete(scenario_id)
    return {"ok": True, "deleted": scenario_id}


# ===========================================================================
# Phase 0：业务流程文档（生成 / 读取 / 审批）
# ===========================================================================
@router.get("/scenarios/{scenario_id}/business-process", response_model=BusinessProcess)
def get_business_process(scenario_id: str) -> BusinessProcess:
    """读取 Phase 0 业务流程文档（含审批状态）。尚未生成则返回空结构。"""
    scenario = get_scenario_or_404(scenario_id)
    if scenario.business_process is not None:
        return scenario.business_process
    return BusinessProcess()


@router.post("/scenarios/{scenario_id}/business-process", response_model=BusinessProcess)
def draft_business_process(scenario_id: str) -> BusinessProcess:
    """（重新）生成 Phase 0 业务流程文档草稿（确定性，无 AI），状态置为待审批。"""
    scenario = get_scenario_or_404(scenario_id)
    bp = process.discover_process(scenario)
    store.write_business_process(scenario_id, bp.markdown)
    scenario.business_process = bp
    if scenario.status in (ScenarioStatus.CREATED, ScenarioStatus.TABLES_UPLOADED):
        scenario.status = ScenarioStatus.PROCESS_DRAFTED
    store.save(scenario)
    return bp


@router.post("/scenarios/{scenario_id}/business-process/approve",
             response_model=BusinessProcess)
def approve_business_process(scenario_id: str, req: ProcessApprovalRequest) -> BusinessProcess:
    """用户审批 Phase 0 业务流程文档（Gate）。approved=True 放行后续阶段。"""
    scenario = get_scenario_or_404(scenario_id)
    bp = scenario.business_process
    if bp is None:
        raise HTTPException(status_code=400, detail="尚未生成业务流程文档，无法审批。")
    bp.approved = bool(req.approved)
    bp.feedback = req.feedback.strip()
    if bp.approved:
        bp.approved_at = time()
        if scenario.status in (ScenarioStatus.CREATED, ScenarioStatus.TABLES_UPLOADED,
                               ScenarioStatus.PROCESS_DRAFTED):
            scenario.status = ScenarioStatus.PROCESS_APPROVED
    else:
        bp.approved_at = None
        if scenario.status == ScenarioStatus.PROCESS_APPROVED:
            scenario.status = ScenarioStatus.PROCESS_DRAFTED
    scenario.business_process = bp
    store.save(scenario)
    return bp
