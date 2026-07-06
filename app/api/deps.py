"""接口层公共依赖。"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ..auth.deps import get_current_user
from ..auth.models import PublicUser
from app.domain.models import Scenario
from app.domain.storage import store


def get_scenario_or_404(scenario_id: str) -> Scenario:
    """按 ID 取业务场景，不存在则返回 404（不校验归属，供内部/服务层使用）。"""
    scenario = store.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"业务场景不存在：{scenario_id}")
    return scenario


def get_owned_scenario_or_404(
    scenario_id: str,
    user: PublicUser = Depends(get_current_user),
) -> Scenario:
    """按 ID 取业务场景并校验归属：不存在→404，非本人→403（多租户隔离）。

    历史遗留场景（owner_id 为空）对任何登录用户不可见，需先通过认领端点归属。
    """
    scenario = store.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"业务场景不存在：{scenario_id}")
    if scenario.owner_id != user.id:
        # 不泄露"存在但不属于你"，统一按 404 处理更安全
        raise HTTPException(status_code=404, detail=f"业务场景不存在：{scenario_id}")
    return scenario
