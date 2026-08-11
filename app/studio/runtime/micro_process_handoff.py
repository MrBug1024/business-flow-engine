"""Server-owned handoff from relationship review to micro-process approval.

The Agent must not decide whether an already-approved relationship candidate
can skip straight to a business-flow derivation.  This small runtime service
recognises that exact transition, runs only the immutable micro-process draft
command, and then lets the existing signed approval flow take over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.studio.capabilities.registry import list_skills, materialize_skill_view
from app.studio.distillation_gates import distillation_command_blocker
from app.studio.models import BusinessRecord
from app.studio.runtime.sandbox import SandboxError, sandbox_manager
from app.studio.storage import store


_RELATIONS_SKILL_NAME = "discover-data-relations"
_RELATIONS_SKILL_SCRIPT = "/skills/discover-data-relations/scripts/analyze_relations.py"
_MICRO_PROCESS_OUTPUT = "/workspace/outputs/data-relations/micro-process.json"
MICRO_PROCESS_DRAFT_COMMAND = (
    f"python {_RELATIONS_SKILL_SCRIPT} micro-process-draft"
    " --review /workspace/outputs/data-relations/trace-review.json"
    f" --output {_MICRO_PROCESS_OUTPUT}"
)
_BUSINESS_FLOW_SCRIPT = re.compile(
    r"derive_business_flow\.py[\"']?\s+(?P<action>[a-z][a-z0-9_-]*)",
    re.IGNORECASE,
)
_READ_ONLY_FLOW_ACTIONS = frozenset({"brief", "summary", "relation", "chain"})
_FLOW_REQUEST_MARKERS = (
    "business-flow",
    "business flow",
    "derive business flow",
    "businessflow",
    "业务流程",
    "推导流程",
    "流程推导",
)
_FLOW_ACTION_MARKERS = (
    "derive",
    "generate",
    "start",
    "prepare",
    "continue",
    "推导",
    "生成",
    "开始",
    "继续",
)


@dataclass(frozen=True, slots=True)
class MicroProcessHandoff:
    """Result of a deterministic transition attempt.

    ``handled`` means the caller must not ask the model to choose a workaround.
    A non-empty ``question`` is a platform-signed approval action; ``detail``
    is otherwise the bounded, user-facing failure reason.
    """

    handled: bool
    drafted: bool = False
    question: dict[str, Any] | None = None
    detail: str = ""
    command: str = MICRO_PROCESS_DRAFT_COMMAND


def is_business_flow_start_command(command: str) -> bool:
    """Return true only for a candidate-forming derive-business-flow command."""

    match = _BUSINESS_FLOW_SCRIPT.search(str(command or "").replace("\\", "/"))
    return bool(match and match.group("action").casefold() not in _READ_ONLY_FLOW_ACTIONS)


def is_business_flow_start_request(message: str | None) -> bool:
    """Recognise an explicit request to start the business-flow stage.

    This intentionally requires both a flow target and an action marker for
    natural language.  It prevents an ordinary discussion mentioning a flow
    from unexpectedly materialising a review artifact.
    """

    text = str(message or "").casefold()
    if not text:
        return False
    normalized = re.sub(r"\s+", " ", text)
    has_target = any(marker.casefold() in normalized for marker in _FLOW_REQUEST_MARKERS)
    has_action = any(marker.casefold() in normalized for marker in _FLOW_ACTION_MARKERS)
    return has_target and has_action


def resume_targets_micro_process_gate(
    record: BusinessRecord,
    resume_payload: dict[str, Any] | None,
) -> bool:
    """Recognise the legacy Agent clarification produced by the old gate path."""

    if record.distillation.current_phase != "micro_process" or not isinstance(resume_payload, dict):
        return False
    source_run_id = str(resume_payload.get("source_run_id") or "")
    if not source_run_id:
        return False
    for item in record.context.questions:
        if item.get("source") != "agent":
            continue
        if source_run_id not in {
            str(item.get("run_id") or ""),
            str(item.get("checkpoint_run_id") or ""),
        }:
            continue
        if _looks_like_micro_process_gate_question(item):
            return True
    return False


def prepare_micro_process_handoff(
    record: BusinessRecord,
    *,
    session_id: str | None,
    run_id: str = "",
    checkpoint_run_id: str = "",
    tool_call_id: str = "",
) -> MicroProcessHandoff:
    """Create or surface the only legitimate micro-process review transition.

    The command is fixed in this module rather than derived from a prompt or
    user text.  We validate its canonical workspace output through the normal
    storage contract before a platform approval question is created.
    """

    if record.distillation.current_phase != "micro_process":
        return MicroProcessHandoff(handled=False)

    store.refresh_distillation_artifact_contracts(record)
    contract = record.distillation.artifact_contracts.get("micro_process", {})
    candidate_ready = bool(isinstance(contract, dict) and contract.get("reviewable"))
    drafted = False

    if not candidate_ready:
        status = str(contract.get("status") or "missing") if isinstance(contract, dict) else "missing"
        if status != "missing":
            detail = str(contract.get("detail") or "") if isinstance(contract, dict) else ""
            return MicroProcessHandoff(
                handled=True,
                detail=(
                    "The current micro-process candidate cannot be submitted for approval. "
                    + (detail or "Its canonical artifact is not reviewable.")
                )[:2000],
            )
        upstream_blocker = distillation_command_blocker(record, MICRO_PROCESS_DRAFT_COMMAND)
        if upstream_blocker:
            return MicroProcessHandoff(handled=True, detail=upstream_blocker[:2000])
        try:
            skills_root = _immutable_relations_skill_view(record)
            backend = sandbox_manager.backend_for(
                business_id=record.id,
                workspace_root=store.workspace_dir(record.id),
                skills_root=skills_root,
            )
            execution = backend.execute(MICRO_PROCESS_DRAFT_COMMAND)
        except Exception:  # noqa: BLE001 - do not expose managed-runtime internals to the chat.
            return MicroProcessHandoff(
                handled=True,
                detail=(
                    "The platform could not prepare the managed runtime for the micro-process "
                    "review candidate. Check the current workspace and managed Skill configuration."
                ),
            )
        try:
            exit_code = int(getattr(execution, "exit_code", 1))
        except (TypeError, ValueError):
            exit_code = 1
        if exit_code != 0:
            return MicroProcessHandoff(
                handled=True,
                detail=_execution_failure_detail(execution),
            )
        drafted = True
        store.refresh_distillation_artifact_contracts(record)

    question, _changed = store.ensure_distillation_approval_question(
        record,
        session_id=session_id,
        run_id=run_id,
        checkpoint_run_id=checkpoint_run_id,
        tool_call_id=tool_call_id,
    )
    if question is None:
        refreshed = record.distillation.artifact_contracts.get("micro_process", {})
        detail = str(refreshed.get("detail") or "") if isinstance(refreshed, dict) else ""
        return MicroProcessHandoff(
            handled=True,
            drafted=drafted,
            detail=(
                "The micro-process candidate was generated, but the platform could not create its "
                "signed approval action. " + (detail or "Refresh the current stage and correct the reported artifact issue.")
            )[:2000],
        )

    # A legacy model clarification is not a review decision.  Retire it once
    # the authoritative platform action exists, so the UI has a single next
    # step and a resumed legacy run cannot keep showing a false choice.
    store.supersede_micro_process_gate_bypass_questions(
        record,
        session_id=session_id or "",
        run_id=run_id,
        checkpoint_run_id=checkpoint_run_id,
    )
    store.save(record)
    return MicroProcessHandoff(
        handled=True,
        drafted=drafted,
        question=question,
        detail=(
            "Created the micro-process review candidate; waiting for the platform approval decision."
            if drafted
            else "A current micro-process review candidate is waiting for the platform approval decision."
        ),
    )


def _immutable_relations_skill_view(record: BusinessRecord):
    definitions = [
        item
        for item in list_skills(record.owner_id)
        if item.name == _RELATIONS_SKILL_NAME
    ]
    if len(definitions) != 1 or definitions[0].kind != "system" or not definitions[0].locked:
        raise SandboxError(
            "The locked discover-data-relations system Skill is unavailable for micro-process drafting."
        )
    try:
        skills_root = materialize_skill_view(record.owner_id).resolve()
        script = (skills_root / _RELATIONS_SKILL_NAME / "scripts" / "analyze_relations.py").resolve()
    except (OSError, ValueError) as exc:
        raise SandboxError("Unable to materialize the locked relationship Skill.") from exc
    if skills_root not in script.parents or not script.is_file():
        raise SandboxError("The locked relationship Skill has no immutable analyze entry point.")
    return skills_root


def _execution_failure_detail(execution: Any) -> str:
    try:
        exit_code = int(getattr(execution, "exit_code", 1))
    except (TypeError, ValueError):
        exit_code = 1
    return (
        "The platform could not create the micro-process review candidate "
        f"(managed sandbox exit code {exit_code}). Review the current stage prerequisites and retry."
    )


def _looks_like_micro_process_gate_question(question: dict[str, Any]) -> bool:
    explicit_phase = str(question.get("distillation_phase") or "")
    if explicit_phase == "micro_process":
        return True
    text = " ".join(
        str(question.get(key) or "")
        for key in ("question", "reason", "category", "detail")
    ).casefold()
    has_micro_phase = "micro_process" in text or "微观" in text
    has_flow_target = any(marker.casefold() in text for marker in _FLOW_REQUEST_MARKERS)
    return has_micro_phase and has_flow_target


__all__ = [
    "MICRO_PROCESS_DRAFT_COMMAND",
    "MicroProcessHandoff",
    "is_business_flow_start_command",
    "is_business_flow_start_request",
    "prepare_micro_process_handoff",
    "resume_targets_micro_process_gate",
]
