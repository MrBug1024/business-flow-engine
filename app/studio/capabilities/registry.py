"""Filesystem registry for project and Studio-installed Skills."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import threading
import uuid
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from time import time

import yaml

from app.core.config import PROJECT_ROOT, settings
from app.core.storage_layout import (
    LEGACY_SKILL_STATE_PATH,
    account_system_root,
    claim_legacy_account_state,
    ensure_storage_layout,
    safe_scope,
)
from app.studio.models import SkillDefinition


SYSTEM_SKILLS_ROOT = PROJECT_ROOT / "system_skills"
STUDIO_SKILL_STATE_PATH = LEGACY_SKILL_STATE_PATH
STUDIO_SKILL_STATE_SCHEMA = 1
STUDIO_SKILL_MANAGER = "ai-business-studio"
_LEGACY_STUDIO_SKILL_MARKER = ".studio-skill.json"
_OWNERSHIP_LOCK = threading.RLock()
_VIEW_LOCKS: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)


def user_skills_root(owner_id: str) -> Path:
    return account_system_root(owner_id) / "skills"


def managed_skill_state_path(owner_id: str) -> Path:
    return account_system_root(owner_id) / "installed_skills.json"


@lru_cache(maxsize=128)
def list_skills(owner_id: str | None = None) -> list[SkillDefinition]:
    """Return system Skills plus only the requested account's installed Skills."""

    ensure_storage_layout()
    if owner_id is not None:
        _claim_legacy_user_skills(owner_id)
    skills: list[SkillDefinition] = []
    seen: set[str] = set()
    for kind, skill_dir in iter_skill_directories(owner_id):
        skill_file = skill_dir / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        frontmatter = _parse_frontmatter(text)
        name = str(frontmatter.get("name") or skill_dir.name)
        normalized_name = name.strip()
        if not normalized_name or normalized_name in seen:
            continue
        seen.add(normalized_name)
        description = str(frontmatter.get("description") or _first_paragraph(text) or name)
        metadata = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
        resources = _skill_resources(skill_dir)
        skills.append(
            SkillDefinition(
                name=normalized_name,
                description=_clean_description(description),
                kind=kind,
                version=str(frontmatter.get("version") or metadata.get("version") or "1.0.0"),
                locked=kind == "system",
                enabled=True,
                dependencies=[],
                compatibility=str(frontmatter.get("compatibility") or ""),
                digest=skill_content_digest(skill_dir),
                location=f"/skills/{skill_dir.name}/SKILL.md",
                resources=resources,
            )
        )
    return skills


def iter_skill_directories(owner_id: str | None = None) -> list[tuple[str, Path]]:
    directories: list[tuple[str, Path]] = []
    roots: list[tuple[str, Path]] = [("system", SYSTEM_SKILLS_ROOT)]
    if owner_id is not None:
        roots.append(("user", user_skills_root(owner_id)))
    for kind, root in roots:
        if not root.exists():
            continue
        resolved_root = root.resolve()
        for item in sorted(root.iterdir(), key=lambda path: path.name.lower()):
            if item.name.startswith(".") or item.is_symlink() or _is_junction(item):
                continue
            if not item.is_dir() or not (item / "SKILL.md").is_file():
                continue
            try:
                if item.resolve().parent != resolved_root:
                    continue
            except OSError:
                continue
            directories.append((kind, item))
    # A manually added duplicate cannot shadow a project-bundled Skill.
    return sorted(directories, key=lambda entry: (entry[0] != "system", entry[1].name.lower()))


def find_skill_directory(name: str, owner_id: str | None = None) -> Path | None:
    for definition in list_skills(owner_id):
        if definition.name != name:
            continue
        for kind, directory in iter_skill_directories(owner_id):
            if kind != definition.kind:
                continue
            text = (directory / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            if (_frontmatter_value(text, "name") or directory.name).strip() == name:
                return directory
    return None


def clear_skill_registry_cache() -> None:
    list_skills.cache_clear()


def is_studio_managed_skill_directory(directory: Path, owner_id: str) -> bool:
    """Return whether Studio owns this directory without modifying the Skill package."""

    record = _managed_skill_records(owner_id).get(directory.name)
    return bool(
        isinstance(record, dict)
        and record.get("managed_by") == STUDIO_SKILL_MANAGER
        and record.get("directory") == directory.name
    )


def record_studio_managed_skill(name: str, owner_id: str, *, source: str) -> None:
    """Persist installation ownership outside the portable Skill directory."""

    with _OWNERSHIP_LOCK:
        records = _managed_skill_records(owner_id)
        records[name] = {
            "managed_by": STUDIO_SKILL_MANAGER,
            "directory": name,
            "source": source,
            "installed_at": time(),
        }
        _write_managed_skill_records(records, owner_id)


def forget_studio_managed_skill(name: str, owner_id: str) -> None:
    with _OWNERSHIP_LOCK:
        records = _managed_skill_records(owner_id)
        if records.pop(name, None) is not None:
            _write_managed_skill_records(records, owner_id)


def managed_skill_record(name: str, owner_id: str) -> dict[str, object] | None:
    record = _managed_skill_records(owner_id).get(name)
    return dict(record) if isinstance(record, dict) else None


def _managed_skill_records(owner_id: str) -> dict[str, dict[str, object]]:
    path = managed_skill_state_path(owner_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != STUDIO_SKILL_STATE_SCHEMA:
        return {}
    raw_records = payload.get("skills")
    if not isinstance(raw_records, dict):
        return {}
    return {
        str(name): dict(record)
        for name, record in raw_records.items()
        if isinstance(name, str) and isinstance(record, dict)
    }


def _write_managed_skill_records(
    records: dict[str, dict[str, object]],
    owner_id: str,
) -> None:
    path = managed_skill_state_path(owner_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            {"schema_version": STUDIO_SKILL_STATE_SCHEMA, "skills": records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _migrate_legacy_ownership_marker(directory: Path, owner_id: str) -> None:
    marker = directory / _LEGACY_STUDIO_SKILL_MARKER
    if marker.is_symlink() or not marker.is_file():
        return
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not (
        isinstance(payload, dict)
        and payload.get("managed_by") == STUDIO_SKILL_MANAGER
        and payload.get("skill_name") == directory.name
    ):
        return
    with _OWNERSHIP_LOCK:
        records = _managed_skill_records(owner_id)
        records[directory.name] = {
            "managed_by": STUDIO_SKILL_MANAGER,
            "directory": directory.name,
            "source": str(payload.get("source") or "legacy"),
            "installed_at": float(payload.get("installed_at") or time()),
        }
        _write_managed_skill_records(records, owner_id)
        marker.unlink(missing_ok=True)


def _claim_legacy_user_skills(owner_id: str) -> None:
    """Move pre-account Studio-installed Skills to the legacy settings owner."""

    owner = safe_scope(owner_id, label="account id")
    if not LEGACY_SKILL_STATE_PATH.is_file() or not claim_legacy_account_state(owner):
        return
    try:
        payload = json.loads(LEGACY_SKILL_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    raw_records = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(raw_records, dict):
        return
    destination_root = user_skills_root(owner)
    records = _managed_skill_records(owner)
    changed = False
    for raw_name, raw_record in raw_records.items():
        if not isinstance(raw_name, str) or not isinstance(raw_record, dict):
            continue
        try:
            name = safe_scope(raw_name, label="Skill name")
        except ValueError:
            continue
        if (
            raw_record.get("managed_by") != STUDIO_SKILL_MANAGER
            or raw_record.get("directory") != name
        ):
            continue
        source = SYSTEM_SKILLS_ROOT / name
        destination = destination_root / name
        if source.is_dir() and not destination.exists():
            destination_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        if destination.is_dir():
            records[name] = dict(raw_record)
            changed = True
            _migrate_legacy_ownership_marker(destination, owner)
    if changed:
        _write_managed_skill_records(records, owner)


def materialize_skill_view(owner_id: str) -> Path:
    """Build an account-only runtime view from shared system and private user Skills."""

    owner = safe_scope(owner_id, label="account id")
    definitions = list_skills(owner)
    desired = {
        skill.name: {"digest": skill.digest, "kind": skill.kind}
        for skill in definitions
    }
    view_root = settings.skill_views_path / owner
    manifest_path = view_root / ".manifest.json"
    with _VIEW_LOCKS[owner]:
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            current = {}
        if current.get("skills") == desired and all(
            (view_root / name / "SKILL.md").is_file() for name in desired
        ):
            return view_root

        settings.skill_views_path.mkdir(parents=True, exist_ok=True)
        staging = settings.skill_views_path / f".{owner}-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            for skill in definitions:
                source = find_skill_directory(skill.name, owner)
                if source is None:
                    raise FileNotFoundError(f"Skill source not found: {skill.name}")
                destination_name = safe_scope(skill.name, label="Skill name")
                shutil.copytree(
                    source,
                    staging / destination_name,
                    ignore=shutil.ignore_patterns(
                        "__pycache__",
                        "*.pyc",
                        "*.pyo",
                        _LEGACY_STUDIO_SKILL_MARKER,
                    ),
                )
            (staging / ".manifest.json").write_text(
                json.dumps(
                    {"version": 1, "owner_id": owner, "skills": desired},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            if view_root.exists():
                shutil.rmtree(view_root)
            staging.replace(view_root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return view_root


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except OSError:
        return True


def installed_skill_names(owner_id: str | None = None) -> set[str]:
    return {skill.name for skill in list_skills(owner_id)}


def skill_content_digest(directory: Path) -> str:
    """Return an immutable identity for the portable contents of a Skill package."""

    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(directory)
        if (
            _LEGACY_STUDIO_SKILL_MARKER in relative.parts
            or "__pycache__" in relative.parts
            or path.suffix.casefold() in {".pyc", ".pyo"}
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _skill_resources(directory: Path) -> list[str]:
    resources: list[str] = []
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(directory)
        if (
            relative.as_posix() in {"SKILL.md", _LEGACY_STUDIO_SKILL_MARKER}
            or "__pycache__" in relative.parts
            or path.suffix.casefold() in {".pyc", ".pyo"}
        ):
            continue
        resources.append(relative.as_posix())
    return resources


def _parse_frontmatter(text: str) -> dict[str, object]:
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if match is None:
        return {}
    try:
        value = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _frontmatter_value(text: str, key: str) -> str:
    structured = _parse_frontmatter(text).get(key)
    if structured is not None and not isinstance(structured, (dict, list)):
        return str(structured).strip()
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+)$", text)
    if not match:
        return ""
    raw = match.group(1).strip()
    if raw in {">", "|"}:
        lines: list[str] = []
        for line in text[match.end() :].splitlines():
            if not lines and not line.strip():
                continue
            if not line.startswith((" ", "\t")):
                break
            lines.append(line.strip())
        return (" " if raw == ">" else "\n").join(lines).strip()
    return raw.strip("\"'")


def _first_paragraph(text: str) -> str:
    body = re.sub(r"(?s)^---.*?---", "", text).strip()
    for block in re.split(r"\n\s*\n", body):
        cleaned = re.sub(r"^#+\s*", "", block.strip())
        if cleaned:
            return cleaned
    return ""


def _clean_description(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value[:500]
