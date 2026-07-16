"""Studio settings persisted beside workspace data."""

from __future__ import annotations

import json
import re
import threading

from app.core.config import settings as env_settings
from app.studio.mcp_runtime import (
    merge_masked_mcp_configs,
    normalize_stored_mcp_configs,
    public_mcp_configs,
)
from app.studio.models import AIModelConfig, StudioSettings, UpdateStudioSettings
from app.studio.registry import list_skills
from app.studio.tool_registry import tool_registry


MODEL_SECRET_MASK = "********"


class StudioSettingsStore:
    def __init__(self) -> None:
        self.root = env_settings.data_path / "business_studio"
        self.path = self.root / "studio_settings.json"
        self._lock = threading.RLock()

    def load(self) -> StudioSettings:
        with self._lock:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                loaded = StudioSettings.model_validate(data)
            else:
                loaded = _default_settings()
            normalized = _normalize_settings(loaded)
            if normalized != loaded or not self.path.exists():
                self.save(normalized)
            return normalized

    def save(self, value: StudioSettings) -> StudioSettings:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
            return value

    def update(self, patch: UpdateStudioSettings) -> StudioSettings:
        current = self.load()
        data = current.model_dump(mode="json")
        incoming = patch.model_dump(exclude_unset=True, mode="json")
        if isinstance(incoming.get("configured_models"), list):
            incoming["configured_models"] = _merge_model_secrets(
                incoming["configured_models"],
                current.configured_models,
            )
        if isinstance(incoming.get("mcp_configs"), list):
            incoming["mcp_configs"] = merge_masked_mcp_configs(
                incoming["mcp_configs"],
                current.mcp_configs,
            )
        data.update(incoming)
        updated = _normalize_settings(StudioSettings.model_validate(data))
        return self.save(updated)

    def public(self, value: StudioSettings | None = None) -> StudioSettings:
        current = value or self.load()
        data = current.model_dump(mode="json")
        for model in data["configured_models"]:
            if model.get("api_key"):
                model["api_key"] = MODEL_SECRET_MASK
        data["mcp_configs"] = public_mcp_configs(current.mcp_configs)
        return StudioSettings.model_validate(data)

    def upsert_mcp_configs(self, entries: list[dict]) -> StudioSettings:
        with self._lock:
            current = self.load()
            by_name = {str(item.get("name")): item for item in current.mcp_configs}
            order = [str(item.get("name")) for item in current.mcp_configs]
            for entry in entries:
                name = str(entry.get("name") or "")
                if name not in by_name:
                    order.append(name)
                by_name[name] = entry
            data = current.model_dump(mode="json")
            data["mcp_configs"] = [by_name[name] for name in order]
            return self.save(_normalize_settings(StudioSettings.model_validate(data)))

    def set_mcp_enabled(self, name: str, enabled: bool) -> StudioSettings | None:
        with self._lock:
            current = self.load()
            found = False
            entries: list[dict] = []
            for entry in current.mcp_configs:
                updated = dict(entry)
                if str(updated.get("name")) == name:
                    updated["enabled"] = enabled
                    found = True
                entries.append(updated)
            if not found:
                return None
            data = current.model_dump(mode="json")
            data["mcp_configs"] = entries
            return self.save(_normalize_settings(StudioSettings.model_validate(data)))

    def delete_mcp_config(self, name: str) -> StudioSettings | None:
        with self._lock:
            current = self.load()
            entries = [item for item in current.mcp_configs if str(item.get("name")) != name]
            if len(entries) == len(current.mcp_configs):
                return None
            data = current.model_dump(mode="json")
            data["mcp_configs"] = entries
            return self.save(_normalize_settings(StudioSettings.model_validate(data)))

    def delete_model(self, model_id: str) -> StudioSettings | None:
        with self._lock:
            current = self.load()
            match = next((model for model in current.configured_models if model.id == model_id), None)
            if match is None:
                return None
            if match.default:
                raise ValueError("The default environment model cannot be deleted.")
            data = current.model_dump(mode="json")
            data["configured_models"] = [
                model.model_dump(mode="json")
                for model in current.configured_models
                if model.id != model_id
            ]
            if current.active_model == match.model:
                data["active_model"] = ""
            return self.save(_normalize_settings(StudioSettings.model_validate(data)))

    def active_model_name(self, requested: str | None = None) -> str:
        return self.active_model_config(requested).model

    def active_model_config(self, requested: str | None = None) -> AIModelConfig:
        current = self.load()
        if requested:
            match = next(
                (model for model in current.configured_models if model.enabled and requested in {model.model, model.id}),
                None,
            )
            if match is not None:
                return match
        if any(model.model == current.active_model and model.enabled for model in current.configured_models):
            return next(model for model in current.configured_models if model.model == current.active_model and model.enabled)
        return _env_model()


def _default_settings() -> StudioSettings:
    env_model = _env_model()
    return StudioSettings(
        active_model=env_model.model,
        configured_models=[env_model],
        installed_tools=[tool.name for tool in tool_registry.list() if tool.mounted],
        installed_skills=[skill.name for skill in list_skills() if skill.enabled],
        mcp_configs=[],
    )


def _normalize_settings(value: StudioSettings) -> StudioSettings:
    env_model = _env_model()
    models: list[AIModelConfig] = [env_model]
    seen_models: set[str] = {env_model.model}
    seen_ids: set[str] = {env_model.id}
    for model in value.configured_models:
        if model.model in seen_models or model.id in seen_ids:
            continue
        seen_models.add(model.model)
        seen_ids.add(model.id)
        models.append(model.model_copy(update={"default": False}))
    enabled_models = [model for model in models if model.enabled]
    active_model = value.active_model
    if not any(model.model == active_model and model.enabled for model in models):
        active_model = (enabled_models[0] if enabled_models else models[0]).model
    actual_tools = {tool.name for tool in tool_registry.list() if tool.mounted}
    installed_tools = sorted(actual_tools)

    installed_skills = sorted(skill.name for skill in list_skills() if skill.enabled)

    return StudioSettings(
        active_model=active_model,
        configured_models=models,
        installed_tools=installed_tools,
        installed_skills=installed_skills,
        mcp_configs=normalize_stored_mcp_configs(value.mcp_configs),
    )


def _env_model() -> AIModelConfig:
    model = env_settings.env_model_name
    return AIModelConfig(
        id=_slug(model),
        name=model,
        provider="openai-compatible",
        model=model,
        base_url=env_settings.openai_base_url,
        api_key="",
        enabled=True,
        default=True,
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower() or "env_model"


def _merge_model_secrets(
    incoming: list[dict],
    existing: list[AIModelConfig],
) -> list[dict]:
    by_id = {model.id: model for model in existing}
    merged: list[dict] = []
    for raw in incoming:
        candidate = dict(raw)
        model_id = str(candidate.get("id") or "")
        if candidate.get("api_key") == MODEL_SECRET_MASK:
            previous = by_id.get(model_id)
            if previous is None:
                raise ValueError(f"Cannot restore the masked API key for unknown model {model_id!r}.")
            candidate["api_key"] = previous.api_key
        merged.append(candidate)
    return merged


studio_settings = StudioSettingsStore()
