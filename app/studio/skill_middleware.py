"""Studio lifecycle adapter for DeepAgents' standards-based SkillsMiddleware."""

from __future__ import annotations

from typing import Any

from deepagents.middleware.skills import SkillsMiddleware, SkillsState, SkillsStateUpdate
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """Reload Skill metadata for every run so persisted chat threads do not go stale."""

    @staticmethod
    def _without_cached_skills(state: Any) -> dict[str, Any]:
        current = dict(state)
        current.pop("skills_metadata", None)
        current.pop("skills_load_errors", None)
        return current

    def before_agent(
        self,
        state: SkillsState,
        runtime: Runtime,
        config: RunnableConfig,
    ) -> SkillsStateUpdate | None:
        update = super().before_agent(self._without_cached_skills(state), runtime, config)
        if update is not None and "skills_load_errors" not in update:
            update["skills_load_errors"] = []
        return update

    async def abefore_agent(
        self,
        state: SkillsState,
        runtime: Runtime,
        config: RunnableConfig,
    ) -> SkillsStateUpdate | None:
        update = await super().abefore_agent(
            self._without_cached_skills(state),
            runtime,
            config,
        )
        if update is not None and "skills_load_errors" not in update:
            update["skills_load_errors"] = []
        return update


__all__ = ["ReloadingSkillsMiddleware"]
