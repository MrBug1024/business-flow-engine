#!/usr/bin/env python3
"""Maintain a small, deterministic state file for a distilled business flow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


MAX_JSON_BYTES = 4 * 1024 * 1024
ARTIFACT_ROOT_ENV = "BUSINESS_ARTIFACT_ROOT"


class OrchestratorError(ValueError):
    pass


class ArtifactPathError(OrchestratorError):
    pass


def artifact_root() -> Path:
    configured = str(os.environ.get(ARTIFACT_ROOT_ENV, "")).strip()
    if not configured:
        raise ArtifactPathError(
            "Persistent artifact output is blocked: the host must set BUSINESS_ARTIFACT_ROOT."
        )
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise ArtifactPathError("BUSINESS_ARTIFACT_ROOT must be an absolute host-managed directory.")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ArtifactPathError("BUSINESS_ARTIFACT_ROOT is not a writable artifact directory.")
    return root


def normalize_artifact_name(value: str | Path, label: str = "artifact name") -> str:
    raw = str(value or "").strip()
    normalized = raw.replace("\\", "/")
    if not normalized:
        raise ArtifactPathError(f"{label} must be a non-empty relative artifact name.")
    if (
        "\x00" in normalized
        or normalized.startswith("/")
        or raw.startswith("\\")
        or re.match(r"^[A-Za-z]:", raw)
    ):
        raise ArtifactPathError(f"{label} must be a safe relative artifact name, not an absolute path.")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ArtifactPathError(f"{label} must not contain traversal or drive-qualified segments.")
    return PurePosixPath(*parts).as_posix()


def _absolute_artifact_input(raw: str) -> bool:
    return (
        Path(raw).expanduser().is_absolute()
        or raw.replace("\\", "/").startswith("/")
        or raw.startswith("\\")
        or bool(re.match(r"^[A-Za-z]:", raw))
    )


def resolve_artifact_name(value: str | Path, label: str = "artifact name") -> tuple[Path, str]:
    root = artifact_root()
    raw = str(value or "").strip()
    if _absolute_artifact_input(raw):
        target = Path(raw).expanduser().resolve()
        try:
            relative_path = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ArtifactPathError(
                f"{label} must be a safe relative artifact name or an already-translated path inside BUSINESS_ARTIFACT_ROOT."
            ) from exc
    else:
        relative_path = normalize_artifact_name(raw, label)
        target = (root.joinpath(*PurePosixPath(relative_path).parts)).resolve()
    if target == root or root not in target.parents:
        raise ArtifactPathError(f"{label} escapes BUSINESS_ARTIFACT_ROOT.")
    return target, relative_path


def artifact_relative_path(path: Path) -> tuple[Path, str]:
    root = artifact_root()
    resolved = path.expanduser().resolve()
    try:
        relative_path = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ArtifactPathError("Artifact output must stay inside BUSINESS_ARTIFACT_ROOT.") from exc
    if not relative_path or relative_path == ".":
        raise ArtifactPathError("Artifact output must name a file below BUSINESS_ARTIFACT_ROOT.")
    return resolved, relative_path


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_reference(path: Path, kind: str = "orchestration_state") -> dict[str, Any]:
    resolved, relative_path = artifact_relative_path(path)
    if not resolved.is_file():
        raise ArtifactPathError("Artifact file does not exist inside BUSINESS_ARTIFACT_ROOT.")
    sha256 = file_digest(resolved)
    return {
        "schema_version": 1,
        "kind": kind,
        "artifact_id": "artifact-" + hashlib.sha256(
            f"{kind}\0{relative_path}\0{sha256}".encode("utf-8")
        ).hexdigest()[:24],
        "relative_path": relative_path,
        "format": resolved.suffix.casefold().lstrip("."),
        "sha256": sha256,
        "size_bytes": resolved.stat().st_size,
    }


def blocked_artifact_output(exc: ArtifactPathError) -> dict[str, Any]:
    return {
        "status": "blocked_host_artifact_sink",
        "message": str(exc),
        "artifact_root_environment": ARTIFACT_ROOT_ENV,
    }


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise OrchestratorError(f"Missing or oversized JSON: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestratorError(f"Invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OrchestratorError("Top-level JSON must be an object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path, _relative_path = artifact_relative_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("state_digest", None)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_handoff(handoff: dict[str, Any]) -> None:
    expected = str(handoff.get("handoff_digest", ""))
    unsigned = dict(handoff)
    unsigned.pop("handoff_digest", None)
    if not expected or digest(unsigned) != expected:
        raise OrchestratorError("Handoff digest mismatch")


def routing_path() -> Path:
    return Path(__file__).resolve().parents[1] / "references" / "capability-routing.json"


def verify_state(state: dict[str, Any]) -> None:
    if state.get("state_digest") != digest(state):
        raise OrchestratorError("State digest mismatch")


def next_route(routing: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    completed = set(str(item) for item in state.get("completed_stage_ids", []))
    route_by_stage = {
        str(item.get("stage_id", "")): item for item in routing.get("routing", []) if isinstance(item, dict)
    }
    for stage_id in routing.get("main_flow", []):
        if str(stage_id) not in completed:
            route = route_by_stage.get(str(stage_id))
            if route is None:
                raise OrchestratorError(f"Missing route for main-flow stage: {stage_id}")
            return route
    return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--request", required=True)
    start.add_argument("--output", required=True, help="Safe relative artifact name under BUSINESS_ARTIFACT_ROOT.")
    status = commands.add_parser("status")
    status.add_argument("--state", required=True, help="Safe relative state artifact name under BUSINESS_ARTIFACT_ROOT.")
    record = commands.add_parser("record")
    record.add_argument("--state", required=True, help="Safe relative state artifact name under BUSINESS_ARTIFACT_ROOT.")
    record.add_argument("--handoff", required=True, help="Safe relative handoff artifact name under BUSINESS_ARTIFACT_ROOT.")
    route = commands.add_parser("route")
    route.add_argument("--stage-id", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    routing = read_json(routing_path())
    route_by_stage = {
        str(item.get("stage_id", "")): item for item in routing.get("routing", []) if isinstance(item, dict)
    }
    if args.command == "start":
        if not args.request.strip():
            raise OrchestratorError("Scenario request cannot be empty")
        state = {
            "schema_version": 1,
            "status": "in_progress",
            "scenario": routing.get("scenario"),
            "request": args.request.strip(),
            "completed_stage_ids": [],
            "handoffs": [],
        }
        state["state_digest"] = digest(state)
        try:
            output_path, _output_relative_path = resolve_artifact_name(args.output, "--output")
        except ArtifactPathError as exc:
            return 2, blocked_artifact_output(exc)
        write_json(output_path, state)
        artifact = artifact_reference(output_path)
        return 0, {
            "status": "in_progress", "state_artifact": artifact,
            "state_artifact_id": artifact["artifact_id"],
            "state_relative_path": artifact["relative_path"], "next": next_route(routing, state),
        }
    if args.command == "route":
        route = route_by_stage.get(args.stage_id)
        if route is None:
            raise OrchestratorError(f"Unknown stage: {args.stage_id}")
        return 0, {"status": "ready", "route": route}
    try:
        state_path, _state_relative_path = resolve_artifact_name(args.state, "--state")
    except ArtifactPathError as exc:
        return 2, blocked_artifact_output(exc)
    state = read_json(state_path)
    verify_state(state)
    if args.command == "status":
        route = next_route(routing, state)
        artifact = artifact_reference(state_path)
        return 0, {
            "status": "complete" if not route else "in_progress", "next": route,
            "completed_stage_ids": state.get("completed_stage_ids", []),
            "state_artifact": artifact, "state_relative_path": artifact["relative_path"],
        }
    try:
        handoff_path, _handoff_relative_path = resolve_artifact_name(args.handoff, "--handoff")
    except ArtifactPathError as exc:
        return 2, blocked_artifact_output(exc)
    handoff = read_json(handoff_path)
    if handoff.get("status") != "complete":
        raise OrchestratorError("Handoff status must be complete")
    verify_handoff(handoff)
    expected = next_route(routing, state)
    stage_id = str(handoff.get("stage_id", ""))
    if stage_id not in route_by_stage:
        raise OrchestratorError(f"Unknown handoff stage: {stage_id}")
    main_flow = {str(item) for item in routing.get("main_flow", [])}
    if stage_id not in main_flow:
        state.setdefault("optional_handoffs", []).append({
            "stage_id": stage_id, "handoff_digest": handoff.get("handoff_digest", "")
        })
        state["state_digest"] = digest(state)
        write_json(state_path, state)
        artifact = artifact_reference(state_path)
        return 0, {
            "status": state["status"], "next": expected,
            "state_artifact": artifact, "state_relative_path": artifact["relative_path"],
        }
    if not expected or stage_id != str(expected.get("stage_id", "")):
        raise OrchestratorError(f"Out-of-order handoff: {stage_id}")
    state["completed_stage_ids"].append(stage_id)
    state["handoffs"].append({"stage_id": stage_id, "handoff_digest": handoff.get("handoff_digest", "")})
    following = next_route(routing, state)
    state["status"] = "in_progress" if following else "complete"
    state["state_digest"] = digest(state)
    write_json(state_path, state)
    artifact = artifact_reference(state_path)
    return 0, {
        "status": state["status"], "next": following,
        "state_artifact": artifact, "state_relative_path": artifact["relative_path"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        code, payload = run(argv)
    except (OrchestratorError, OSError, ValueError) as exc:
        code, payload = 2, {"status": "blocked", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
