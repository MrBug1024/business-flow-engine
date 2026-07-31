"""AI-centered orchestration for Business Studio."""

from __future__ import annotations

import re
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from time import time
from typing import Any

from app.core.config import settings as env_settings
from app.studio.completion import (
    active_skill_names,
    has_positive_completion_claim,
    validate_task_completion,
)
from app.studio.models import AIRun, BusinessRecord
from app.studio.prompt_loader import render_prompt
from app.studio.runtime import run_agent
from app.studio.settings import studio_settings
from app.studio.storage import new_id, store


class ResumeBlockedError(ValueError):
    pass


@dataclass(frozen=True)
class ResumePreparation:
    session_id: str
    source_run_id: str | None
    selected_model: str
    question_ids: tuple[str, ...]
    answers: tuple[dict[str, Any], ...]
    prompt: str


class BusinessOrchestrator:
    def chat(
        self,
        record: BusinessRecord,
        message: str,
        model: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        done: dict[str, Any] | None = None
        for event in self.stream_chat(record, message, model, session_id):
            if event.get("type") == "done":
                done = event
        if done is None:
            raise RuntimeError("Agent 对话未正常结束。")
        resolved_session_id = str(done["assistant_message"].get("session_id") or "")
        user_message = next(
            (
                item
                for item in reversed(record.messages)
                if item.role == "user" and item.session_id == resolved_session_id
            ),
            None,
        )
        return {
            "user_message": user_message,
            "assistant_message": done["assistant_message"],
            "run": done["run"],
            "context": record.context,
        }

    def stream_chat(
        self,
        record: BusinessRecord,
        message: str,
        model: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        _sync_workspace_metadata(record)
        session = store.require_chat_session(record, session_id)
        selected_model = studio_settings.active_model_name(
            model,
            owner_id=record.owner_id,
        )
        continuation_source = _manual_continuation_source(record, session.id, message)
        original_goal = (
            _task_original_prompt(record, continuation_source, message)
            if continuation_source is not None
            else message
        )
        user_message = store.append_message(record, "user", message, session_id=session.id)
        task_id = continuation_source.task_id if continuation_source is not None else new_id("task")
        segment_index = (
            _next_task_segment_index(record, task_id) if continuation_source is not None else 1
        )
        run = self._new_run(
            record,
            selected_model,
            session.id,
            task_id=task_id,
            segment_index=segment_index,
            continued_from_run_id=(
                continuation_source.id if continuation_source is not None else None
            ),
        )
        if continuation_source is not None:
            run.plan = list(continuation_source.plan)
            run.task_progress = deepcopy(continuation_source.task_progress)
            _ensure_platform_task_checkpoint(record, continuation_source, original_goal)
            run.task_progress = deepcopy(continuation_source.task_progress)
            if run.task_progress:
                run.task_progress["status"] = "running"
        store.save(record)

        yield _event(run, "message", {"message": user_message.model_dump(mode="json")})
        segment_prompt = (
            _auto_continuation_prompt(
                record,
                original_goal,
                continuation_source,
                "The user explicitly requested that the interrupted task continue.",
            )
            if continuation_source is not None
            else message
        )
        include_history = continuation_source is None
        segment_limit = max(1, env_settings.agent_auto_continuation_limit)
        segments_used = 1
        while True:
            yield _event(run, "run_start", {"run": run.model_dump(mode="json")})
            continuation_error = ""
            for event in self._stream_run(
                record,
                run,
                selected_model,
                user_prompt=segment_prompt,
                include_history=include_history,
            ):
                if (
                    event.get("type") == "error"
                    and segments_used < segment_limit
                    and _is_recoverable_segment_error(str(event.get("message") or ""), run)
                ):
                    continuation_error = str(event.get("message") or "")
                    _ensure_platform_task_checkpoint(record, run, original_goal)
                    continue
                if event.get("type") == "error" and not event.get("assistant_message"):
                    failure_message = _append_failure_message(
                        record,
                        run,
                        str(event.get("message") or "Agent execution failed."),
                    )
                    event["assistant_message"] = failure_message.model_dump(mode="json")
                yield event
            if not continuation_error and _progress_requests_continuation(run):
                if segments_used >= segment_limit:
                    error = (
                        "交付完成验收连续未通过，已停止本次任务，避免把未完成结果误报为完成。"
                    )
                    run.status = "failed"
                    run.finished_at = time()
                    run.error = error
                    run.summary = "Artifact completion validation did not pass."
                    failure_message = _append_failure_message(record, run, error)
                    yield _event(
                        run,
                        "error",
                        {
                            "message": error,
                            "assistant_message": failure_message.model_dump(mode="json"),
                            "run": run.model_dump(mode="json"),
                        },
                    )
                    return
                continuation_error = (
                    "Agent completion was not accepted. Continue from the saved checkpoint, "
                    "activate the matching Skill, create the required artifacts, and pass its "
                    "filesystem completion contract before replying."
                )
            if not continuation_error:
                return

            run.status = "succeeded"
            run.finished_at = time()
            run.summary = (
                f"Task segment {run.segment_index} reached a context or execution boundary; "
                "continued from a compact checkpoint in a fresh Agent run."
            )
            run.error = ""
            next_run = self._new_run(
                record,
                selected_model,
                session.id,
                task_id=task_id,
                segment_index=run.segment_index + 1,
                continued_from_run_id=run.id,
            )
            next_run.plan = list(run.plan)
            next_run.task_progress = deepcopy(run.task_progress)
            if next_run.task_progress:
                next_run.task_progress["status"] = "running"
            handoff = _event(
                run,
                "task_handoff",
                {
                    "call_id": f"handoff_{run.id}_{next_run.id}",
                    "name": f"阶段 {run.segment_index + 1}",
                    "status": "succeeded",
                    "summary": "阶段状态已保存，正在用新的模型上下文从检查点继续同一任务。",
                    "reason": continuation_error[:1000],
                    "task_id": task_id,
                    "from_run_id": run.id,
                    "to_run_id": next_run.id,
                    "segment_index": next_run.segment_index,
                },
            )
            store.save(record)
            yield handoff
            segment_prompt = _auto_continuation_prompt(
                record,
                original_goal,
                run,
                continuation_error,
            )
            include_history = False
            run = next_run
            segments_used += 1

    def prepare_resume(
        self,
        record: BusinessRecord,
        session_id: str,
        model: str | None = None,
        run_id: str | None = None,
    ) -> ResumePreparation:
        session = store.require_chat_session(record, session_id)
        source_run = _resume_source_run(record, session.id, run_id)
        linked_questions = [
            item
            for item in record.context.questions
            if source_run is not None and item.get("run_id") == source_run.id
        ]
        pending = [item for item in linked_questions if item.get("status", "open") != "answered"]
        if pending:
            raise ResumeBlockedError("Please answer all questions from the waiting run before resuming.")

        answers = _resume_answers(
            record,
            session.id,
            linked_questions if linked_questions else None,
        )
        if not answers:
            raise ResumeBlockedError("No answered questions are available for this session.")

        source_run_id = source_run.id if source_run is not None else next(
            (str(item.get("run_id")) for item in reversed(answers) if item.get("run_id")),
            None,
        )
        return ResumePreparation(
            session_id=session.id,
            source_run_id=source_run_id,
            selected_model=studio_settings.active_model_name(
                model,
                owner_id=record.owner_id,
            ),
            question_ids=tuple(str(item["question_id"]) for item in answers if item.get("question_id")),
            answers=tuple(answers),
            prompt=_resume_prompt(answers),
        )

    def stream_resume(
        self,
        record: BusinessRecord,
        preparation: ResumePreparation,
    ) -> Iterator[dict[str, Any]]:
        session = store.require_chat_session(record, preparation.session_id)
        source_run = next(
            (item for item in record.runs if item.id == preparation.source_run_id),
            None,
        )
        run = self._new_run(
            record,
            preparation.selected_model,
            session.id,
            task_id=source_run.task_id if source_run is not None else "",
            segment_index=source_run.segment_index if source_run is not None else 1,
            resumed_from_run_id=preparation.source_run_id,
        )
        if source_run is not None:
            run.plan = list(source_run.plan)
            run.task_progress = deepcopy(source_run.task_progress)
        store.save(record)
        try:
            yield _event(
                run,
                "run_start",
                {
                    "run": run.model_dump(mode="json"),
                    "resume": {"from_run_id": preparation.source_run_id},
                },
            )
            for event in self._stream_run(
                record,
                run,
                preparation.selected_model,
                user_prompt=preparation.prompt,
                resume_payload={
                    "source_run_id": preparation.source_run_id,
                    "answers": list(preparation.answers),
                },
            ):
                if event.get("type") == "error" and not event.get("assistant_message"):
                    failure_message = _append_failure_message(
                        record,
                        run,
                        str(event.get("message") or "Agent execution failed."),
                    )
                    event["assistant_message"] = failure_message.model_dump(mode="json")
                if event.get("type") == "done" and run.status in {"succeeded", "waiting_for_user"}:
                    if source_run is not None and source_run.status == "waiting_for_user":
                        source_run.status = "succeeded"
                        source_run.finished_at = time()
                        source_run.summary = "User confirmation received; continuation run created."
                    consumed_at = time()
                    consumed_ids = set(preparation.question_ids)
                    for question in record.context.questions:
                        if str(question.get("id") or "") not in consumed_ids:
                            continue
                        question["continued_at"] = consumed_at
                        question["continuation_run_id"] = run.id
                    store.save(record)
                    event["context"] = record.context.model_dump(mode="json")
                yield event
            if _progress_requests_continuation(run):
                error = (
                    "恢复任务的交付完成验收未通过；本次恢复已停止，未保存模型的完成声明。"
                )
                run.status = "failed"
                run.finished_at = time()
                run.error = error
                run.summary = "Artifact completion validation did not pass after resume."
                failure_message = _append_failure_message(record, run, error)
                yield _event(
                    run,
                    "error",
                    {
                        "message": error,
                        "assistant_message": failure_message.model_dump(mode="json"),
                        "run": run.model_dump(mode="json"),
                    },
                )
        except GeneratorExit:
            _fail_cancelled_stream(record, run)
            raise

    def _stream_run(
        self,
        record: BusinessRecord,
        run: AIRun,
        selected_model: str,
        *,
        user_prompt: str | None = None,
        include_history: bool = True,
        resume_payload: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        response_parts: list[str] = []
        activity_cursor = len(run.events)
        completion_message = ""
        last_progress_key: tuple[str, str, str] | None = None
        try:
            for payload in run_agent(
                record,
                run,
                requested_model=selected_model,
                user_prompt=user_prompt,
                include_history=include_history,
                resume_payload=resume_payload,
            ):
                event_type = payload.pop("type")
                if event_type == "token":
                    response_parts.append(str(payload.get("content") or ""))
                event = _event(run, event_type, payload)
                yield event
                if event_type != "agent_progress":
                    continue

                action = str(event.get("action") or "update").casefold()
                update_text = _progress_message_text(event)
                if action == "complete":
                    completion_message = update_text
                    continue
                if action not in {"plan", "start", "update", "block", "compact"}:
                    continue
                progress_key = (
                    action,
                    str(event.get("work_item_id") or ""),
                    update_text,
                )
                if not update_text or progress_key == last_progress_key:
                    continue
                _reconcile_activity_events(run)
                activity_events = deepcopy(run.events[activity_cursor:])
                progress_message = store.append_message(
                    record,
                    "assistant",
                    update_text,
                    run.id,
                    session_id=run.session_id,
                    task_id=run.task_id,
                    kind="progress",
                    progress_action=action,
                    work_item_id=str(event.get("work_item_id") or ""),
                    progress=deepcopy(run.task_progress or event),
                    activity_events=activity_events,
                )
                activity_cursor = len(run.events)
                response_parts.clear()
                last_progress_key = progress_key
                store.save(record)
                yield _event(
                    run,
                    "progress_message",
                    {"message": progress_message.model_dump(mode="json")},
                )

            waiting_for_user = run.status == "waiting_for_user"
            response = "".join(response_parts).strip()
            if _progress_requests_continuation(run) and not waiting_for_user:
                run.status = "succeeded"
                run.finished_at = time()
                run.summary = _run_summary(run)
                store.save(record)
                return
            if not response and not waiting_for_user:
                response = completion_message
            if not response and not waiting_for_user:
                raise RuntimeError("Model returned no final content after completing a task segment.")
            if not response:
                response = (
                    "请先完成下方待确认问题，确认后我会继续处理。"
                    if waiting_for_user
                    else "模型没有返回内容。"
                )
            completion_issues = _completion_claim_issues(record, run, user_prompt or "", response)
            if completion_issues and not waiting_for_user:
                progress = deepcopy(run.task_progress or {})
                progress.update(
                    {
                        "task_id": run.task_id,
                        "status": "continuing",
                        "objective": str(
                            progress.get("objective")
                            or _task_user_prompt(record, run, user_prompt or "")
                        )[:1000],
                        "summary": "模型声明完成，但真实交付物未通过磁盘验收。",
                        "next_step": (
                            "读取并执行匹配的 Skill，补齐产物后重新执行 complete 验收："
                            + "; ".join(completion_issues[:6])
                        )[:1200],
                        "completion_validation": {
                            "valid": False,
                            "issues": list(completion_issues),
                        },
                        "revision": int(progress.get("revision") or 0) + 1,
                        "updated_at": time(),
                    }
                )
                run.task_progress = progress
                rejection = _event(
                    run,
                    "agent_progress",
                    deepcopy(progress)
                    | {
                        "action": "compact",
                        "requested_action": "complete",
                        "work_item_id": "",
                        "message": "",
                    },
                )
                store.save(record)
                yield rejection
                return
            assistant_message = store.append_message(
                record,
                "assistant",
                response,
                run.id,
                session_id=run.session_id,
                task_id=run.task_id,
                kind="final",
                progress=deepcopy(run.task_progress),
                activity_events=_activity_events(run, activity_cursor),
            )
            if waiting_for_user:
                run.finished_at = None
                run.summary = run.summary or "Waiting for user confirmation."
            else:
                run.status = "succeeded"
                run.finished_at = time()
                run.summary = _run_summary(run)
            store.save(record)
            yield _event(
                run,
                "done",
                {
                    "assistant_message": assistant_message.model_dump(mode="json"),
                    "run": run.model_dump(mode="json"),
                    "context": record.context.model_dump(mode="json"),
                },
            )
        except GeneratorExit:
            _fail_cancelled_stream(record, run)
            raise
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.finished_at = time()
            run.error = str(exc)
            for question in record.context.questions:
                if question.get("continuation_run_id") != run.id:
                    continue
                question.pop("continued_at", None)
                question.pop("continuation_run_id", None)
            store.save(record)
            yield _event(run, "error", {"message": str(exc), "run": run.model_dump(mode="json")})

    def confirm(
        self,
        record: BusinessRecord,
        question_id: str | None,
        answer: str,
        accepted: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        matched_question = next(
            (item for item in record.context.questions if item.get("id") == question_id),
            None,
        )
        if (
            matched_question is not None
            and session_id
            and matched_question.get("session_id")
            and matched_question.get("session_id") != session_id
        ):
            raise ValueError("Question does not belong to this chat session.")
        resolved_session_id = (
            str(matched_question.get("session_id") or session_id or "")
            if matched_question is not None
            else str(session_id or "")
        ) or None
        resolved_run_id = (
            str(matched_question.get("run_id") or "") or None
            if matched_question is not None
            else None
        )
        confirmation = {
            "id": new_id("confirm"),
            "question_id": question_id,
            "run_id": resolved_run_id,
            "session_id": resolved_session_id,
            "answer": answer,
            "accepted": accepted,
            "created_at": time(),
            "source": "user",
        }
        record.context.confirmations.append(confirmation)
        if matched_question is not None:
            matched_question["status"] = "answered"
            matched_question["answer"] = answer
            matched_question["answered_at"] = time()
            if resolved_session_id and not matched_question.get("session_id"):
                matched_question["session_id"] = resolved_session_id
            matched_question.pop("continued_at", None)
            matched_question.pop("continuation_run_id", None)
        record.context.assumptions.append(
            {
                "id": new_id("assumption"),
                "statement": f"用户确认：{answer}",
                "confidence": 0.95 if accepted else 0.5,
                "source": "user_confirmation",
            }
        )
        record.status = "confirmed"
        store.create_version(record, "用户确认关键问题", "confirmation", actor="user")
        store.save(record)
        return confirmation

    def _new_run(
        self,
        record: BusinessRecord,
        model: str | None,
        session_id: str | None = None,
        *,
        task_id: str = "",
        segment_index: int = 1,
        continued_from_run_id: str | None = None,
        resumed_from_run_id: str | None = None,
    ) -> AIRun:
        run = AIRun(
            id=new_id("run"),
            business_id=record.id,
            session_id=session_id,
            task_id=task_id or new_id("task"),
            segment_index=max(1, segment_index),
            continued_from_run_id=continued_from_run_id,
            resumed_from_run_id=resumed_from_run_id,
            model=studio_settings.active_model_name(
                model,
                owner_id=record.owner_id,
            ),
            started_at=time(),
        )
        store.append_run(record, run)
        return run


orchestrator = BusinessOrchestrator()


def _progress_message_text(event: dict[str, Any]) -> str:
    explicit = str(event.get("message") or "").strip()
    if explicit:
        return explicit[:4000]

    action = str(event.get("action") or "update").casefold()
    objective = str(event.get("objective") or "").strip()
    title = str(event.get("title") or "").strip()
    summary = str(event.get("summary") or "").strip()
    result = str(event.get("result") or "").strip()
    verification = str(event.get("verification") or "").strip()
    next_step = str(event.get("next_step") or "").strip()
    if action == "plan":
        items = [
            str(item.get("title") or "").strip()
            for item in event.get("work_items") or []
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
        lines = [objective or summary or "我已经理解目标，正在按下面的步骤推进。"]
        if items:
            lines.append("\n" + "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1)))
        if next_step:
            lines.append(f"\n接下来：{next_step}")
        return "".join(lines)[:4000]

    lead = result or summary or title
    parts = [lead] if lead else []
    if verification and verification not in lead:
        parts.append(f"验收：{verification}")
    if next_step:
        parts.append(f"下一步：{next_step}")
    if action == "compact" and not parts:
        parts.append("当前阶段和检查点已经保存，我会在新的上下文中继续同一任务。")
    if action == "block" and not parts:
        parts.append("当前任务需要补充一项信息后才能继续。")
    return "\n\n".join(parts)[:4000]


def _append_failure_message(record: BusinessRecord, run: AIRun, error: str):
    existing = next(
        (
            item
            for item in reversed(record.messages)
            if item.run_id == run.id and item.kind == "error"
        ),
        None,
    )
    if existing is not None:
        return existing

    progress = run.task_progress or {}
    checkpoint = str(
        progress.get("result")
        or progress.get("summary")
        or progress.get("title")
        or ""
    ).strip()
    if not checkpoint:
        current_id = str(progress.get("current_work_item_id") or "")
        current_item = next(
            (
                item
                for item in progress.get("work_items") or []
                if isinstance(item, dict) and str(item.get("id") or "") == current_id
            ),
            None,
        )
        if current_item is not None:
            checkpoint = str(
                current_item.get("result")
                or current_item.get("title")
                or ""
            ).strip()
    next_step = str(progress.get("next_step") or "").strip()
    content = "当前任务在这一阶段中断，未达到最终验收标准。"
    if checkpoint:
        content += f"\n\n已经保存的最近进展：{checkpoint}"
    content += f"\n\n中断原因：{error[:1200]}"
    if next_step:
        content += f"\n\n继续处理时将从这里恢复：{next_step}"
    else:
        content += "\n\n已有消息和工作区检查点会保留，后续可以从当前阶段继续。"

    last_message = next(
        (item for item in reversed(record.messages) if item.run_id == run.id),
        None,
    )
    cutoff = last_message.created_at if last_message is not None else 0
    _reconcile_activity_events(run)
    activity_events = [
        deepcopy(event)
        for event in run.events
        if float(event.get("created_at") or 0) > cutoff
    ][-40:]
    message = store.append_message(
        record,
        "assistant",
        content,
        run.id,
        session_id=run.session_id,
        task_id=run.task_id,
        kind="error",
        progress_action="block",
        progress=deepcopy(progress),
        activity_events=activity_events,
    )
    store.save(record)
    return message


def _reconcile_activity_events(run: AIRun) -> None:
    """Close persisted running events from the authoritative invocation ledger."""

    invocations = {
        str(item.get("call_id") or ""): item
        for item in run.tool_invocations
        if str(item.get("call_id") or "")
    }
    for event in run.events:
        if str(event.get("status") or "") != "running":
            continue
        invocation = invocations.get(str(event.get("call_id") or ""))
        if invocation is None:
            continue
        status = str(invocation.get("status") or "")
        if status not in {"succeeded", "failed"}:
            continue
        event["status"] = status
        if status == "succeeded":
            event["output"] = str(invocation.get("summary") or "")[:4000]
            event.pop("error", None)
        else:
            event["error"] = str(invocation.get("error") or "")[:4000]


def _activity_events(run: AIRun, start: int = 0) -> list[dict[str, Any]]:
    _reconcile_activity_events(run)
    return deepcopy(run.events[max(0, start):])


def _event(run: AIRun, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = {
        "id": new_id("evt"),
        "type": event_type,
        "run_id": run.id,
        "session_id": run.session_id,
        "created_at": time(),
        **payload,
    }
    transient_file_delta = event_type == "file_operation" and payload.get("status") == "streaming"
    if event_type not in {"token", "reasoning", "model_call", "progress_message"} and not transient_file_delta:
        persisted = dict(event)
        if event_type in {"run_start", "done", "error"}:
            persisted.pop("run", None)
            persisted.pop("context", None)
            persisted.pop("assistant_message", None)
        run.events.append(_bounded_event_value(persisted))
    return event


def _bounded_event_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= 4000:
            return value
        return f"{value[:4000]}\n[truncated]"
    if isinstance(value, dict):
        return {str(key): _bounded_event_value(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [_bounded_event_value(item) for item in value[:50]]
        if len(value) > 50:
            items.append(f"[{len(value) - 50} more items omitted]")
        return items
    return value


def _fail_cancelled_stream(record: BusinessRecord, run: AIRun) -> None:
    if any(event.get("type") in {"done", "error"} for event in run.events):
        return
    run.status = "failed"
    run.finished_at = time()
    run.summary = "Agent stream cancelled."
    run.error = "Client disconnected before the agent stream completed."
    for question in record.context.questions:
        if question.get("continuation_run_id") != run.id:
            continue
        question.pop("continued_at", None)
        question.pop("continuation_run_id", None)
    store.save(record)


def _sync_workspace_metadata(record: BusinessRecord) -> None:
    """Synchronize factual workspace metadata without inferring business meaning."""

    record.context.name = record.name
    record.context.goal = record.goal
    known_requirements = {str(item.get("text") or "") for item in record.context.user_requirements}
    for text, source in ((record.goal, "business_goal"), (record.description, "business_description")):
        cleaned = text.strip()
        if cleaned and cleaned not in known_requirements:
            record.context.user_requirements.append(
                {"id": new_id("req"), "text": cleaned, "source": source, "created_at": time()}
            )
            known_requirements.add(cleaned)
    record.context.source_files = [
        {
            "id": file.id,
            "filename": file.filename,
            "suffix": file.suffix,
            "size": file.size,
            "parse_status": file.parse_status,
            "summary": file.summary,
            "columns": file.columns,
            "warnings": file.warnings,
        }
        for file in record.files
    ]
    current_file_ids = {file.id for file in record.files}
    record.context.tool_usages = [
        item
        for item in record.context.tool_usages
        if not item.get("source_file_id") or item.get("source_file_id") in current_file_ids
    ]


def _resume_source_run(
    record: BusinessRecord,
    session_id: str,
    requested_run_id: str | None,
) -> AIRun | None:
    if requested_run_id:
        match = next(
            (
                item
                for item in record.runs
                if item.id == requested_run_id and item.session_id == session_id
            ),
            None,
        )
        if match is None:
            raise KeyError(requested_run_id)
        return match
    return next(
        (
            item
            for item in reversed(record.runs)
            if item.session_id == session_id and item.status == "waiting_for_user"
        ),
        None,
    )


def _resume_answers(
    record: BusinessRecord,
    session_id: str,
    linked_questions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    candidates = linked_questions if linked_questions is not None else [
        item
        for item in record.context.questions
        if item.get("status") == "answered"
        and item.get("answer")
        and not item.get("continuation_run_id")
        and item.get("session_id") in {None, "", session_id}
    ]
    answers = [
        {
            "question_id": item.get("id"),
            "question": str(item.get("question") or "").strip(),
            "answer": str(item.get("answer") or "").strip(),
            "run_id": item.get("run_id"),
            "session_id": item.get("session_id") or session_id,
            "answered_at": item.get("answered_at") or item.get("created_at") or 0,
            "hitl_index": item.get("hitl_index"),
        }
        for item in candidates
        if item.get("status") == "answered"
        and not item.get("continuation_run_id")
        and str(item.get("answer") or "").strip()
    ]
    answers.sort(key=lambda item: float(item.get("answered_at") or 0))
    return answers[-12:]


def _resume_prompt(answers: list[dict[str, Any]]) -> str:
    answer_lines = "\n".join(
        f"- 问题：{item['question'] or item['question_id']}\n  用户确认：{item['answer']}"
        for item in answers
    )
    return render_prompt("runtime/resume.md", answers=answer_lines)


def _is_recoverable_segment_error(message: str, run: AIRun) -> bool:
    lowered = message.casefold()
    context_boundary = any(
        marker in lowered
        for marker in (
            "maximum context length",
            "context length exceeded",
            "context window",
            "too many tokens",
            "input is too long",
        )
    )
    if context_boundary:
        return True
    call_boundary = "model call limit" in lowered or "model call limits exceeded" in lowered
    return call_boundary and _has_durable_task_checkpoint(run)


def _has_durable_task_checkpoint(run: AIRun) -> bool:
    progress = run.task_progress or {}
    work_items = progress.get("work_items")
    has_work_item = isinstance(work_items, list) and any(
        isinstance(item, dict) and str(item.get("title") or "").strip()
        for item in work_items
    )
    has_checkpoint_text = any(
        str(progress.get(key) or "").strip()
        for key in ("objective", "summary", "result", "next_step")
    )
    has_artifact = bool(progress.get("artifacts"))
    has_semantic_checkpoint = has_work_item or has_checkpoint_text or has_artifact
    if has_semantic_checkpoint:
        return True

    for event in run.events:
        if event.get("status") != "succeeded":
            continue
        if event.get("type") == "skill_activation":
            return True
        if event.get("type") != "file_operation":
            continue
        path = str(event.get("path") or "")
        if path.startswith("/workspace/") and path != "/workspace/context/business_context.json":
            return True
    return False


def _is_manual_continuation_message(message: str) -> bool:
    value = re.sub(r"[\s，。,.！!？?]+", "", str(message or "").casefold())
    if len(value) > 24:
        return False
    return value in {
        "继续",
        "请继续",
        "继续吧",
        "继续处理",
        "继续完成",
        "继续运行",
        "接着做",
        "接着处理",
        "从断点继续",
        "继续上次任务",
        "恢复任务",
        "continue",
        "pleasecontinue",
        "resume",
        "keepgoing",
        "continuetask",
        "resumetask",
    }


def _manual_continuation_source(
    record: BusinessRecord,
    session_id: str,
    message: str,
) -> AIRun | None:
    if not _is_manual_continuation_message(message):
        return None
    session_runs = [run for run in record.runs if run.session_id == session_id]
    if not session_runs:
        return None

    latest = session_runs[-1]
    if not _run_accepts_manual_continuation(latest):
        return None

    # Older platform versions created a detached task for a bare "continue".
    # Recover the preceding interrupted task once, then future segments retain
    # the correct task_id and continued_from_run_id.
    if _is_manual_continuation_message(_task_user_prompt(record, latest, "")):
        previous = next(
            (
                run
                for run in reversed(session_runs[:-1])
                if run.task_id != latest.task_id and _run_accepts_manual_continuation(run)
            ),
            None,
        )
        if previous is not None:
            return previous
    return latest


def _run_accepts_manual_continuation(run: AIRun) -> bool:
    progress_status = str((run.task_progress or {}).get("status") or "").casefold()
    return (
        run.status == "failed"
        or progress_status in {"continuing", "running", "blocked"}
    ) and run.status != "waiting_for_user"


def _next_task_segment_index(record: BusinessRecord, task_id: str) -> int:
    return max(
        (run.segment_index for run in record.runs if run.task_id == task_id),
        default=0,
    ) + 1


def _task_original_prompt(record: BusinessRecord, run: AIRun, fallback: str) -> str:
    task_runs = [item for item in record.runs if item.task_id == run.task_id]
    first_started_at = min((item.started_at for item in task_runs), default=run.started_at)
    candidates = [
        item.content
        for item in record.messages
        if item.role == "user"
        and item.session_id == run.session_id
        and item.created_at <= first_started_at
    ]
    for prompt in reversed(candidates):
        if prompt.strip() and not _is_manual_continuation_message(prompt):
            return prompt
    return fallback


def _ensure_platform_task_checkpoint(
    record: BusinessRecord,
    run: AIRun,
    original_prompt: str,
) -> None:
    task_runs = [item for item in record.runs if item.task_id == run.task_id] or [run]
    skills = active_skill_names(record.runs, run.task_id)
    workspace_paths = _platform_workspace_paths(task_runs)
    succeeded = sum(
        1
        for item in task_runs
        for invocation in item.tool_invocations
        if invocation.get("status") == "succeeded"
    )
    failed = sum(
        1
        for item in task_runs
        for invocation in item.tool_invocations
        if invocation.get("status") == "failed"
    )
    validation = validate_task_completion(
        store.workspace_dir(record.id),
        prompt=original_prompt,
        active_skills=skills,
        owner_id=record.owner_id,
    )
    facts: list[str] = []
    if skills:
        facts.append("已激活 Skill：" + "、".join(skills))
    if succeeded or failed:
        facts.append(f"能力调用成功 {succeeded} 次、失败 {failed} 次")
    if workspace_paths:
        facts.append("最近工作区路径：" + "、".join(workspace_paths[-6:]))

    progress = deepcopy(run.task_progress or {})
    progress.update(
        {
            "task_id": run.task_id,
            "status": "continuing",
            "objective": str(progress.get("objective") or original_prompt)[:1000],
            "summary": str(progress.get("summary") or "；".join(facts) or "平台已保存运行检查点。")[:1200],
            "next_step": str(
                progress.get("next_step")
                or (
                    "从已激活 Skill 的有界摘要、状态文件和最近工作区路径恢复；"
                    "不要重复读取原始大文件或反复改写等价诊断命令。"
                )
            )[:1200],
            "platform_checkpoint": {
                "skills": list(skills),
                "workspace_paths": workspace_paths,
                "successful_invocations": succeeded,
                "failed_invocations": failed,
            },
            "revision": int(progress.get("revision") or 0) + 1,
            "updated_at": time(),
        }
    )
    if validation.required:
        progress["completion_validation"] = {
            "valid": validation.valid,
            "issues": list(validation.issues[:12]),
        }
        if validation.issues:
            progress["next_step"] = (
                str(progress["next_step"])
                + " 当前交付验收问题："
                + "; ".join(validation.issues[:6])
            )[:1200]
    run.task_progress = progress
    if not any(
        event.get("type") == "agent_progress" and event.get("checkpoint_source") == "platform"
        for event in run.events
    ):
        _event(
            run,
            "agent_progress",
            deepcopy(progress)
            | {
                "action": "compact",
                "checkpoint_source": "platform",
                "work_item_id": "",
                "message": "",
            },
        )


def _platform_workspace_paths(task_runs: list[AIRun]) -> list[str]:
    paths: list[str] = []
    for event in (event for run in task_runs for event in run.events):
        if event.get("type") != "file_operation" or event.get("status") != "succeeded":
            continue
        for key in ("path", "destination"):
            value = str(event.get(key) or "").strip()
            if (
                value.startswith("/workspace/")
                and value != "/workspace/context/business_context.json"
                and value not in paths
            ):
                paths.append(value)
    return paths[-20:]


def _progress_requests_continuation(run: AIRun) -> bool:
    return str(run.task_progress.get("status") or "").casefold() == "continuing"


def _completion_claim_issues(
    record: BusinessRecord,
    run: AIRun,
    prompt: str,
    response: str,
) -> tuple[str, ...]:
    """Reject positive completion claims that are not backed by workspace artifacts."""

    if not has_positive_completion_claim(response):
        return ()
    task_prompt = _task_user_prompt(record, run, prompt)
    progress = run.task_progress or {}
    validation = validate_task_completion(
        store.workspace_dir(record.id),
        artifacts=[str(item) for item in progress.get("artifacts") or []],
        prompt=task_prompt,
        active_skills=active_skill_names(record.runs, run.task_id),
        owner_id=record.owner_id,
    )
    if validation.required:
        return validation.issues if not validation.valid else ()

    claimed_paths = _workspace_paths_in_text(response)
    if claimed_paths:
        claimed_validation = validate_task_completion(
            store.workspace_dir(record.id),
            artifacts=claimed_paths,
            owner_id=record.owner_id,
        )
        return claimed_validation.issues if not claimed_validation.valid else ()

    if _looks_like_artifact_request(task_prompt) and not _task_has_successful_workspace_write(
        record, run.task_id
    ):
        return (
            "The request requires workspace deliverables, but the task has no successful "
            "workspace write and no verified artifact list.",
        )
    return ()


def _task_user_prompt(record: BusinessRecord, run: AIRun, fallback: str) -> str:
    for item in reversed(record.messages):
        if item.role != "user" or item.session_id != run.session_id:
            continue
        if item.created_at <= run.started_at:
            return item.content
    return fallback


def _workspace_paths_in_text(text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"/workspace/[^\s`'\"<>，。；;）)]+", str(text or "")):
        value = match.group(0).rstrip("/.,:")
        if value and value not in paths:
            paths.append(value)
    return paths[:20]


def _looks_like_artifact_request(prompt: str) -> bool:
    value = str(prompt or "").casefold()
    action = re.search(
        r"(?:生成|创建|实现|构建|制作|写入|修改|优化|导出|打包|蒸馏|推导|"
        r"create|generate|build|implement|write|modify|export|package)",
        value,
    )
    artifact = re.search(
        r"(?:文件|产物|交付|流程|图谱|报告|代码|脚本|能力包|skill|artifact|"
        r"file|report|code|script|workflow|diagram|package)",
        value,
    )
    return bool(action and artifact)


def _task_has_successful_workspace_write(record: BusinessRecord, task_id: str) -> bool:
    for candidate in record.runs:
        if candidate.task_id != task_id:
            continue
        for event in candidate.events:
            if event.get("type") != "file_operation" or event.get("status") != "succeeded":
                continue
            if event.get("mutating") and event.get("operation") in {
                "create",
                "edit",
                "move",
                "create_directory",
            }:
                return True
    return False


def _auto_continuation_prompt(
    record: BusinessRecord,
    original_prompt: str,
    source_run: AIRun,
    error: str,
) -> str:
    return render_prompt(
        "runtime/auto-continuation.md",
        segment_index=source_run.segment_index + 1,
        original_goal=original_prompt[:4000],
        continuation_error=error[:1000],
        task_manifest=_task_manifest_text(record, source_run, original_prompt),
    )


def _task_manifest_text(record: BusinessRecord, source_run: AIRun, original_prompt: str) -> str:
    task_runs = [item for item in record.runs if item.task_id == source_run.task_id] or [source_run]
    progress_events = [
        event
        for run in task_runs
        for event in run.events
        if event.get("type") in {"agent_progress", "plan", "task_handoff"}
    ]
    latest_progress = next(
        (event for event in reversed(progress_events) if event.get("type") == "agent_progress"),
        {},
    )
    objective = str(latest_progress.get("objective") or original_prompt).strip()[:1000]
    plan_items = _manifest_plan_items(task_runs, progress_events)
    progress_lines = _manifest_progress_lines(progress_events)
    invocation_lines = _manifest_invocation_lines(task_runs)
    artifact_lines = _manifest_artifact_lines(progress_events)
    skill_lines = "\n".join(
        f"  - {name}" for name in active_skill_names(record.runs, source_run.task_id)
    )
    workspace_lines = "\n".join(
        f"  - {path}" for path in _platform_workspace_paths(task_runs)[-12:]
    )
    validation = latest_progress.get("completion_validation") or {}
    validation_lines = "\n".join(
        f"  - {str(issue)[:500]}" for issue in (validation.get("issues") or [])[:8]
    )
    return "\n".join(
        [
            f"- 目标：{objective}",
            "- 已激活 Skill：",
            skill_lines or "  - 暂无。",
            "- 工作项：",
            plan_items or "  - 暂无显式计划；请先按任务需要建立一个简短计划。",
            "- 最近语义进展：",
            progress_lines or "  - 暂无语义进展；以工作区产物和最近成功调用为准。",
            "- 最近能力结果：",
            invocation_lines or "  - 暂无可压缩的能力结果。",
            "- 最近访问或变更的工作区路径：",
            workspace_lines or "  - 暂无。",
            "- 已声明产物/检查点：",
            artifact_lines or "  - 未显式声明；按相关 Skill 的默认输出目录和状态文件恢复。",
            "- 当前交付验收问题：",
            validation_lines or "  - 暂无已记录问题；完成前仍须运行 Skill 验收。",
        ]
    )


def _manifest_plan_items(task_runs: list[AIRun], progress_events: list[dict[str, Any]]) -> str:
    for event in reversed(progress_events):
        work_items = event.get("work_items")
        if isinstance(work_items, list) and work_items:
            lines = []
            for item in work_items[:12]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                status = str(item.get("status") or "pending").strip()
                expected = str(item.get("expected") or item.get("verification") or "").strip()
                suffix = f"；验收：{expected[:240]}" if expected else ""
                lines.append(f"  - [{status}] {title[:240]}{suffix}")
            if lines:
                return "\n".join(lines)
        if event.get("type") == "plan":
            items = [str(item).strip() for item in event.get("items") or [] if str(item).strip()]
            if items:
                return "\n".join(f"  - [pending] {item[:240]}" for item in items[:12])
    for run in reversed(task_runs):
        if run.plan:
            return "\n".join(f"  - [pending] {item[:240]}" for item in run.plan[:12])
    return ""


def _manifest_progress_lines(progress_events: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for event in progress_events[-8:]:
        if event.get("type") == "task_handoff":
            summary = str(event.get("summary") or "").strip()
            if summary:
                lines.append(f"  - [handoff] {summary[:360]}")
            continue
        if event.get("type") != "agent_progress":
            continue
        action = str(event.get("action") or event.get("status") or "update")
        text = str(
            event.get("summary")
            or event.get("result")
            or event.get("title")
            or event.get("next_step")
            or ""
        ).strip()
        if text:
            lines.append(f"  - [{action}] {text[:500]}")
    return "\n".join(lines[-8:])


def _manifest_invocation_lines(task_runs: list[AIRun]) -> str:
    invocations = [
        item
        for run in task_runs
        for item in run.tool_invocations
        if str(item.get("summary") or item.get("error") or "").strip()
    ][-8:]
    return "\n".join(
        f"  - [{item.get('status', 'done')}] {str(item.get('summary') or item.get('error'))[:500]}"
        for item in invocations
    )


def _manifest_artifact_lines(progress_events: list[dict[str, Any]]) -> str:
    artifacts: list[str] = []
    for event in progress_events:
        for item in event.get("artifacts") or []:
            value = str(item).strip()
            if value and value not in artifacts:
                artifacts.append(value)
    return "\n".join(f"  - {item[:500]}" for item in artifacts[-12:])


def _run_summary(run: AIRun) -> str:
    progress = _latest_progress_event(run)
    if progress:
        summary = str(
            progress.get("summary")
            or progress.get("result")
            or progress.get("title")
            or progress.get("next_step")
            or ""
        ).strip()
        if summary:
            return summary[:500]
    succeeded = sum(1 for item in run.tool_invocations if item.get("status") == "succeeded")
    failed = sum(1 for item in run.tool_invocations if item.get("status") == "failed")
    if not run.tool_invocations:
        return "模型完成直接回复，未调用外部能力。"
    return f"Agent 完成回复；能力调用成功 {succeeded} 个，失败 {failed} 个。"


def _latest_progress_event(run: AIRun) -> dict[str, Any]:
    return next(
        (event for event in reversed(run.events) if event.get("type") == "agent_progress"),
        {},
    )
