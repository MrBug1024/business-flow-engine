"""Studio lifecycle adapter for DeepAgents' standards-based SkillsMiddleware."""

from __future__ import annotations

import re
from typing import Any

from deepagents.middleware.skills import SkillsMiddleware, SkillsState, SkillsStateUpdate
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """Reload Skill metadata for every run so persisted chat threads do not go stale."""

    # This catalog shares the model system-prompt budget with MCP metadata and
    # the evidence-gate context, so full package prose stays on-demand in SKILL.md.
    max_visible_skills = 6
    max_description_characters = 110
    max_path_characters = 200

    def _format_skills_list(self, skills: list[Any]) -> str:
        """Expose a deterministic bounded catalog without suppressing Skill metadata."""

        if not skills:
            return "(No Skills are currently available.)"
        ordered = sorted(
            (item for item in skills if isinstance(item, dict)),
            key=lambda item: (
                0 if _is_primary_executor(item) else 1,
                str(item.get("name") or "").casefold(),
            ),
        )
        visible = ordered[: self.max_visible_skills]
        lines = ["<skill_catalog>"]
        for skill in visible:
            name = _one_line(skill.get("name"), 100)
            description = _one_line(
                skill.get("description"),
                self.max_description_characters,
            )
            path = _one_line(skill.get("path"), self.max_path_characters)
            if not name or not path:
                continue
            lines.append(f"- **{name}**: {description or 'No description provided.'}")
            lines.append(f"  -> Read `{path}` completely with `limit=1000` before use")
        omitted = len(ordered) - len(visible)
        if omitted:
            lines.append(
                f"- {omitted} additional Skills are omitted by the prompt budget; "
                "search them with `discover_studio_capabilities`."
            )
        lines.append("</skill_catalog>")
        return "\n".join(lines)

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


def _one_line(value: Any, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _is_primary_executor(skill: dict[str, Any]) -> bool:
    """Keep a package's one-shot entrypoint visible when the catalog is bounded."""

    name = str(skill.get("name") or "").casefold()
    description = str(skill.get("description") or "").casefold()
    return any(
        marker in name
        for marker in ("-main-executor", "scenario-main", "knowledge_engine", "main_skill")
    ) or "primary_end_to_end_executor" in description or "自包含执行器" in description
