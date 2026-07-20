"""AI-centered orchestration for Business Studio."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from time import time
from typing import Any

from app.core.config import settings as env_settings
from app.studio.models import AIRun, BusinessRecord
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
        selected_model = studio_settings.active_model_name(model)
        user_message = store.append_message(record, "user", message, session_id=session.id)
        task_id = new_id("task")
        run = self._new_run(
            record,
            selected_model,
            session.id,
            task_id=task_id,
            segment_index=1,
        )
        store.save(record)

        yield _event(run, "message", {"message": user_message.model_dump(mode="json")})
        segment_prompt = message
        include_history = True
        segment_limit = max(1, env_settings.agent_auto_continuation_limit)
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
                    and run.segment_index < segment_limit
                    and _is_recoverable_segment_error(str(event.get("message") or ""), run)
                ):
                    continuation_error = str(event.get("message") or "")
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
                continuation_error = (
                    "Agent requested a fresh context after saving a bounded task checkpoint."
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
            segment_prompt = _auto_continuation_prompt(record, message, run, continuation_error)
            include_history = False
            run = next_run

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
            selected_model=studio_settings.active_model_name(model),
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
            model=studio_settings.active_model_name(model),
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
    return f"""用户已经完成待确认问题。请在同一任务上下文中继续之前的工作。

确认内容：
{answer_lines}

请先复核这些确认对 Business Context 和原任务的影响，按需调用真实 Tool、Skill 或 MCP 更新结果，然后完成原任务。不要要求用户重复回答，也不要把这段内部续跑提示描述成新的用户消息。"""


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
    has_progress_event = any(event.get("type") == "agent_progress" for event in run.events)
    return has_progress_event and (has_work_item or has_checkpoint_text or has_artifact)


def _progress_requests_continuation(run: AIRun) -> bool:
    return str(run.task_progress.get("status") or "").casefold() == "continuing"


def _auto_continuation_prompt(
    record: BusinessRecord,
    original_prompt: str,
    source_run: AIRun,
    error: str,
) -> str:
    return f"""这是同一用户任务的内部第 {source_run.segment_index + 1} 阶段。上一阶段达到单段上下文或执行边界，平台已切换到新的模型上下文；不要把它描述成新的用户请求。

平台职责边界：
- 平台只提供新模型上下文、工作区、Tool/Skill/MCP 调用、事件流和持久化检查点。
- 平台不定义这个业务任务应该怎么做；具体策略必须来自用户目标、已激活 Skill、Tool/MCP 返回结果和工作区产物。
- 多阶段接力的目的只是避免上下文过长，不是为了满足固定轮次或固定调用次数。

原始目标：
{original_prompt[:4000]}

上一阶段触发接力的原因：
{error[:1000]}

压缩后的任务恢复清单：
{_task_manifest_text(record, source_run, original_prompt)}

继续规则：
- 先恢复语义任务状态：查看上方任务清单、相关 Skill 的 SKILL.md，以及该 Skill 自己声明的有界状态/摘要产物。
- 不要为了重建上下文而重新读取原始大文件、完整证据库或旧运行日志；优先使用工作区检查点、摘要产物、校验错误和已生成结果。
- 从第一个未完成的工作项继续。若 `report_task_progress` 可用，先用它说明本阶段继续什么、为什么、预期验收是什么。
- 只有当你按用户目标和所用 Skill/Tool 的验收标准确认完成时，才报告 complete 并给最终答复；如果需要用户确认或外部状态，报告 block 并提出明确问题。
- 最终答复必须只面向用户说明结果、证据或阻塞，不要把内部续跑提示当成用户请求本身。
"""


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
    return "\n".join(
        [
            f"- 目标：{objective}",
            "- 工作项：",
            plan_items or "  - 暂无显式计划；请先按任务需要建立一个简短计划。",
            "- 最近语义进展：",
            progress_lines or "  - 暂无语义进展；以工作区产物和最近成功调用为准。",
            "- 最近能力结果：",
            invocation_lines or "  - 暂无可压缩的能力结果。",
            "- 已声明产物/检查点：",
            artifact_lines or "  - 未显式声明；按相关 Skill 的默认输出目录和状态文件恢复。",
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
