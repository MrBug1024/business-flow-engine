"""Concurrency guard coverage for durable chat-resume claims."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest import TestCase

from app.studio.models import AIRun
from app.studio.storage import StudioStore


class ResumeClaimStorageTests(TestCase):
    def test_one_answered_checkpoint_can_only_create_one_active_resume_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir))
            record = store.create("Resume claim", owner_id="owner_resume_claim")
            session = store.require_chat_session(record)
            source = AIRun(
                id="source_resume_claim",
                business_id=record.id,
                session_id=session.id,
                task_id="task_resume_claim",
                status="waiting_for_user",
                started_at=time(),
            )
            record.runs.append(source)
            record.context.questions.append(
                {
                    "id": "q_resume_claim",
                    "run_id": source.id,
                    "session_id": session.id,
                    "status": "answered",
                    "answer": "continue",
                }
            )
            store.save(record)

            claimed = store.claim_chat_resume(
                business_id=record.id,
                owner_id=record.owner_id,
                session_id=session.id,
                source_run_id=source.id,
                selected_model="test-model",
                question_ids=("q_resume_claim",),
            )

            self.assertEqual(source.id, claimed["run"].resumed_from_run_id)
            with self.assertRaisesRegex(ValueError, "already resuming"):
                store.claim_chat_resume(
                    business_id=record.id,
                    owner_id=record.owner_id,
                    session_id=session.id,
                    source_run_id=source.id,
                    selected_model="test-model",
                    question_ids=("q_resume_claim",),
                )

            # A transport/provider failure before the resumed graph consumes
            # the answer must leave a retryable checkpoint, not a permanent
            # "already resuming" state.
            claimed["run"].status = "failed"
            store.save(claimed["record"])
            store.release_chat_resume_claim(
                business_id=record.id,
                owner_id=record.owner_id,
                source_run_id=source.id,
                continuation_run_id=claimed["run"].id,
            )
            retried = store.claim_chat_resume(
                business_id=record.id,
                owner_id=record.owner_id,
                session_id=session.id,
                source_run_id=source.id,
                selected_model="test-model",
                question_ids=("q_resume_claim",),
            )
            self.assertNotEqual(claimed["run"].id, retried["run"].id)
