"""业务场景管理接口（多租户：每个用户只见/只管自己的场景）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth.deps import get_current_user
from ..auth.models import PublicUser
from ..models import CreateScenarioRequest, Scenario
from ..storage import store
from .deps import get_owned_scenario_or_404

router = APIRouter(tags=["scenarios"])


@router.get("/scenarios", response_model=list[Scenario])
def list_scenarios(user: PublicUser = Depends(get_current_user)) -> list[Scenario]:
    return store.list(owner_id=user.id)


@router.post("/scenarios", response_model=Scenario, status_code=201)
def create_scenario(
    req: CreateScenarioRequest, user: PublicUser = Depends(get_current_user)
) -> Scenario:
    return store.create(
        name=req.name.strip(), description=req.description.strip(), owner_id=user.id
    )


@router.get("/scenarios/{scenario_id}", response_model=Scenario)
def get_scenario(scenario: Scenario = Depends(get_owned_scenario_or_404)) -> Scenario:
    return scenario


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(scenario: Scenario = Depends(get_owned_scenario_or_404)) -> dict:
    store.delete(scenario.id)
    return {"ok": True, "deleted": scenario.id}


@router.post("/scenarios/{scenario_id}/claim", response_model=Scenario)
def claim_scenario(scenario_id: str, user: PublicUser = Depends(get_current_user)) -> Scenario:
    """认领一个历史遗留（owner_id 为空）的场景。已归属他人的不可认领。"""
    scenario = store.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"业务场景不存在：{scenario_id}")
    if scenario.owner_id and scenario.owner_id != user.id:
        raise HTTPException(status_code=403, detail="该场景已归属其他用户，无法认领")
    scenario.owner_id = user.id
    return store.save(scenario)
