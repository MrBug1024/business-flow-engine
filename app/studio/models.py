"""Pydantic models for AI Business Studio.

The central object is BusinessContext. Everything else in the Studio either
updates it, renders it, or generates portable artifacts from it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Status = Literal["created", "files_uploaded", "analyzed", "confirmed", "outputs_generated"]


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


ScenarioMarkdownRequest = DescriptionMarkdownRequest


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
    answer: str = Field(min_length=1, max_length=4000)
    accepted: bool = True
