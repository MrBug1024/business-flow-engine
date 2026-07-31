"""Semantic task progress Tool for long-running Agent work."""

from __future__ import annotations

import re
from copy import deepcopy
from time import time
from typing import Any, Literal
from uuid import uuid4

from langchain_core.tools import tool

from app.studio.completion import active_skill_names, validate_task_completion
from app.studio.runtime.tool_context import get_tool_context


ProgressAction = Literal["plan", "start", "update", "complete", "block", "compact"]


@tool(
    description=(
        "Maintain the user-facing state of one substantial task. A plan must describe "
        "meaningful business work items with why/expected outcomes. Reuse each stable "
        "work_item_id when starting or updating it, then record actual results and "
        "verification. For artifact-producing work, completion is accepted only when "
        "every reported workspace file exists and any activated or request-matched Skill "
        "completion contract passes. Mark the task complete only after acceptance criteria pass, "
        "block only for a real decision/dependency, and compact only after saving a "
        "bounded checkpoint. For plan/start/update/block/compact, include `message`: "
        "a concise user-facing update that says what was learned or will happen next, "
        "without narrating low-level calls. Each message becomes a durable AI chat "
        "reply. Never call this for each low-level Tool, Skill, MCP, model turn, file "
        "read, or sandbox command."
    ),
)
def report_task_progress(
    action: ProgressAction,
    objective: str = "",
    work_items: list[dict[str, Any]] | None = None,
    work_item_id: str = "",
    title: str = "",
    summary: str = "",
    why: str = "",
    result: str = "",
    verification: str = "",
    artifacts: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    next_step: str = "",
    message: str = "",
) -> dict[str, Any]:
    """Merge a semantic task checkpoint without encoding any business workflow."""

    context = get_tool_context()
    run = next((item for item in context.record.runs if item.id == context.run_id), None)
    task_id = str(getattr(run, "task_id", "") or "")
    normalized_items = [_normalize_work_item(item) for item in (work_items or [])]
    normalized_items = [item for item in normalized_items if item["title"]]
    previous = _previous_task_progress(context.record.runs, task_id)
    completion_validation: dict[str, Any] | None = None
    effective_action: ProgressAction = action
    effective_message = str(message or "").strip()[:4000]
    effective_summary = summary
    effective_result = result
    effective_verification = verification
    effective_next_step = next_step
    if action == "complete":
        validation = validate_task_completion(
            context.workspace_path,
            artifacts=artifacts or [],
            prompt=_latest_user_prompt(context.record, run),
            active_skills=active_skill_names(context.record.runs, task_id),
            owner_id=str(getattr(context.record, "owner_id", "") or "") or None,
        )
        if validation.skills:
            validation = validate_task_completion(
                context.workspace_path,
                artifacts=artifacts or [],
                prompt=_latest_user_prompt(context.record, run),
                active_skills=active_skill_names(context.record.runs, task_id),
                owner_id=str(getattr(context.record, "owner_id", "") or "") or None,
                require_reported_artifacts=True,
            )
        completion_validation = validation.as_dict()
        if not validation.valid:
            effective_action = "update"
            effective_message = "交付验收未通过，正在补齐缺失、无效或已过期的产物。"
            effective_summary = "交付完成声明已被磁盘验收拒绝。"
            effective_result = ""
            effective_verification = ""
            effective_next_step = "修复完成验收问题后重新报告 complete：" + "; ".join(
                validation.issues[:6]
            )
    state = _merge_progress_state(
        previous,
        action=effective_action,
        task_id=task_id,
        objective=objective,
        work_items=normalized_items,
        work_item_id=work_item_id,
        title=title,
        summary=effective_summary,
        why=why,
        result=effective_result,
        verification=effective_verification,
        artifacts=artifacts or [],
        acceptance_criteria=acceptance_criteria or [],
        next_step=effective_next_step,
    )
    if completion_validation is not None:
        state["completion_validation"] = completion_validation
    if run is not None:
        run.task_progress = deepcopy(state)
        run.plan = [item["title"] for item in state["work_items"]][:12]

    event = deepcopy(state) | {
        "type": "agent_progress",
        "action": effective_action,
        "requested_action": action,
        "work_item_id": _clean_optional_id(work_item_id or title),
        "message": effective_message,
    }
    context.emit(event)
    if action == "plan" and state["work_items"]:
        context.emit({"type": "plan", "items": [item["title"] for item in state["work_items"]]})
    context.save()
    return {
        "status": (
            "rejected"
            if action == "complete" and completion_validation and not completion_validation["valid"]
            else "recorded"
        ),
        "action": action,
        "recorded_action": effective_action,
        "task_id": task_id,
        "task_status": state["status"],
        "summary": state["summary"] or state["title"] or action,
        "work_item_count": len(state["work_items"]),
        "revision": state["revision"],
        **({"completion_validation": completion_validation} if completion_validation is not None else {}),
    }


def _latest_user_prompt(record: Any, run: Any) -> str:
    session_id = str(getattr(run, "session_id", "") or "")
    started_at = float(getattr(run, "started_at", 0) or 0)
    for item in reversed(getattr(record, "messages", []) or []):
        if str(getattr(item, "role", "") or "") != "user":
            continue
        if session_id and str(getattr(item, "session_id", "") or "") != session_id:
            continue
        created_at = float(getattr(item, "created_at", 0) or 0)
        if started_at and created_at > started_at:
            continue
        return str(getattr(item, "content", "") or "")
    return ""


def _previous_task_progress(runs: list[Any], task_id: str) -> dict[str, Any]:
    if not task_id:
        return {}
    for candidate in reversed(runs):
        if str(getattr(candidate, "task_id", "") or "") != task_id:
            continue
        state = getattr(candidate, "task_progress", None)
        if isinstance(state, dict) and state:
            return deepcopy(state)
    return {}


def _merge_progress_state(
    previous: dict[str, Any],
    *,
    action: ProgressAction,
    task_id: str,
    objective: str,
    work_items: list[dict[str, str]],
    work_item_id: str,
    title: str,
    summary: str,
    why: str,
    result: str,
    verification: str,
    artifacts: list[str],
    acceptance_criteria: list[str],
    next_step: str,
) -> dict[str, Any]:
    now = time()
    state: dict[str, Any] = {
        "task_id": task_id,
        "status": "planned",
        "objective": "",
        "title": "",
        "summary": "",
        "why": "",
        "result": "",
        "verification": "",
        "next_step": "",
        "acceptance_criteria": [],
        "artifacts": [],
        "work_items": [],
        "current_work_item_id": "",
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        **deepcopy(previous),
    }
    state["task_id"] = task_id
    state["revision"] = int(state.get("revision") or 0) + 1
    state["updated_at"] = now
    state["status"] = _status_for_action(action)

    _replace_if_present(state, "objective", objective, 1000)
    _replace_if_present(state, "title", title, 240)
    _replace_if_present(state, "summary", summary, 2000)
    _replace_if_present(state, "why", why, 1200)
    _replace_if_present(state, "result", result, 2000)
    _replace_if_present(state, "verification", verification, 1200)
    _replace_if_present(state, "next_step", next_step, 1200)

    if acceptance_criteria:
        state["acceptance_criteria"] = _unique_strings(acceptance_criteria, 12, 500)
    state["artifacts"] = _unique_strings(
        [*(state.get("artifacts") or []), *artifacts],
        20,
        500,
    )
    state["work_items"] = _merge_work_items(state.get("work_items"), work_items)

    target_id = _clean_optional_id(work_item_id or title)
    if target_id:
        state["current_work_item_id"] = target_id
        target = next(
            (item for item in state["work_items"] if item.get("id") == target_id),
            None,
        )
        if target is None and title.strip():
            target = _normalize_work_item({"id": target_id, "title": title})
            state["work_items"].append(target)
        if target is not None:
            _replace_if_present(target, "title", title, 240)
            _replace_if_present(target, "why", why, 1000)
            _replace_if_present(target, "result", result, 1200)
            _replace_if_present(target, "verification", verification, 1000)
            if action == "start":
                target["status"] = "running"
            elif action == "update":
                target["status"] = (
                    "completed"
                    if str(result or "").strip() and str(verification or "").strip()
                    else "running"
                )
            elif action == "complete":
                target["status"] = "completed"
            elif action == "block":
                target["status"] = "blocked"

    if action == "complete":
        state["current_work_item_id"] = ""
    return state


def _merge_work_items(existing: Any, incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    merged = [
        _normalize_work_item(item)
        for item in (existing if isinstance(existing, list) else [])
        if isinstance(item, dict)
    ]
    positions = {item["id"]: index for index, item in enumerate(merged)}
    for item in incoming:
        index = positions.get(item["id"])
        if index is None:
            item["status"] = item.get("status") or "pending"
            positions[item["id"]] = len(merged)
            merged.append(item)
            continue
        merged[index] = {
            key: value or merged[index].get(key, "")
            for key, value in item.items()
        }
    return [item for item in merged if item["title"]][:12]


def _replace_if_present(target: dict[str, Any], key: str, value: Any, limit: int) -> None:
    cleaned = str(value or "").strip()
    if cleaned:
        target[key] = cleaned[:limit]


def _unique_strings(values: list[Any], maximum: int, limit: int) -> list[str]:
    result: list[str] = []
    for item in values:
        value = str(item or "").strip()[:limit]
        if value and value not in result:
            result.append(value)
    return result[-maximum:]


def _normalize_work_item(raw: dict[str, Any]) -> dict[str, str]:
    title = str(raw.get("title") or raw.get("name") or raw.get("summary") or "").strip()
    item_id = str(raw.get("id") or raw.get("key") or title).strip()
    return {
        "id": _clean_id(item_id),
        "title": title[:240],
        "status": _clean_status(raw.get("status")) if raw.get("status") else "",
        "why": str(raw.get("why") or raw.get("purpose") or "").strip()[:1000],
        "expected": str(raw.get("expected") or raw.get("acceptance") or "").strip()[:1000],
        "result": str(raw.get("result") or raw.get("outcome") or "").strip()[:1200],
        "verification": str(raw.get("verification") or "").strip()[:1000],
    }


def _status_for_action(action: str) -> str:
    if action == "complete":
        return "completed"
    if action == "block":
        return "blocked"
    if action == "plan":
        return "planned"
    if action == "compact":
        return "continuing"
    return "running"


def _clean_status(value: Any) -> str:
    status = str(value or "").strip().casefold()
    return status if status in {"pending", "running", "completed", "blocked"} else "pending"


def _clean_id(value: str) -> str:
    cleaned = re.sub(r"[^\w-]+", "_", value.strip(), flags=re.UNICODE).strip("_").casefold()
    return cleaned[:80] or f"work_{uuid4().hex[:12]}"


def _clean_optional_id(value: str) -> str:
    return _clean_id(value) if value.strip() else ""


report_task_progress.metadata = {
    "studio": {
        "protocol": "task_progress",
        "retry_safe": True,
        "capability": {
            "id": "report-task-progress",
            "responsibility": "Persist semantic task plans, verified progress, blockers, and completion state.",
            "excludes": [
                "Performing business analysis",
                "Replacing Tool, Skill, or MCP execution",
                "Emitting one event per low-level call",
            ],
        },
    }
}
