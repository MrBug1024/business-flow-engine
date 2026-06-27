"""领域模型与 API 数据契约（Pydantic）。

集中定义业务场景、表结构、关联关系、业务流程、技能等核心数据结构，
保证前后端、存储层、Agent 工具之间的数据形态一致。
"""

from __future__ import annotations

from enum import Enum
from time import time
from typing import Any, Optional

from pydantic import BaseModel, Field


# ===========================================================================
# 业务场景状态机
# ===========================================================================
class ScenarioStatus(str, Enum):
    """业务场景的生命周期状态，驱动前端的状态徽标与可用操作。"""

    CREATED = "created"                  # 已创建，尚未上传数据
    TABLES_UPLOADED = "tables_uploaded"  # 已上传业务表
    RELATIONS_DEDUCED = "relations_deduced"  # 已推导表关联
    FLOW_DEDUCED = "flow_deduced"        # 已推导业务流程
    SKILLS_GENERATED = "skills_generated"  # 已生成技能库
    ACTIVE = "active"                    # 技能库可对外提供业务能力


# ===========================================================================
# 表结构
# ===========================================================================
class ColumnMeta(BaseModel):
    """单个字段的元信息（仅基于表头与少量样本推断，绝不遍历全量数据）。"""

    name: str
    dtype: str = "unknown"
    null_rate: float = 0.0
    sample_values: list[Any] = Field(default_factory=list)


class TableMeta(BaseModel):
    """单张业务表的结构元信息。"""

    table_name: str
    display_name: str
    file_path: str
    row_count: int = 0
    col_count: int = 0
    header_row: int = 0  # 自动识别出的表头所在行号（0 表示首行即表头；>0 表示上方存在标题等无关行）
    columns: list[ColumnMeta] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


# ===========================================================================
# 关联关系
# ===========================================================================
class Relation(BaseModel):
    """两张表之间的一条关联关系。"""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    relation_type: str = "foreign_key"  # foreign_key / possible_link / rule_mapping
    confidence: float = 0.0
    evidence: str = ""


class GraphNode(BaseModel):
    id: str
    label: str
    type: str = "table"  # table / result / rule / input / process / output


class GraphEdge(BaseModel):
    source: str
    target: str
    label: str = ""


class GraphData(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class RelationResult(BaseModel):
    """关联推导结果。"""

    relations: list[Relation] = Field(default_factory=list)
    ambiguous_questions: list[str] = Field(default_factory=list)
    graph_data: GraphData = Field(default_factory=GraphData)
    summary: str = ""


# ===========================================================================
# 业务流程
# ===========================================================================
class FlowStep(BaseModel):
    """业务流程中的一个处理步骤。"""

    step_id: int
    step_name: str
    operation: str  # FILTER / JOIN / AGGREGATE / CALCULATE / MAP ...
    input_tables: list[str] = Field(default_factory=list)
    output: str = ""
    logic: str = ""
    description: str = ""
    pseudo_sql: str = ""


class FlowResult(BaseModel):
    """业务流程推导结果。"""

    flow_steps: list[FlowStep] = Field(default_factory=list)
    flow_graph: GraphData = Field(default_factory=GraphData)
    ambiguous_questions: list[str] = Field(default_factory=list)
    summary: str = ""


# ===========================================================================
# 技能（Skill）
# ===========================================================================
class Skill(BaseModel):
    """一个可复用的业务能力单元（落盘为 SKILL.md + scripts/）。"""

    skill_id: str
    name: str
    operation: str = ""
    description: str = ""
    step_id: Optional[int] = None
    is_main: bool = False
    is_evolved: bool = False
    status: str = "generated"  # generated / evolved
    path: str = ""


# ===========================================================================
# 业务场景聚合根
# ===========================================================================
class Scenario(BaseModel):
    """业务场景：承载一次完整的「数据 → 关联 → 流程 → 技能」逆向工程过程。"""

    id: str
    name: str
    description: str = ""
    status: ScenarioStatus = ScenarioStatus.CREATED
    created_at: float = Field(default_factory=time)
    updated_at: float = Field(default_factory=time)
    tables_meta: list[TableMeta] = Field(default_factory=list)
    relations: Optional[RelationResult] = None
    flow: Optional[FlowResult] = None
    skills: list[Skill] = Field(default_factory=list)


# ===========================================================================
# 对话消息
# ===========================================================================
class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ToolTrace(BaseModel):
    """一次工具/技能调用的轨迹（用于前端可视化「AI 正在做什么」）。"""

    name: str
    args_summary: str = ""
    result_summary: str = ""


class ChatMessage(BaseModel):
    """持久化的一条对话消息。"""

    id: str
    role: ChatRole
    content: str = ""
    thinking: str = ""
    tools: list[ToolTrace] = Field(default_factory=list)
    created_at: float = Field(default_factory=time)


# ===========================================================================
# API 请求体
# ===========================================================================
class CreateScenarioRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = ""


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)


class EvolveSkillRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1)
