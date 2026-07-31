#!/usr/bin/env python3
"""Maintain a small, deterministic state file for a distilled business flow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence


MAX_JSON_BYTES = 4 * 1024 * 1024


class OrchestratorError(ValueError):
    pass


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
    start.add_argument("--output", required=True)
    status = commands.add_parser("status")
    status.add_argument("--state", required=True)
    record = commands.add_parser("record")
    record.add_argument("--state", required=True)
    record.add_argument("--handoff", required=True)
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
        state = {
            "schema_version": 1,
            "status": "in_progress",
            "scenario": routing.get("scenario"),
            "request": args.request.strip(),
            "completed_stage_ids": [],
            "handoffs": [],
        }
        state["state_digest"] = digest(state)
        write_json(Path(args.output).resolve(), state)
        return 0, {"status": "in_progress", "state": str(Path(args.output).resolve()), "next": next_route(routing, state)}
    if args.command == "route":
        route = route_by_stage.get(args.stage_id)
        if route is None:
            raise OrchestratorError(f"Unknown stage: {args.stage_id}")
        return 0, {"status": "ready", "route": route}
    state_path = Path(args.state).resolve()
    state = read_json(state_path)
    verify_state(state)
    if args.command == "status":
        route = next_route(routing, state)
        return 0, {"status": "complete" if not route else "in_progress", "next": route, "completed_stage_ids": state.get("completed_stage_ids", [])}
    handoff = read_json(Path(args.handoff).resolve())
    if handoff.get("status") != "complete":
        raise OrchestratorError("Handoff status must be complete")
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
        return 0, {"status": state["status"], "next": expected, "state": str(state_path)}
    if not expected or stage_id != str(expected.get("stage_id", "")):
        raise OrchestratorError(f"Out-of-order handoff: {stage_id}")
    state["completed_stage_ids"].append(stage_id)
    state["handoffs"].append({"stage_id": stage_id, "handoff_digest": handoff.get("handoff_digest", "")})
    following = next_route(routing, state)
    state["status"] = "in_progress" if following else "complete"
    state["state_digest"] = digest(state)
    write_json(state_path, state)
    return 0, {"status": state["status"], "next": following, "state": str(state_path)}


def main(argv: Sequence[str] | None = None) -> int:
    try:
        code, payload = run(argv)
    except (OrchestratorError, OSError, ValueError) as exc:
        code, payload = 2, {"status": "blocked", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
