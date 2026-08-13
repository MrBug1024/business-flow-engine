"""Regression coverage for answer/approval continuation behavior."""

from __future__ import annotations

from time import time
from types import MethodType
from unittest import TestCase
from unittest.mock import patch

from app.studio import orchestrator as orchestrator_module
from app.studio.models import AIRun, BusinessContext, BusinessRecord, ChatSession


class _InMemoryStore:
    def __init__(self, record: BusinessRecord, session: ChatSession) -> None:
        self.record = record
        self.session = session
        self.claimed_run: AIRun | None = None
        self.released_run_ids: list[str] = []

    def require_chat_session(self, _record: BusinessRecord, _session_id: str | None = None) -> ChatSession:
        return self.session

    def save(self, _record: BusinessRecord) -> None:
        return None

    def claim_chat_resume(self, **kwargs: object) -> dict[str, object]:
        source_run_id = str(kwargs["source_run_id"])
        source = next(item for item in self.record.runs if item.id == source_run_id)
        if self.claimed_run is not None:
            raise ValueError("This waiting task is already resuming.")
        self.claimed_run = AIRun(
            id="claimed_resume_run",
            business_id=self.record.id,
            session_id=self.session.id,
            task_id=source.task_id,
            segment_index=source.segment_index,
            resumed_from_run_id=source.id,
            model=str(kwargs["selected_model"]),
            plan=list(source.plan),
            task_progress=dict(source.task_progress),
            started_at=time(),
        )
        self.record.runs.append(self.claimed_run)
        return {"record": self.record, "source_run": source, "run": self.claimed_run}

    def release_chat_resume_claim(self, **kwargs: object) -> BusinessRecord:
        self.released_run_ids.append(str(kwargs["continuation_run_id"]))
        return self.record


class ResumeContinuationTests(TestCase):
    def test_answered_checkpoint_automatically_runs_the_next_task_segment(self) -> None:
        now = time()
        record = BusinessRecord(
            id="business_resume_test",
            name="Resume test",
            created_at=now,
            updated_at=now,
            context=BusinessContext(business_id="business_resume_test", name="Resume test"),
            chat_sessions=[
                ChatSession(
                    id="session_resume_test",
                    business_id="business_resume_test",
                    created_at=now,
                    updated_at=now,
                )
            ],
        )
        source_run = AIRun(
            id="source_run",
            business_id=record.id,
            session_id="session_resume_test",
            task_id="task_resume_test",
            segment_index=1,
            status="waiting_for_user",
            started_at=now,
        )
        record.runs.append(source_run)
        record.context.questions.append(
            {
                "id": "q_resume_test",
                "run_id": source_run.id,
                "session_id": source_run.session_id,
                "status": "answered",
                "answer": "approved",
            }
        )
        preparation = orchestrator_module.ResumePreparation(
            session_id="session_resume_test",
            source_run_id=source_run.id,
            selected_model="test-model",
            question_ids=("q_resume_test",),
            answers=({"question_id": "q_resume_test", "answer": "approved"},),
            prompt="Continue after the user's answer: approved.",
        )
        orchestrator = orchestrator_module.BusinessOrchestrator()
        stream_calls: list[tuple[str, dict[str, object]]] = []
        created_runs: list[AIRun] = []

        def fake_new_run(
            _self: object,
            business: BusinessRecord,
            model: str | None,
            session_id: str | None = None,
            **kwargs: object,
        ) -> AIRun:
            run = AIRun(
                id=f"next_run_{len(created_runs) + 1}",
                business_id=business.id,
                session_id=session_id,
                task_id=str(kwargs.get("task_id") or "task_resume_test"),
                segment_index=int(kwargs.get("segment_index") or 1),
                continued_from_run_id=kwargs.get("continued_from_run_id") or None,
                resumed_from_run_id=kwargs.get("resumed_from_run_id") or None,
                model=model or "test-model",
                started_at=time(),
            )
            business.runs.append(run)
            created_runs.append(run)
            return run

        def fake_stream_run(
            _self: object,
            _business: BusinessRecord,
            run: AIRun,
            _model: str,
            **kwargs: object,
        ):
            stream_calls.append((run.id, kwargs))
            if len(stream_calls) == 1:
                self.assertIsNotNone(kwargs.get("resume_payload"))
                run.task_progress = {"status": "continuing", "objective": "finish work"}
                if False:
                    yield {}
                return
            self.assertIsNone(kwargs.get("resume_payload"))
            self.assertFalse(bool(kwargs.get("include_history")))
            self.assertIn("approved", str(kwargs.get("user_prompt") or ""))
            run.status = "succeeded"
            run.task_progress = {"status": "completed"}
            yield {"type": "done"}

        orchestrator._new_run = MethodType(fake_new_run, orchestrator)  # type: ignore[method-assign]
        orchestrator._stream_run = MethodType(fake_stream_run, orchestrator)  # type: ignore[method-assign]
        memory_store = _InMemoryStore(record, record.chat_sessions[0])
        with patch.object(orchestrator_module, "store", memory_store):
            events = list(orchestrator.stream_resume(record, preparation))

        self.assertEqual(2, len(stream_calls))
        self.assertEqual("source_run", stream_calls[0][1]["resume_payload"]["source_run_id"])
        self.assertTrue(any(event["type"] == "task_handoff" for event in events))
        self.assertTrue(any(event["type"] == "done" for event in events))
        self.assertEqual("succeeded", source_run.status)
        self.assertEqual("claimed_resume_run", record.context.questions[0]["continuation_run_id"])
        self.assertEqual("claimed_resume_run", created_runs[0].continued_from_run_id)

    def test_new_question_stops_resume_even_when_old_progress_requests_continuation(self) -> None:
        now = time()
        record = BusinessRecord(
            id="business_resume_question_test",
            name="Resume question test",
            created_at=now,
            updated_at=now,
            context=BusinessContext(
                business_id="business_resume_question_test",
                name="Resume question test",
            ),
            chat_sessions=[
                ChatSession(
                    id="session_resume_question_test",
                    business_id="business_resume_question_test",
                    created_at=now,
                    updated_at=now,
                )
            ],
        )
        source_run = AIRun(
            id="source_question_run",
            business_id=record.id,
            session_id="session_resume_question_test",
            task_id="task_resume_question_test",
            status="waiting_for_user",
            started_at=now,
        )
        record.runs.append(source_run)
        record.context.questions.append(
            {
                "id": "q_answered_before_resume",
                "run_id": source_run.id,
                "session_id": source_run.session_id,
                "status": "answered",
                "answer": "continue",
            }
        )
        preparation = orchestrator_module.ResumePreparation(
            session_id=source_run.session_id or "",
            source_run_id=source_run.id,
            selected_model="test-model",
            question_ids=("q_answered_before_resume",),
            answers=({"question_id": "q_answered_before_resume", "answer": "continue"},),
            prompt="Continue after the user's answer.",
        )
        orchestrator = orchestrator_module.BusinessOrchestrator()

        def fake_stream_run(
            _self: object,
            _business: BusinessRecord,
            run: AIRun,
            _model: str,
            **_kwargs: object,
        ):
            run.task_progress = {"status": "continuing"}
            run.status = "waiting_for_user"
            yield {"type": "done"}

        def unexpected_next_run(*_args: object, **_kwargs: object) -> AIRun:
            raise AssertionError("A new user question must stop automatic continuation.")

        orchestrator._stream_run = MethodType(fake_stream_run, orchestrator)  # type: ignore[method-assign]
        orchestrator._new_run = MethodType(unexpected_next_run, orchestrator)  # type: ignore[method-assign]
        memory_store = _InMemoryStore(record, record.chat_sessions[0])
        with patch.object(orchestrator_module, "store", memory_store):
            events = list(orchestrator.stream_resume(record, preparation))

        self.assertTrue(any(event["type"] == "done" for event in events))
        self.assertFalse(any(event["type"] == "task_handoff" for event in events))
        self.assertEqual("succeeded", source_run.status)

    def test_unexpected_resume_failure_releases_claim_for_a_later_retry(self) -> None:
        now = time()
        record = BusinessRecord(
            id="business_resume_failure_test",
            name="Resume failure test",
            created_at=now,
            updated_at=now,
            context=BusinessContext(
                business_id="business_resume_failure_test",
                name="Resume failure test",
            ),
            chat_sessions=[
                ChatSession(
                    id="session_resume_failure_test",
                    business_id="business_resume_failure_test",
                    created_at=now,
                    updated_at=now,
                )
            ],
        )
        source_run = AIRun(
            id="source_failure_run",
            business_id=record.id,
            session_id="session_resume_failure_test",
            task_id="task_resume_failure_test",
            status="waiting_for_user",
            started_at=now,
        )
        record.runs.append(source_run)
        record.context.questions.append(
            {
                "id": "q_answered_before_failure",
                "run_id": source_run.id,
                "session_id": source_run.session_id,
                "status": "answered",
                "answer": "continue",
            }
        )
        preparation = orchestrator_module.ResumePreparation(
            session_id=source_run.session_id or "",
            source_run_id=source_run.id,
            selected_model="test-model",
            question_ids=("q_answered_before_failure",),
            answers=({"question_id": "q_answered_before_failure", "answer": "continue"},),
            prompt="Continue after the user's answer.",
        )
        orchestrator = orchestrator_module.BusinessOrchestrator()

        def failing_stream_run(*_args: object, **_kwargs: object):
            raise RuntimeError("provider unexpectedly failed")
            yield {}  # pragma: no cover - keeps this helper a generator.

        orchestrator._stream_run = MethodType(failing_stream_run, orchestrator)  # type: ignore[method-assign]
        memory_store = _InMemoryStore(record, record.chat_sessions[0])
        with patch.object(orchestrator_module, "store", memory_store):
            with self.assertRaisesRegex(RuntimeError, "provider unexpectedly failed"):
                list(orchestrator.stream_resume(record, preparation))

        self.assertIsNotNone(memory_store.claimed_run)
        self.assertEqual("failed", memory_store.claimed_run.status)
        self.assertEqual(["claimed_resume_run"], memory_store.released_run_ids)
