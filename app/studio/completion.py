"""Filesystem-backed completion validation for artifact-producing Agent tasks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from app.studio.capabilities.registry import list_skills
from app.studio.models import SkillDefinition


_MAX_STATUS_BYTES = 4 * 1024 * 1024
_COMPLETION_PATTERNS = (
    re.compile(r"(?:已|已经|现已|全部|均已)(?:经)?[^。；;\n]{0,24}(?:完成|生成|产出|交付|落盘|写入|创建)"),
    re.compile(r"(?:推导|蒸馏|打包|构建|生成|交付)[^。；;\n]{0,12}(?:完成|成功)"),
    re.compile(r"\bstatus\s*[=:]\s*[\"']?(?:complete|completed|success|succeeded)\b", re.I),
    re.compile(r"\b(?:completed|successfully (?:created|generated|written|delivered))\b", re.I),
)
_NEGATED_COMPLETION_PATTERNS = (
    re.compile(r"(?:未|没有|尚未|并未|无法|不能)[^。；;\n]{0,10}(?:完成|生成|产出|交付|落盘|写入|创建)"),
    re.compile(
        r"\b(?:not|isn't|wasn't|hasn't been|could not)\s+"
        r"(?:complete|completed|generated|created|delivered)\b",
        re.I,
    ),
)


@dataclass(frozen=True)
class CompletionValidation:
    """Result of checking declared artifacts and matching Skill contracts."""

    required: bool
    valid: bool
    issues: tuple[str, ...]
    artifacts: tuple[str, ...]
    skills: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "valid": self.valid,
            "issues": list(self.issues),
            "artifacts": list(self.artifacts),
            "skills": list(self.skills),
        }


def has_positive_completion_claim(text: str) -> bool:
    """Return whether text asserts completion rather than describing a blocker."""

    value = str(text or "").strip()
    if not value:
        return False
    stripped = value
    for pattern in _NEGATED_COMPLETION_PATTERNS:
        stripped = pattern.sub("", stripped)
    return any(pattern.search(stripped) for pattern in _COMPLETION_PATTERNS)


def active_skill_names(runs: Iterable[Any], task_id: str) -> tuple[str, ...]:
    """Collect successfully activated Skills across every segment of one task."""

    names: list[str] = []
    for run in runs:
        if str(getattr(run, "task_id", "") or "") != str(task_id or ""):
            continue
        for event in getattr(run, "events", []) or []:
            if event.get("type") != "skill_activation" or event.get("status") != "succeeded":
                continue
            name = str(event.get("skill_name") or event.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def validate_task_completion(
    workspace_root: str | Path,
    *,
    artifacts: Sequence[str] = (),
    prompt: str = "",
    active_skills: Sequence[str] = (),
    owner_id: str | None = None,
    skills: Sequence[SkillDefinition] | None = None,
    require_reported_artifacts: bool = False,
) -> CompletionValidation:
    """Validate real files against reported artifacts and declarative Skill contracts."""

    root = Path(workspace_root).resolve()
    definitions = list(skills) if skills is not None else list_skills(owner_id)
    contracts = _matching_contracts(definitions, prompt, active_skills)
    required = bool(contracts or artifacts or require_reported_artifacts)
    issues: list[str] = []
    reported: list[str] = []
    resolved_reported: dict[str, Path] = {}

    for raw in artifacts:
        relative, path, error = _resolve_workspace_artifact(root, raw)
        if error:
            issues.append(error)
            continue
        if relative not in reported:
            reported.append(relative)
            resolved_reported[relative] = path

    if require_reported_artifacts and not reported:
        issues.append("Completion requires an explicit non-empty artifacts list.")

    for relative, path in resolved_reported.items():
        _check_regular_nonempty_file(relative, path, issues)

    checked_paths: set[str] = set()
    json_cache: dict[str, dict[str, Any] | None] = {}
    for skill, contract in contracts:
        required_artifacts = _string_list(contract.get("required_artifacts"))
        if require_reported_artifacts:
            undeclared = [
                item for item in required_artifacts if _clean_relative(item) not in reported
            ]
            if undeclared:
                issues.append(
                    f"Skill '{skill.name}' required artifacts were not reported: "
                    + ", ".join(undeclared)
                )
        for raw in required_artifacts:
            relative, path, error = _resolve_workspace_artifact(root, raw)
            if error:
                issues.append(f"Skill '{skill.name}': {error}")
                continue
            if relative not in checked_paths:
                _check_regular_nonempty_file(relative, path, issues)
                checked_paths.add(relative)

        for raw in _string_list(contract.get("forbidden_artifacts")):
            relative, path, error = _resolve_workspace_artifact(root, raw)
            if error:
                issues.append(f"Skill '{skill.name}': {error}")
            elif path.exists():
                issues.append(f"Forbidden artifact still exists: {relative}")

        for check in _dict_list(contract.get("status_checks")):
            _check_json_status(root, skill.name, check, issues, json_cache)

        for check in _dict_list(contract.get("fingerprints")):
            _check_fingerprint(root, skill.name, check, issues, json_cache)

    return CompletionValidation(
        required=required,
        valid=not issues,
        issues=tuple(dict.fromkeys(issues)),
        artifacts=tuple(reported),
        skills=tuple(skill.name for skill, _contract in contracts),
    )


def _matching_contracts(
    skills: Sequence[SkillDefinition],
    prompt: str,
    activated: Sequence[str],
) -> list[tuple[SkillDefinition, dict[str, Any]]]:
    activated_names = {str(item).strip().casefold() for item in activated if str(item).strip()}
    lowered_prompt = _routing_text(prompt)
    matches: list[tuple[SkillDefinition, dict[str, Any]]] = []
    for skill in skills:
        contract = skill.completion if isinstance(skill.completion, dict) else {}
        if not contract:
            continue
        triggers = [
            normalized
            for item in _string_list(contract.get("triggers"))
            if (normalized := _routing_text(item))
        ]
        activated_match = skill.name.casefold() in activated_names
        prompt_match = bool(
            lowered_prompt and any(trigger in lowered_prompt for trigger in triggers)
        )
        if activated_match or prompt_match:
            matches.append((skill, contract))
    return matches


def _routing_text(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _resolve_workspace_artifact(root: Path, raw: Any) -> tuple[str, Path, str]:
    value = str(raw or "").strip().replace("\\", "/")
    if value == "/workspace" or not value:
        return "", root, "Artifact must name a file inside /workspace."
    if value.startswith("/workspace/"):
        value = value[len("/workspace/") :]
    elif value.startswith("workspace/"):
        value = value[len("workspace/") :]
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        return "", root, f"Artifact path is outside /workspace: {raw}"
    relative = _clean_relative(value)
    if not relative or relative == ".":
        return "", root, "Artifact must name a file inside /workspace."
    candidate = (root / Path(*PurePosixPath(relative).parts)).resolve()
    if candidate != root and root not in candidate.parents:
        return relative, candidate, f"Artifact path escapes /workspace: {raw}"
    return relative, candidate, ""


def _clean_relative(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if raw.startswith("/workspace/"):
        raw = raw[len("/workspace/") :]
    elif raw.startswith("workspace/"):
        raw = raw[len("workspace/") :]
    while raw.startswith("./"):
        raw = raw[2:]
    return PurePosixPath(raw).as_posix()


def _check_regular_nonempty_file(relative: str, path: Path, issues: list[str]) -> None:
    if not path.exists():
        issues.append(f"Artifact does not exist: {relative}")
        return
    if not path.is_file():
        issues.append(f"Artifact is not a regular file: {relative}")
        return
    try:
        if path.stat().st_size <= 0:
            issues.append(f"Artifact is empty: {relative}")
    except OSError as exc:
        issues.append(f"Artifact cannot be inspected: {relative} ({exc})")


def _check_json_status(
    root: Path,
    skill_name: str,
    check: dict[str, Any],
    issues: list[str],
    cache: dict[str, dict[str, Any] | None],
) -> None:
    raw_artifact = check.get("artifact")
    relative, path, error = _resolve_workspace_artifact(root, raw_artifact)
    if error:
        issues.append(f"Skill '{skill_name}': {error}")
        return
    payload = _load_json_object(relative, path, issues, cache)
    if payload is None:
        return
    field = str(check.get("field") or "status").strip()
    allowed = _string_list(check.get("allowed")) or ["complete"]
    actual = _nested_value(payload, field)
    if str(actual or "").casefold() not in {item.casefold() for item in allowed}:
        issues.append(
            f"Skill '{skill_name}' status check failed for {relative}: "
            f"{field}={actual!r}, expected one of {allowed!r}"
        )


def _check_fingerprint(
    root: Path,
    skill_name: str,
    check: dict[str, Any],
    issues: list[str],
    cache: dict[str, dict[str, Any] | None],
) -> None:
    artifact_relative, artifact_path, artifact_error = _resolve_workspace_artifact(
        root, check.get("artifact")
    )
    source_relative, source_path, source_error = _resolve_workspace_artifact(
        root, check.get("source")
    )
    if artifact_error or source_error:
        issues.append(f"Skill '{skill_name}': {artifact_error or source_error}")
        return
    payload = _load_json_object(artifact_relative, artifact_path, issues, cache)
    if payload is None:
        return
    if not source_path.is_file():
        issues.append(f"Fingerprint source does not exist: {source_relative}")
        return
    field = str(check.get("field") or "source.fingerprint").strip()
    expected = _file_sha256(source_path)
    actual = str(_nested_value(payload, field) or "")
    if actual != expected:
        issues.append(
            f"Skill '{skill_name}' fingerprint check failed for {artifact_relative}: "
            f"{field} does not match {source_relative}"
        )


def _load_json_object(
    relative: str,
    path: Path,
    issues: list[str],
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    if relative in cache:
        return cache[relative]
    if not path.is_file():
        issues.append(f"JSON artifact does not exist: {relative}")
        cache[relative] = None
        return None
    try:
        if path.stat().st_size > _MAX_STATUS_BYTES:
            raise ValueError(f"status artifact exceeds {_MAX_STATUS_BYTES} bytes")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("top level must be an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        issues.append(f"Invalid JSON artifact {relative}: {exc}")
        cache[relative] = None
        return None
    cache[relative] = value
    return value


def _nested_value(payload: dict[str, Any], dotted: str) -> Any:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


__all__ = [
    "CompletionValidation",
    "active_skill_names",
    "has_positive_completion_claim",
    "validate_task_completion",
]
