"""Runtime gates for the evidence-driven business-distillation lifecycle.

The model may prepare a candidate, but it must never advance the scenario by
silently running a downstream Skill.  The durable, identity-bound approvals
live on :class:`BusinessRecord`; this module turns them into a small command
gate used directly by the Agent runtime.

Keeping this policy outside prompt text is deliberate.  A prompt can be
ignored or misunderstood, whereas a blocked sandbox call cannot generate a
downstream artifact from unreviewed evidence.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from typing import Any

from app.studio.models import BusinessRecord, DistillationPhase


_SCRIPT_ACTION = re.compile(
    r"(?P<script>analyze_relations|derive_business_flow|distill_capabilities)\.py"
    r"[\"']?\s+(?P<action>[a-z][a-z0-9_-]*)",
    re.IGNORECASE,
)
_SANDBOX_ROLE_MANIFEST = "/workspace/outputs/data-relations/approved-role-manifest.json"
_PROMPT_NOTE_LIMIT = 600
_DIRECT_COMMAND_UNSAFE_SELECTOR_MARKERS = ("\n", "\r", ";", "|", "&", "<", ">", "`", "$", "%", "^", "!")
_PHASE_LABELS: dict[DistillationPhase, str] = {
    "file_roles": "文件、工作表角色矩阵",
    "data_lineage": "结果锚点数据链路样本",
    "relations": "数据关联关系",
    "micro_process": "微观复现契约",
    "business_flow": "业务流程",
    "capability": "能力方案",
    "package": "历史回放与发布",
}


def _bounded_prompt_text(value: Any, limit: int = _PROMPT_NOTE_LIMIT) -> str:
    """Keep durable reviewer notes intact while bounding the model-only summary."""

    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _active_approval(record: BusinessRecord, phase: DistillationPhase) -> bool:
    state = record.distillation
    return any(
        item.phase == phase
        and item.decision == "approved"
        and item.status == "active"
        # A downstream correction (for example choosing a different result
        # anchor) creates a new workbench revision, but it does not erase a
        # still-valid approval of the unchanged source snapshot.  Storage
        # explicitly invalidates an approval whenever its phase or an
        # upstream source changes, so source revision + active status is the
        # durable authority here rather than the cosmetic workbench revision.
        and item.source_revision == state.source_revision
        for item in state.approvals
    )


def _stale_approval_reason(record: BusinessRecord, phase: DistillationPhase) -> str | None:
    """Detect an approved candidate whose bytes changed after review.

    ``graph.py`` refreshes server-computed artifact contracts immediately
    before this gate is evaluated.  Keeping the comparison here makes a
    stale output a hard execution stop rather than an advisory warning in a
    UI panel.  Missing contracts are tolerated for pure unit callers; the
    runtime path always supplies one for workspace artifacts.
    """

    state = record.distillation
    approvals = [
        item for item in state.approvals
        if item.phase == phase
        and item.decision == "approved"
        and item.status == "active"
        and item.source_revision == state.source_revision
    ]
    if not approvals:
        return None
    current_fingerprint = ""
    if phase == "file_roles":
        current_fingerprint = str(state.role_manifest_fingerprint or "")
    else:
        contract = state.artifact_contracts.get(str(phase))
        if isinstance(contract, dict):
            current_fingerprint = str(contract.get("fingerprint") or "")
    if not current_fingerprint:
        return None
    if any(item.artifact_fingerprint == current_fingerprint for item in approvals):
        return None
    return (
        f"Blocked: the approved {phase} artifact fingerprint no longer matches its current bytes. "
        "The candidate was changed after review; return to that Workbench phase for correction and fresh approval."
    )


def _approval_blocker(phase: DistillationPhase, next_action: str) -> str:
    return (
        f"Blocked by the evidence gate: {_PHASE_LABELS[phase]} has not been approved for the current "
        f"scenario revision. {next_action} must wait for a signed decision in the platform approval dialog. "
        "The dialog is opened automatically after the current artifact passes server validation. "
        "AI text, a CLI --reviewer value, or a stale approval cannot substitute for that approval."
    )


def _require_approval(record: BusinessRecord, phase: DistillationPhase, next_action: str) -> str | None:
    stale = _stale_approval_reason(record, phase)
    if stale:
        return stale
    if not _active_approval(record, phase):
        return _approval_blocker(phase, next_action)
    return None


def _phase_order_blocker(record: BusinessRecord, expected: DistillationPhase, next_action: str) -> str | None:
    current = record.distillation.current_phase
    if current == expected:
        return None
    return (
        f"Blocked by the staged workbench: {next_action} belongs to `{expected}`, but the current "
        f"review phase is {_PHASE_LABELS[current]} (`{current}`). Do not generate or overwrite a "
        "downstream candidate. If the current artifact is reviewable, the platform will open its "
        "approval dialog; otherwise revise or generate that current-phase artifact first."
    )


def distillation_command_blocker(record: BusinessRecord, command: str) -> str | None:
    """Return a fail-closed reason when a sandbox command skips a review gate.

    Read-only summary commands are deliberately left alone.  Candidate-forming
    commands are allowed only after their *upstream* evidence is approved; the
    candidate itself still awaits the next Workbench checkpoint.
    """

    normalized = str(command or "").replace("\\", "/").casefold()
    if not normalized:
        return None

    # These legacy CLI actions accepted an arbitrary reviewer string.  They
    # are explicitly barred even after a real reviewer has approved a phase;
    # only the platform API may mint the signed approval receipt.
    if "/skills/discover-data-relations/" in normalized and (
        "trace-review-approve" in normalized or "micro-process-approve" in normalized
    ):
        return (
            "Blocked: CLI review approval is not trusted. Use the approval dialog opened in the AI chat so "
            "the platform can bind the authenticated reviewer, artifact fingerprint, and current revision."
        )

    match = _SCRIPT_ACTION.search(normalized)
    if match is None:
        return None
    script = match.group("script")
    action = match.group("action")

    if script == "analyze_relations":
        if action == "analyze":
            phase_blocker = _phase_order_blocker(record, "data_lineage", "Result-anchored tracing")
            if phase_blocker:
                return phase_blocker
            approval_blocker = _require_approval(record, "file_roles", "Result-anchored tracing")
            if approval_blocker:
                return approval_blocker
            return None
        if action in {"trace-review-init", "trace-review-correct"}:
            return _phase_order_blocker(record, "data_lineage", "Trace review correction")
        if action in {"claims-init", "claims-copy", "claims-node", "claims-edge", "claims-chain", "claims-branch", "claims-coverage", "claims-exclusion", "claims-remove", "claims-recover", "preflight", "finalize"}:
            phase_blocker = _phase_order_blocker(record, "relations", "Relationship synthesis")
            if phase_blocker:
                return phase_blocker
            approval_blocker = _require_approval(record, "data_lineage", "Relationship synthesis")
            if approval_blocker:
                return approval_blocker
            return None
        if action == "micro-process-draft":
            phase_blocker = _phase_order_blocker(record, "micro_process", "Micro-process reconstruction")
            if phase_blocker:
                return phase_blocker
            approval_blocker = _require_approval(record, "relations", "Micro-process reconstruction")
            if approval_blocker:
                return approval_blocker
            return None
        # evidence/brief/summary/status and correction inspection are read-only
        # from a release perspective.  The file-level script itself rejects
        # attempts to manufacture approval data.
        return None

    if script == "derive_business_flow":
        if action not in {"brief", "summary", "relation", "chain"}:
            phase_blocker = _phase_order_blocker(record, "business_flow", "Business-flow derivation")
            if phase_blocker:
                return phase_blocker
            approval_blocker = _require_approval(record, "micro_process", "Business-flow derivation")
            if approval_blocker:
                return approval_blocker
        return None

    if script == "distill_capabilities":
        if action not in {"brief", "summary"}:
            phase_blocker = _phase_order_blocker(record, "capability", "Capability distillation")
            if phase_blocker:
                return phase_blocker
            approval_blocker = _require_approval(record, "business_flow", "Capability distillation")
            if approval_blocker:
                return approval_blocker
        return None

    return None


def _anchor_selector_payload(record: BusinessRecord) -> dict[str, Any] | None:
    """Return the current user choice without exposing reviewer metadata."""

    state = record.distillation
    selection: Any = getattr(state, "anchor_selector", None)
    if hasattr(selection, "model_dump"):
        selection = selection.model_dump()
    if not isinstance(selection, dict):
        return None
    try:
        file = str(selection.get("file") or "").strip()
        table = str(selection.get("table") or "").strip()
        row_number = int(selection.get("row_number"))
        source_revision = int(selection.get("source_revision"))
    except (TypeError, ValueError):
        return None
    if not file or not table or row_number <= 0 or source_revision != state.source_revision:
        return None
    return {"file": file, "table": table, "row_number": row_number}


def _quote_direct_selector_argument(value: str) -> str:
    """Quote one JSON selector for the native shell used by the sandbox.

    ``shlex.quote`` produces POSIX single quotes, which CMD does not strip.
    The managed Studio runtime can execute on Windows, so use the same Windows
    argv quoting primitive when necessary.  The caller rejects command-shell
    metacharacters before reaching this helper; selector text is never an
    arbitrary shell fragment.
    """

    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def apply_selected_trace_anchor(
    record: BusinessRecord,
    command: str,
) -> tuple[str, str | None]:
    """Inject the persisted result anchor into a direct tracing command.

    The selected row is an auditable user decision, not a model suggestion.
    The runtime therefore appends it itself rather than trusting the Agent to
    remember the row or allowing a different selector to leak into the run.
    Compound shell commands are rejected because safely rewriting one part
    would otherwise create an ambiguous execution boundary.
    """

    raw = str(command or "")
    normalized = raw.replace("\\", "/").casefold()
    if not re.search(r"analyze_relations\.py[\"']?\s+analyze(?:\s|$)", normalized):
        return raw, None
    selector = _anchor_selector_payload(record)
    if selector is None:
        # The first run may intentionally have no selector: it produces a
        # redacted selection-required surface.  It may not accept a model- or
        # CLI-authored selector/trace file, which would bypass that human
        # choice entirely.
        if (
            re.search(r"--trace-anchor-selector(?:\s|=|$)", normalized)
            or re.search(r"--trace-file(?:\s|=|$)", normalized)
        ):
            return raw, (
                "Blocked: no result anchor has been selected in Data and evidence. "
                "Run unselected tracing only to obtain the reviewable selection surface, then persist the user's choice."
            )
        return raw, None
    if any(marker in raw for marker in ("\n", "\r", ";", "|", "&", "<", ">", "`", "$")):
        return raw, (
            "Blocked: the persisted result anchor can be injected only into one direct tracing command. "
            "Remove shell chaining and retry from Data and evidence."
        )
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        return raw, "Blocked: tracing command has invalid quoting; the selected result anchor cannot be verified."
    selector_index = next(
        (index for index, token in enumerate(tokens) if token == "--trace-anchor-selector"),
        -1,
    )
    inline_selector = next(
        (token.split("=", 1)[1] for token in tokens if token.startswith("--trace-anchor-selector=")),
        "",
    )
    if inline_selector:
        try:
            supplied = json.loads(inline_selector)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw, "Blocked: --trace-anchor-selector must be valid JSON matching the user-selected anchor."
        if supplied != selector:
            return raw, (
                "Blocked: tracing must use the exact result anchor selected in Data and evidence. "
                "Do not substitute an AI-selected row."
            )
        return raw, None
    if selector_index >= 0:
        if selector_index + 1 >= len(tokens):
            return raw, "Blocked: --trace-anchor-selector is missing its JSON value."
        try:
            supplied = json.loads(tokens[selector_index + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw, "Blocked: --trace-anchor-selector must be valid JSON matching the user-selected anchor."
        if supplied != selector:
            return raw, (
                "Blocked: tracing must use the exact result anchor selected in Data and evidence. "
                "Do not substitute an AI-selected row."
            )
        return raw, None
    if any(token == "--trace-file" or token.startswith("--trace-file=") for token in tokens):
        return raw, (
            "Blocked: a persisted result anchor cannot be combined with --trace-file. "
            "Run direct result-anchored tracing instead."
        )
    encoded_selector = json.dumps(selector, ensure_ascii=False, separators=(",", ":"))
    if any(marker in encoded_selector for marker in _DIRECT_COMMAND_UNSAFE_SELECTOR_MARKERS):
        return raw, (
            "Blocked: the persisted result anchor contains shell-sensitive characters and cannot be "
            "injected into a direct tracing command. Rename the source or select an equivalent safe anchor."
        )
    return f"{raw} --trace-anchor-selector {_quote_direct_selector_argument(encoded_selector)}", None


def apply_approved_role_manifest(command: str) -> tuple[str, str | None]:
    """Inject the one signed role contract usable by sandboxed tracing.

    The command line must never select a different file/table-role manifest:
    that would turn a user-approved result role back into an AI inference.
    ``graph.py`` validates the host-side manifest before calling this helper;
    this function owns only safe direct-command rewriting.
    """

    raw = str(command or "")
    normalized = raw.replace("\\", "/").casefold()
    if not re.search(r"analyze_relations\.py[\"']?\s+analyze(?:\s|$)", normalized):
        return raw, None
    if any(marker in raw for marker in ("\n", "\r", ";", "|", "&", "<", ">", "`", "$")):
        return raw, (
            "Blocked: the approved role manifest can be injected only into one direct tracing command. "
            "Remove shell chaining and retry from Data and evidence."
        )
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        return raw, "Blocked: tracing command has invalid quoting; the approved role manifest cannot be verified."
    manifest_values: list[str] = []
    for index, token in enumerate(tokens):
        if token == "--role-manifest":
            if index + 1 >= len(tokens):
                return raw, "Blocked: --role-manifest is missing its approved manifest path."
            manifest_values.append(tokens[index + 1])
        elif token.startswith("--role-manifest="):
            manifest_values.append(token.split("=", 1)[1])
    if manifest_values:
        if any(value.replace("\\", "/") != _SANDBOX_ROLE_MANIFEST for value in manifest_values):
            return raw, (
                "Blocked: tracing must use the current platform-approved role manifest; "
                "do not substitute an AI-authored role assignment."
            )
        return raw, None
    return f"{raw} --role-manifest {_SANDBOX_ROLE_MANIFEST}", None


def distillation_runtime_context(record: BusinessRecord) -> str:
    """Return a bounded prompt insert explaining the current hard gate."""

    state = record.distillation
    approvals = [
        str(item.phase)
        for item in state.approvals
        if (
            item.status == "active"
            and item.decision == "approved"
            and item.source_revision == state.source_revision
        )
    ]
    approved = ", ".join(approvals) or "none"
    selection = _anchor_selector_payload(record)
    selector = ""
    if selection:
        selector = (
            f"\n- User-selected result anchor: {selection!r}. The platform appends it to a direct "
            "tracing command; do not supply a different selector or --trace-file."
        )
    correction = ""
    rejected = next(
        (
            item for item in reversed(state.approvals)
            if item.status == "active"
            and item.decision == "rejected"
            and item.phase == state.current_phase
            and item.source_revision == state.source_revision
        ),
        None,
    )
    if rejected is not None:
        note = _bounded_prompt_text(rejected.note)
        correction = (
            f"\n- Latest user correction for `{rejected.phase}`: {note or 'No note was recorded; ask a focused question before revising.'}"
            " Revise only this candidate; do not advance to a later phase."
        )
    return (
        "\n\n## Evidence-gated distillation status\n"
        f"- Current workbench phase: `{state.current_phase}`; revision: `{state.revision}`.\n"
        f"- Active reviewer approvals for this revision: {approved}.\n"
        "- You may create only the candidate allowed by the current upstream approval. Do not call "
        "CLI approval commands or claim an artifact is approved. If a reviewable gate blocks execution, "
        "the platform opens an approval dialog; explain the current artifact without asking the user to edit JSON."
        f"{selector}{correction}"
    )


__all__ = [
    "apply_approved_role_manifest",
    "apply_selected_trace_anchor",
    "distillation_command_blocker",
    "distillation_runtime_context",
]
