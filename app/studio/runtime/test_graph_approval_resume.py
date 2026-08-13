"""Regression coverage for platform approval checkpoint resumption."""

from __future__ import annotations

from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from app.studio.models import AIRun, BusinessContext, BusinessRecord
from app.studio.runtime import graph


class _ApprovalStore:
    def __init__(self, persisted: BusinessRecord) -> None:
        self.persisted = persisted

    def refresh_distillation_artifact_contracts(self, _record: BusinessRecord) -> None:
        return None

    def ensure_distillation_approval_question(self, _record: BusinessRecord, **_kwargs: object):
        return ({"id": "q_distillation_approval"}, True)

    def require(self, _business_id: str, _owner_id: str) -> BusinessRecord:
        return self.persisted

    def save(self, _record: BusinessRecord) -> None:
        return None

    def workspace_dir(self, _business_id: str) -> Path:
        return Path.cwd()


class ApprovalResumeTests(TestCase):
    def test_approved_interrupt_revalidates_and_executes_original_tool(self) -> None:
        now = time()
        record = BusinessRecord(
            id="business_approval_test",
            name="Approval test",
            created_at=now,
            updated_at=now,
            context=BusinessContext(business_id="business_approval_test", name="Approval test"),
        )
        record.distillation.current_phase = "data_lineage"
        run = AIRun(
            id="run_approval_test",
            business_id=record.id,
            session_id="session_approval_test",
            task_id="task_approval_test",
            started_at=now,
        )
        record.runs.append(run)
        persisted = record.model_copy(deep=True)
        persisted.distillation.current_phase = "relations"
        store = _ApprovalStore(persisted)
        emitted: list[dict[str, object]] = []
        request = SimpleNamespace(
            state={},
            tool_call={"args": {"command": "python downstream.py"}},
            runtime=SimpleNamespace(stream_writer=emitted.append),
        )
        called: list[bool] = []

        def handler(_request: object) -> ToolMessage:
            called.append(True)
            return ToolMessage(content="ok", tool_call_id="call_approval_test", name="execute")

        def command_blocker(current: BusinessRecord, _command: str) -> str | None:
            return "approval required" if current.distillation.current_phase == "data_lineage" else None

        runtime = object.__new__(graph.StudioGraphRuntime)
        with (
            patch.object(graph, "store", store),
            patch.object(graph, "interrupt", return_value={"answers": [{"question_id": "q_distillation_approval"}]}),
            patch.object(graph, "distillation_command_blocker", side_effect=command_blocker),
            patch.object(graph, "_prepare_execute_command_for_gate", side_effect=lambda _record, command: (command, None, None, None)),
            patch.object(graph, "_workspace_file_state", return_value={}),
            patch.object(graph, "_emit_workspace_changes"),
            patch.object(graph, "_runtime_tool_failed", return_value=False),
            patch.object(graph, "_runtime_tool_summary", return_value="executed"),
        ):
            result = runtime._execute_runtime_tool(
                request,
                handler,
                record,
                run,
                call_id="call_approval_test",
                name="execute",
                arguments=request.tool_call["args"],
                skill_trace=graph._SkillTrace(run.id),
            )

        self.assertEqual("ok", result.content)
        self.assertEqual([True], called)
        self.assertEqual("relations", record.distillation.current_phase)
        self.assertFalse(any(event.get("status") == "failed" for event in emitted))
        self.assertTrue(any(event.get("status") == "succeeded" for event in emitted))

