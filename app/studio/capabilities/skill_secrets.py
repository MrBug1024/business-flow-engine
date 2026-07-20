"""Private, Skill-scoped runtime secrets.

Skill directories are portable model-readable artifacts. Secrets therefore live
outside those directories and are injected only for commands that explicitly
reference the owning Skill package.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from app.core.config import settings
from app.studio.capabilities.registry import SYSTEM_SKILLS_ROOT


_STORE_VERSION = 1
_SKILL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._-])/skills/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?![A-Za-z0-9._-])"
)
_LEGACY_PACKAGE_FIELDS = {
    "ocr-parser": {
        "OCR_API_KEY": ("config/defaults.json", "OCR_API_KEY"),
    },
    "vector-kb": {
        "VECTOR_KB_API_KEY": ("config/defaults.json", "api_key"),
    },
}


class SkillSecretStore:
    """Persist private values by Skill and expose the smallest required scope."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        skills_root: Path | None = None,
        skill_environment_keys: Mapping[str, Iterable[str]] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.path = path or (
            settings.data_path / "business_studio" / "skill_secrets.json"
        )
        self.skills_root = skills_root or SYSTEM_SKILLS_ROOT
        source_keys = (
            skill_environment_keys
            if skill_environment_keys is not None
            else settings.sandbox_skill_environment_keys
        )
        self.skill_environment_keys = {
            str(skill_name).strip(): tuple(
                sorted(
                    {
                        str(environment_key).strip()
                        for environment_key in environment_keys
                        if str(environment_key).strip()
                    }
                )
            )
            for skill_name, environment_keys in source_keys.items()
            if str(skill_name).strip()
        }
        self.environment = environment if environment is not None else os.environ
        self._lock = threading.RLock()
        self.migrate_legacy_secrets()

    def sandbox_environment(
        self,
        command: str | None = None,
        *,
        skill_names: Iterable[str] | str | None = None,
    ) -> dict[str, str]:
        """Return variables owned only by Skills explicitly used by this command."""

        requested_skills = self._requested_skills(command, skill_names)
        if not requested_skills:
            return {}

        with self._lock, _interprocess_lock(self.path):
            stored, _ = self._decode_store(_read_json_object(self.path))
            result: dict[str, str] = {}
            conflicted_keys: set[str] = set()
            for skill_name in requested_skills:
                skill_values = stored.get(skill_name, {})
                for environment_key in self.skill_environment_keys[skill_name]:
                    value = self.environment.get(
                        environment_key,
                        skill_values.get(environment_key, ""),
                    )
                    value = str(value).strip() if value is not None else ""
                    if not value or environment_key in conflicted_keys:
                        continue
                    if (
                        environment_key in result
                        and result[environment_key] != value
                    ):
                        result.pop(environment_key, None)
                        conflicted_keys.add(environment_key)
                        continue
                    result[environment_key] = value
            return result

    def migrate_legacy_secrets(self) -> None:
        """Atomically migrate flat/package secrets before clearing package files."""

        with self._lock, _interprocess_lock(self.path):
            raw = _read_json_object(self.path)
            values, canonical = self._decode_store(raw)
            package_updates: list[tuple[Path, dict[str, Any]]] = []

            for skill_name, fields in _LEGACY_PACKAGE_FIELDS.items():
                for environment_key, (relative_path, json_key) in fields.items():
                    package_path = self.skills_root / skill_name / relative_path
                    payload = _read_json_object(package_path)
                    legacy_value = str(payload.get(json_key) or "").strip()
                    if not legacy_value:
                        continue
                    skill_values = values.setdefault(skill_name, {})
                    skill_values.setdefault(environment_key, legacy_value)
                    payload[json_key] = ""
                    package_updates.append((package_path, payload))

            should_save = bool(package_updates) or bool(raw) and not canonical
            if should_save:
                self._save(values)

            # The private copy must be durable before model-readable files are cleared.
            for package_path, payload in package_updates:
                _atomic_write_json(package_path, payload)

    def _requested_skills(
        self,
        command: str | None,
        skill_names: Iterable[str] | str | None,
    ) -> tuple[str, ...]:
        requested: set[str] = set()
        if command:
            requested.update(
                match.group("name") for match in _SKILL_PATH_PATTERN.finditer(command)
            )
        if isinstance(skill_names, str):
            requested.add(skill_names)
        elif skill_names is not None:
            requested.update(str(skill_name) for skill_name in skill_names)
        return tuple(sorted(requested.intersection(self.skill_environment_keys)))

    def _decode_store(
        self,
        raw: Mapping[str, Any],
    ) -> tuple[dict[str, dict[str, str]], bool]:
        canonical = raw.get("version") == _STORE_VERSION and isinstance(
            raw.get("skills"), dict
        )
        if canonical:
            return _normalize_skill_values(raw["skills"]), True

        if raw and all(isinstance(value, dict) for value in raw.values()):
            return _normalize_skill_values(raw), False

        values: dict[str, dict[str, str]] = {}
        for environment_key, value in raw.items():
            if not isinstance(environment_key, str) or not isinstance(value, str):
                continue
            clean_value = value.strip()
            if not clean_value:
                continue
            for skill_name, allowed_keys in self.skill_environment_keys.items():
                if environment_key in allowed_keys:
                    values.setdefault(skill_name, {})[environment_key] = clean_value
        return values, False

    def _save(self, values: Mapping[str, Mapping[str, str]]) -> None:
        payload = {
            "version": _STORE_VERSION,
            "skills": {
                skill_name: dict(sorted(skill_values.items()))
                for skill_name, skill_values in sorted(values.items())
                if skill_values
            },
        }
        _atomic_write_json(self.path, payload, mode=0o600)


def _normalize_skill_values(raw: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for skill_name, raw_values in raw.items():
        if not isinstance(skill_name, str) or not isinstance(raw_values, dict):
            continue
        values = {
            environment_key: value.strip()
            for environment_key, value in raw_values.items()
            if isinstance(environment_key, str)
            and isinstance(value, str)
            and value.strip()
        }
        if values:
            result[skill_name] = values
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    mode: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target_mode = mode
    if target_mode is None and path.exists():
        try:
            target_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            target_mode = None
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if target_mode is not None:
            temporary_path.chmod(target_mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _interprocess_lock(target: Path):
    lock_path = target.with_suffix(f"{target.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        _lock_file(handle)
        try:
            yield
        finally:
            _unlock_file(handle)


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


skill_secret_store = SkillSecretStore()
