"""Account-scoped Studio settings stored with system state."""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path

from app.core.config import settings as env_settings
from app.core.storage_layout import (
    LEGACY_SETTINGS_PATH,
    account_system_root,
    claim_legacy_account_state,
    ensure_storage_layout,
    safe_scope,
)
from app.studio.capabilities.mcp import (
    merge_masked_mcp_configs,
    normalize_stored_mcp_configs,
    public_mcp_configs,
)
from app.studio.models import AIModelConfig, StudioSettings, UpdateStudioSettings
from app.studio.capabilities.registry import list_skills
from app.studio.capabilities.tools import tool_registry


MODEL_SECRET_MASK = "********"


class StudioSettingsStore:
    def __init__(self, root: Path | None = None) -> None:
        ensure_storage_layout()
        self.root = root or env_settings.studio_users_path
        self._uses_default_root = root is None
        self._lock = threading.RLock()

    def path_for(self, owner_id: str) -> Path:
        owner = safe_scope(owner_id, label="account id")
        root = account_system_root(owner) if self._uses_default_root else self.root / owner
        return root / "studio_settings.json"

    def load(self, owner_id: str | None = None) -> StudioSettings:
        with self._lock:
            if owner_id is None:
                return _normalize_settings(_default_settings(None), None)
            path = self.path_for(owner_id)
            self._claim_legacy_settings(owner_id, path)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                loaded = StudioSettings.model_validate(data)
            else:
                loaded = _default_settings(owner_id)
            normalized = _normalize_settings(loaded, owner_id)
            if normalized != loaded or not path.exists():
                self.save(normalized, owner_id)
            return normalized

    def save(self, value: StudioSettings, owner_id: str) -> StudioSettings:
        with self._lock:
            path = self.path_for(owner_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
            return value

    def update(self, patch: UpdateStudioSettings, owner_id: str) -> StudioSettings:
        current = self.load(owner_id)
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
        updated = _normalize_settings(StudioSettings.model_validate(data), owner_id)
        return self.save(updated, owner_id)

    def public(
        self,
        value: StudioSettings | None = None,
        owner_id: str | None = None,
    ) -> StudioSettings:
        current = value or self.load(owner_id)
        data = current.model_dump(mode="json")
        for model in data["configured_models"]:
            if model.get("api_key"):
                model["api_key"] = MODEL_SECRET_MASK
        data["mcp_configs"] = public_mcp_configs(current.mcp_configs)
        return StudioSettings.model_validate(data)

    def upsert_mcp_configs(self, entries: list[dict], owner_id: str) -> StudioSettings:
        with self._lock:
            current = self.load(owner_id)
            by_name = {str(item.get("name")): item for item in current.mcp_configs}
            order = [str(item.get("name")) for item in current.mcp_configs]
            for entry in entries:
                name = str(entry.get("name") or "")
                if name not in by_name:
                    order.append(name)
                by_name[name] = entry
            data = current.model_dump(mode="json")
            data["mcp_configs"] = [by_name[name] for name in order]
            return self.save(
                _normalize_settings(StudioSettings.model_validate(data), owner_id),
                owner_id,
            )

    def set_mcp_enabled(
        self,
        name: str,
        enabled: bool,
        owner_id: str,
    ) -> StudioSettings | None:
        with self._lock:
            current = self.load(owner_id)
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
            return self.save(
                _normalize_settings(StudioSettings.model_validate(data), owner_id),
                owner_id,
            )

    def delete_mcp_config(self, name: str, owner_id: str) -> StudioSettings | None:
        with self._lock:
            current = self.load(owner_id)
            entries = [item for item in current.mcp_configs if str(item.get("name")) != name]
            if len(entries) == len(current.mcp_configs):
                return None
            data = current.model_dump(mode="json")
            data["mcp_configs"] = entries
            return self.save(
                _normalize_settings(StudioSettings.model_validate(data), owner_id),
                owner_id,
            )

    def delete_model(self, model_id: str, owner_id: str) -> StudioSettings | None:
        with self._lock:
            current = self.load(owner_id)
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
            return self.save(
                _normalize_settings(StudioSettings.model_validate(data), owner_id),
                owner_id,
            )

    def active_model_name(
        self,
        requested: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> str:
        return self.active_model_config(requested, owner_id=owner_id).model

    def active_model_config(
        self,
        requested: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> AIModelConfig:
        current = self.load(owner_id)
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

    def _claim_legacy_settings(self, owner_id: str, destination: Path) -> None:
        if (
            not self._uses_default_root
            or destination.exists()
            or not LEGACY_SETTINGS_PATH.is_file()
            or not claim_legacy_account_state(owner_id)
        ):
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_SETTINGS_PATH, destination)


def _default_settings(owner_id: str | None) -> StudioSettings:
    env_model = _env_model()
    return StudioSettings(
        active_model=env_model.model,
        configured_models=[env_model],
        installed_tools=[tool.name for tool in tool_registry.list() if tool.mounted],
        installed_skills=[skill.name for skill in list_skills(owner_id) if skill.enabled],
        mcp_configs=[],
    )


def _normalize_settings(value: StudioSettings, owner_id: str | None) -> StudioSettings:
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

    installed_skills = sorted(skill.name for skill in list_skills(owner_id) if skill.enabled)

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
