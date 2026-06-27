"""接口层公共依赖。"""

from __future__ import annotations

from fastapi import HTTPException

from ..models import Scenario
from ..storage import store


def get_scenario_or_404(scenario_id: str) -> Scenario:
    """按 ID 取业务场景，不存在则返回 404。"""
    scenario = store.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"业务场景不存在：{scenario_id}")
    return scenario
