"""业务场景管理接口（v1.0.3：移除 Phase 0 审批接口）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..models import CreateScenarioRequest, Scenario
from ..storage import store
from .deps import get_scenario_or_404

router = APIRouter(tags=["scenarios"])


@router.get("/scenarios", response_model=list[Scenario])
def list_scenarios() -> list[Scenario]:
    return store.list()


@router.post("/scenarios", response_model=Scenario, status_code=201)
def create_scenario(req: CreateScenarioRequest) -> Scenario:
    return store.create(name=req.name.strip(), description=req.description.strip())


@router.get("/scenarios/{scenario_id}", response_model=Scenario)
def get_scenario(scenario_id: str) -> Scenario:
    return get_scenario_or_404(scenario_id)


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(scenario_id: str) -> dict:
    get_scenario_or_404(scenario_id)
    store.delete(scenario_id)
    return {"ok": True, "deleted": scenario_id}
