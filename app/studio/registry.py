"""Filesystem registry for project and Studio-installed Skills."""

from __future__ import annotations

import json
import hashlib
import re
import threading
from functools import lru_cache
from pathlib import Path
from time import time

import yaml

from app.core.config import PROJECT_ROOT, settings
from app.studio.models import SkillDefinition


SYSTEM_SKILLS_ROOT = PROJECT_ROOT / "system_skills"
STUDIO_SKILL_STATE_PATH = settings.data_path / "business_studio" / "installed_skills.json"
STUDIO_SKILL_STATE_SCHEMA = 1
STUDIO_SKILL_MANAGER = "ai-business-studio"
_LEGACY_STUDIO_SKILL_MARKER = ".studio-skill.json"
_OWNERSHIP_LOCK = threading.RLock()


@lru_cache(maxsize=1)
def list_skills() -> list[SkillDefinition]:
    """Return all skills from the unified project-level Skill store."""

    skills: list[SkillDefinition] = []
    seen: set[str] = set()
    for kind, skill_dir in iter_skill_directories():
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


def iter_skill_directories() -> list[tuple[str, Path]]:
    root = SYSTEM_SKILLS_ROOT
    if not root.exists():
        return []
    resolved_root = root.resolve()
    directories: list[tuple[str, Path]] = []
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
        kind = "user" if is_studio_managed_skill_directory(item) else "system"
        directories.append((kind, item))
    # A manually added duplicate cannot shadow a project-bundled Skill.
    return sorted(directories, key=lambda entry: (entry[0] != "system", entry[1].name.lower()))


def find_skill_directory(name: str) -> Path | None:
    for definition in list_skills():
        if definition.name != name:
            continue
        for kind, directory in iter_skill_directories():
            if kind != definition.kind:
                continue
            text = (directory / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            if (_frontmatter_value(text, "name") or directory.name).strip() == name:
                return directory
    return None


def clear_skill_registry_cache() -> None:
    list_skills.cache_clear()


def is_studio_managed_skill_directory(directory: Path) -> bool:
    """Return whether Studio owns this directory without modifying the Skill package."""

    _migrate_legacy_ownership_marker(directory)
    record = _managed_skill_records().get(directory.name)
    return bool(
        isinstance(record, dict)
        and record.get("managed_by") == STUDIO_SKILL_MANAGER
        and record.get("directory") == directory.name
    )


def record_studio_managed_skill(name: str, *, source: str) -> None:
    """Persist installation ownership outside the portable Skill directory."""

    with _OWNERSHIP_LOCK:
        records = _managed_skill_records()
        records[name] = {
            "managed_by": STUDIO_SKILL_MANAGER,
            "directory": name,
            "source": source,
            "installed_at": time(),
        }
        _write_managed_skill_records(records)


def forget_studio_managed_skill(name: str) -> None:
    with _OWNERSHIP_LOCK:
        records = _managed_skill_records()
        if records.pop(name, None) is not None:
            _write_managed_skill_records(records)


def managed_skill_record(name: str) -> dict[str, object] | None:
    record = _managed_skill_records().get(name)
    return dict(record) if isinstance(record, dict) else None


def _managed_skill_records() -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(STUDIO_SKILL_STATE_PATH.read_text(encoding="utf-8"))
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


def _write_managed_skill_records(records: dict[str, dict[str, object]]) -> None:
    path = STUDIO_SKILL_STATE_PATH
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


def _migrate_legacy_ownership_marker(directory: Path) -> None:
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
        records = _managed_skill_records()
        records[directory.name] = {
            "managed_by": STUDIO_SKILL_MANAGER,
            "directory": directory.name,
            "source": str(payload.get("source") or "legacy"),
            "installed_at": float(payload.get("installed_at") or time()),
        }
        _write_managed_skill_records(records)
        marker.unlink(missing_ok=True)


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except OSError:
        return True


def installed_skill_names() -> set[str]:
    return {skill.name for skill in list_skills()}


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
