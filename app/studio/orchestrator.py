"""AI-centered orchestration for Business Studio."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from time import time
from typing import Any

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
        run = self._new_run(record, selected_model, session.id)
        store.save(record)

        yield _event(run, "message", {"message": user_message.model_dump(mode="json")})
        yield _event(run, "run_start", {"run": run.model_dump(mode="json")})
        yield from self._stream_run(record, run, selected_model, user_prompt=message)

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
            resumed_from_run_id=preparation.source_run_id,
        )
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
        resume_payload: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        response_parts: list[str] = []
        try:
            for payload in run_agent(
                record,
                run,
                requested_model=selected_model,
                user_prompt=user_prompt,
                resume_payload=resume_payload,
            ):
                event_type = payload.pop("type")
                if event_type == "token":
                    response_parts.append(str(payload.get("content") or ""))
                yield _event(run, event_type, payload)

            waiting_for_user = run.status == "waiting_for_user"
            response = "".join(response_parts).strip()
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
        resumed_from_run_id: str | None = None,
    ) -> AIRun:
        run = AIRun(
            id=new_id("run"),
            business_id=record.id,
            session_id=session_id,
            resumed_from_run_id=resumed_from_run_id,
            model=studio_settings.active_model_name(model),
            started_at=time(),
        )
        store.append_run(record, run)
        return run


orchestrator = BusinessOrchestrator()


def _event(run: AIRun, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = {
        "id": new_id("evt"),
        "type": event_type,
        "run_id": run.id,
        "session_id": run.session_id,
        "created_at": time(),
        **payload,
    }
    run.events.append(event)
    return event


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


def _run_summary(run: AIRun) -> str:
    succeeded = sum(1 for item in run.tool_invocations if item.get("status") == "succeeded")
    failed = sum(1 for item in run.tool_invocations if item.get("status") == "failed")
    if not run.tool_invocations:
        return "模型完成直接回复，未调用外部能力。"
    return f"Agent 完成回复；能力调用成功 {succeeded} 个，失败 {failed} 个。"
