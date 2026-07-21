"""Canonical storage layout and compatibility migration helpers."""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path
from time import time
from typing import Any

from app.core.config import settings


STORAGE_LAYOUT_VERSION = 2
UNASSIGNED_ACCOUNT = "_unassigned"
LEGACY_STUDIO_ROOT = settings.data_path / "business_studio"
LEGACY_SETTINGS_PATH = settings.studio_system_path / "legacy_studio_settings.json"
LEGACY_SKILL_STATE_PATH = settings.studio_system_path / "legacy_installed_skills.json"
LEGACY_ACCOUNT_CLAIM_PATH = settings.studio_system_path / "legacy_account_claim.json"
MIGRATION_REPORT_PATH = settings.system_path / "migrations" / "storage-layout-v2.json"

_SAFE_SCOPE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MIGRATION_LOCK = threading.RLock()
_MIGRATION_COMPLETE = False


def safe_scope(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _SAFE_SCOPE.fullmatch(normalized):
        raise ValueError(f"Invalid {label}.")
    return normalized


def account_business_root(owner_id: str) -> Path:
    return settings.business_accounts_path / safe_scope(owner_id, label="account id")


def account_system_root(owner_id: str) -> Path:
    return settings.studio_users_path / safe_scope(owner_id, label="account id")


def ensure_storage_layout() -> dict[str, Any]:
    """Move legacy system state out of data without overwriting newer targets."""

    global _MIGRATION_COMPLETE
    with _MIGRATION_LOCK:
        if _MIGRATION_COMPLETE:
            return _read_json(MIGRATION_REPORT_PATH)

        previous = _read_json(MIGRATION_REPORT_PATH)
        if previous.get("version") == STORAGE_LAYOUT_VERSION:
            report = previous
            report.setdefault("moved", [])
            report.setdefault("conflicts", [])
        else:
            report = {
                "version": STORAGE_LAYOUT_VERSION,
                "created_at": time(),
                "moved": [],
                "conflicts": [],
            }
        report["last_checked_at"] = time()
        report["status"] = "ready"
        legacy = LEGACY_STUDIO_ROOT
        mappings = [
            (legacy / "runtime", settings.agent_runtime_path),
            (legacy / "system_sandbox", settings.sandbox_root_path),
            (legacy / "studio_settings.json", LEGACY_SETTINGS_PATH),
            (legacy / "installed_skills.json", LEGACY_SKILL_STATE_PATH),
            (
                legacy / "skill_secrets.json",
                settings.studio_system_path / "skill_secrets.json",
            ),
            (
                legacy / "skill_secrets.json.lock",
                settings.studio_system_path / "skill_secrets.json.lock",
            ),
            (
                legacy / "backend.stdout.log",
                settings.system_path / "logs" / "backend.stdout.log",
            ),
            (
                legacy / "backend.stderr.log",
                settings.system_path / "logs" / "backend.stderr.log",
            ),
            (settings.data_path / "dev-system", settings.system_path / "legacy" / "dev-system"),
            (settings.data_path / "scenarios", settings.system_path / "legacy" / "scenarios"),
        ]
        for source, destination in mappings:
            _merge_move(source, destination, report)

        settings.business_accounts_path.mkdir(parents=True, exist_ok=True)
        settings.studio_system_path.mkdir(parents=True, exist_ok=True)
        settings.studio_users_path.mkdir(parents=True, exist_ok=True)
        settings.agent_runtime_path.mkdir(parents=True, exist_ok=True)
        MIGRATION_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_json(MIGRATION_REPORT_PATH, report)
        _MIGRATION_COMPLETE = True
        return report


def claim_legacy_account_state(owner_id: str) -> bool:
    """Assign pre-account Studio settings and managed Skills to one existing account."""

    owner = safe_scope(owner_id, label="account id")
    ensure_storage_layout()
    with _MIGRATION_LOCK:
        payload = _read_json(LEGACY_ACCOUNT_CLAIM_PATH)
        claimed_by = str(payload.get("owner_id") or "")
        if claimed_by:
            return claimed_by == owner
        LEGACY_ACCOUNT_CLAIM_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_json(
            LEGACY_ACCOUNT_CLAIM_PATH,
            {"version": 1, "owner_id": owner, "claimed_at": time()},
        )
        return True


def cleanup_legacy_data_root() -> None:
    """Remove only empty legacy directories after both migration phases finish."""

    for path in (LEGACY_STUDIO_ROOT / "businesses", LEGACY_STUDIO_ROOT):
        try:
            path.rmdir()
        except OSError:
            pass


def _merge_move(source: Path, destination: Path, report: dict[str, Any]) -> None:
    if not source.exists():
        return
    try:
        if source.resolve() == destination.resolve():
            return
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.move(str(source), str(destination))
        report["moved"].append({"from": str(source), "to": str(destination)})
        return
    if source.is_dir() and destination.is_dir():
        for child in list(source.iterdir()):
            _merge_move(child, destination / child.name, report)
        try:
            source.rmdir()
        except OSError:
            pass
        return
    conflict_root = settings.system_path / "migrations" / "conflicts"
    conflict_root.mkdir(parents=True, exist_ok=True)
    conflict_target = conflict_root / source.name
    index = 2
    while conflict_target.exists():
        conflict_target = conflict_root / f"{source.stem}-{index}{source.suffix}"
        index += 1
    shutil.move(str(source), str(conflict_target))
    report["conflicts"].append(
        {
            "from": str(source),
            "existing": str(destination),
            "preserved_at": str(conflict_target),
        }
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "LEGACY_ACCOUNT_CLAIM_PATH",
    "LEGACY_SETTINGS_PATH",
    "LEGACY_SKILL_STATE_PATH",
    "LEGACY_STUDIO_ROOT",
    "MIGRATION_REPORT_PATH",
    "STORAGE_LAYOUT_VERSION",
    "UNASSIGNED_ACCOUNT",
    "account_business_root",
    "account_system_root",
    "claim_legacy_account_state",
    "cleanup_legacy_data_root",
    "ensure_storage_layout",
    "safe_scope",
]
