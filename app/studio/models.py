"""Pydantic models for AI Business Studio.

The central object is BusinessContext. Everything else in the Studio either
updates it, renders it, or generates portable artifacts from it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Status = Literal["created", "files_uploaded", "analyzed", "confirmed", "outputs_generated"]

# These phases are deliberately platform-owned.  Skills may produce candidate
# artifacts for a phase, but they must not be able to move the durable state or
# manufacture a user approval.
DistillationPhase = Literal[
    "file_roles",
    "data_lineage",
    "relations",
    "micro_process",
    "business_flow",
    "capability",
    "package",
]
DISTILLATION_PHASES: tuple[DistillationPhase, ...] = (
    "file_roles",
    "data_lineage",
    "relations",
    "micro_process",
    "business_flow",
    "capability",
    "package",
)
DistillationStageStatus = Literal[
    "pending",
    "ready_for_review",
    "approved",
    "rejected",
    "invalidated",
]
TableRole = Literal["input", "result", "rule", "reference", "template", "ignore"]
ApprovalDecision = Literal["approved", "rejected"]
ApprovalStatus = Literal["active", "superseded", "invalidated"]


class WorkspaceNode(BaseModel):
    name: str
    path: str
    kind: Literal["file", "folder"]
    icon: str = ""
    size: int = 0
    children: list["WorkspaceNode"] = Field(default_factory=list)


class CreateBusinessRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=4000)


class UpdateBusinessRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    goal: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=4000)
    status: Status | None = None


class DescriptionMarkdownRequest(BaseModel):
    content: str = Field(max_length=80000)


class WorkspaceCreateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    kind: Literal["file", "folder"]
    content: str = Field(default="", max_length=2_000_000)


class WorkspaceMoveRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    destination: str = Field(min_length=1, max_length=1000)


class AIModelConfig(BaseModel):
    id: str
    name: str
    provider: str = "openai-compatible"
    model: str
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True
    default: bool = False


class StudioSettings(BaseModel):
    active_model: str
    configured_models: list[AIModelConfig] = Field(default_factory=list)
    installed_tools: list[str] = Field(default_factory=list)
    installed_skills: list[str] = Field(default_factory=list)
    mcp_configs: list[dict[str, Any]] = Field(default_factory=list)


class UpdateStudioSettings(BaseModel):
    active_model: str | None = None
    configured_models: list[AIModelConfig] | None = None
    installed_tools: list[str] | None = None
    installed_skills: list[str] | None = None
    mcp_configs: list[dict[str, Any]] | None = None


class MCPServersRequest(BaseModel):
    config: dict[str, Any]


class UpdateMCPServerRequest(BaseModel):
    enabled: bool


class InstallSkillFromUrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class BusinessFile(BaseModel):
    id: str
    business_id: str
    filename: str
    suffix: str
    size: int
    mime_type: str = ""
    storage_path: str
    workspace_path: str = ""
    uploaded_at: float
    parse_status: Literal["pending", "parsed", "parsed_with_warnings", "failed"] = "pending"
    parser: str = ""
    summary: str = ""
    text: str = ""
    columns: list[str] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    sheets: list[dict[str, Any]] = Field(default_factory=list)
    structured: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    # The catalog is a persisted, bounded view of the source file rather than
    # an inference artifact.  ``source_digest`` binds the cached parser facts
    # to exact bytes, while the stat fingerprint lets the read path avoid
    # hashing/re-parsing unchanged large files.
    source_digest: str = ""
    source_stat: dict[str, int] = Field(default_factory=dict)


class DistillationStage(BaseModel):
    """Durable status for one human-controlled distillation checkpoint."""

    phase: DistillationPhase
    status: DistillationStageStatus = "pending"
    revision: int = 0
    updated_at: float | None = None
    invalidation_reason: str = ""


def _default_distillation_stages() -> list[DistillationStage]:
    return [DistillationStage(phase=phase) for phase in DISTILLATION_PHASES]


class TableRoleConfirmation(BaseModel):
    """A user-confirmed semantic role for one uploaded file/table scope.

    ``table_name`` is also used for sheet names.  ``__file__`` is a supported
    file-level scope for formats that do not expose tables.
    """

    id: str
    file_id: str
    table_name: str = Field(min_length=1, max_length=240)
    role: TableRole
    note: str = Field(default="", max_length=4000)
    confirmed_by: str
    confirmed_at: float
    source_revision: int
    revision: int
    status: Literal["confirmed", "superseded"] = "confirmed"
    superseded_at: float | None = None


class TraceAnchorSelector(BaseModel):
    """One user-selected historical result row used to build a trace."""

    file: str = Field(min_length=1, max_length=500)
    table: str = Field(min_length=1, max_length=240)
    row_number: int = Field(ge=1)
    selected_by: str
    selected_at: float
    source_revision: int
    revision: int


class DistillationApproval(BaseModel):
    """An identity-bound approval of one immutable candidate artifact."""

    id: str
    phase: DistillationPhase
    decision: ApprovalDecision
    artifact_id: str
    artifact_fingerprint: str
    note: str = Field(default="", max_length=4000)
    actor_id: str
    revision: int
    source_revision: int = 0
    created_at: float
    status: ApprovalStatus = "active"
    superseded_at: float | None = None
    invalidated_at: float | None = None
    invalidation_reason: str = ""
    review_artifact_path: str = ""
    review_artifact_fingerprint: str = ""
    platform_receipt_path: str = ""
    platform_receipt_fingerprint: str = ""


class DistillationState(BaseModel):
    """Revisioned source-of-truth for the staged distillation workbench."""

    schema_version: int = 1
    revision: int = 0
    source_revision: int = 0
    current_phase: DistillationPhase = "file_roles"
    stages: list[DistillationStage] = Field(default_factory=_default_distillation_stages)
    table_roles: list[TableRoleConfirmation] = Field(default_factory=list)
    anchor_selector: TraceAnchorSelector | None = None
    approvals: list[DistillationApproval] = Field(default_factory=list)
    artifact_contracts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    role_manifest_fingerprint: str = ""
    last_invalidated_at: float | None = None
    last_invalidation_reason: str = ""


class ContextVersion(BaseModel):
    version: int
    summary: str
    trigger: str
    created_at: float
    actor: str = "system"
    model: str = "local-context-builder"
    evidence_ids: list[str] = Field(default_factory=list)
    snapshot: dict[str, Any] = Field(default_factory=dict)


class BusinessContext(BaseModel):
    business_id: str
    name: str
    goal: str = ""
    user_requirements: list[dict[str, Any]] = Field(default_factory=list)
    source_files: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    flows: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    terminology: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    data_lineage: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)
    confirmations: list[dict[str, Any]] = Field(default_factory=list)
    tool_usages: list[dict[str, Any]] = Field(default_factory=list)
    skill_references: list[dict[str, Any]] = Field(default_factory=list)
    mcp_references: list[dict[str, Any]] = Field(default_factory=list)
    versions: list[ContextVersion] = Field(default_factory=list)


class ChatSession(BaseModel):
    id: str
    business_id: str
    title: str = ""
    created_at: float
    updated_at: float


class ChatMessage(BaseModel):
    id: str
    session_id: str = ""
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: float
    run_id: str | None = None
    task_id: str = ""
    kind: Literal["standard", "progress", "final", "error"] = "standard"
    progress_action: str = ""
    work_item_id: str = ""
    progress: dict[str, Any] = Field(default_factory=dict)
    activity_events: list[dict[str, Any]] = Field(default_factory=list)


class AIRun(BaseModel):
    id: str
    business_id: str
    session_id: str | None = None
    task_id: str = ""
    segment_index: int = 1
    continued_from_run_id: str | None = None
    resumed_from_run_id: str | None = None
    status: Literal["running", "waiting_for_user", "succeeded", "failed"] = "running"
    model: str = "local-context-builder"
    plan: list[str] = Field(default_factory=list)
    task_progress: dict[str, Any] = Field(default_factory=dict)
    tool_invocations: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    started_at: float
    finished_at: float | None = None
    summary: str = ""
    error: str = ""


class PackageRecord(BaseModel):
    id: str
    business_id: str
    version: int
    filename: str
    storage_path: str
    created_at: float
    download_url: str


class BusinessRecord(BaseModel):
    id: str
    owner_id: str = ""
    name: str
    goal: str = ""
    description: str = ""
    status: Status = "created"
    created_at: float
    updated_at: float
    current_version: int = 0
    files: list[BusinessFile] = Field(default_factory=list)
    context: BusinessContext
    chat_sessions: list[ChatSession] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    runs: list[AIRun] = Field(default_factory=list)
    packages: list[PackageRecord] = Field(default_factory=list)
    workspace_deleted_paths: list[str] = Field(default_factory=list)
    distillation: DistillationState = Field(default_factory=DistillationState)


class BusinessSummary(BaseModel):
    id: str
    name: str
    goal: str = ""
    description: str = ""
    status: Status
    created_at: float
    updated_at: float
    current_version: int
    file_count: int
    open_question_count: int
    package_count: int


class SkillDefinition(BaseModel):
    name: str
    description: str
    kind: Literal["system", "user", "third_party"] = "system"
    version: str = "1.0.0"
    locked: bool = True
    enabled: bool = True
    dependencies: list[str] = Field(default_factory=list)
    compatibility: str = ""
    digest: str = ""
    location: str = ""
    resources: list[str] = Field(default_factory=list)
    capability_id: str = ""
    responsibility: str = ""
    excludes: list[str] = Field(default_factory=list)
    completion: dict[str, Any] = Field(default_factory=dict)
    contract_status: Literal["declared", "undeclared"] = "undeclared"




class CreateChatSessionRequest(BaseModel):
    title: str = Field(default="", max_length=120)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    model: str | None = None
    session_id: str | None = Field(default=None, max_length=80)


class ResumeChatRequest(BaseModel):
    model: str | None = None
    run_id: str | None = Field(default=None, max_length=80)


class ConfirmationRequest(BaseModel):
    question_id: str | None = None
    session_id: str | None = Field(default=None, max_length=80)
    option_id: str | None = Field(default=None, max_length=160)
    answer: str = Field(min_length=1, max_length=4000)
    accepted: bool = True


class TableRoleRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=120)
    table_name: str = Field(min_length=1, max_length=240)
    role: TableRole
    note: str = Field(default="", max_length=4000)
    expected_revision: int | None = Field(default=None, ge=0)


class TraceAnchorSelectorRequest(BaseModel):
    file: str = Field(min_length=1, max_length=500)
    table: str = Field(min_length=1, max_length=240)
    row_number: int = Field(ge=1)
    expected_revision: int | None = Field(default=None, ge=0)


class DistillationTraceRequest(BaseModel):
    """Request a server-owned deterministic data-lineage trace.

    The client deliberately cannot submit a command, manifest path, or anchor
    here.  The platform derives all three from the approved scenario state.
    """

    expected_revision: int | None = Field(default=None, ge=0)
    session_id: str | None = Field(default=None, max_length=80)
    # Set only by the server-side chat intent route. It is audit text, never
    # an execution argument: the trace command, manifest, anchor policy, and
    # correction review remain server owned.
    chat_message: str | None = Field(default=None, max_length=8000)


class TraceReviewKeyPair(BaseModel):
    """One user-confirmed field pair for a deterministic retrace."""

    source_field: str = Field(min_length=1, max_length=240)
    target_field: str = Field(min_length=1, max_length=240)


class TraceReviewKeyPairCorrection(BaseModel):
    """A user-confirmed table relationship correction, without row values."""

    source_file: str = Field(min_length=1, max_length=500)
    source_table: str = Field(min_length=1, max_length=240)
    target_file: str = Field(min_length=1, max_length=500)
    target_table: str = Field(min_length=1, max_length=240)
    key_pairs: list[TraceReviewKeyPair] = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=4000)


class TraceReviewCorrectionRequest(BaseModel):
    """Apply reviewer-owned key-pair corrections before a deterministic retrace."""

    corrections: list[TraceReviewKeyPairCorrection] = Field(min_length=1, max_length=32)
    replace_existing: bool = False
    note: str = Field(default="", max_length=4000)
    expected_revision: int | None = Field(default=None, ge=0)


class DistillationApprovalRequest(BaseModel):
    phase: DistillationPhase
    decision: ApprovalDecision
    artifact_id: str = Field(min_length=1, max_length=500)
    artifact_fingerprint: str = Field(min_length=1, max_length=256)
    note: str = Field(default="", max_length=4000)
    expected_revision: int | None = Field(default=None, ge=0)
