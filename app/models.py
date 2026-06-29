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
    """业务场景的生命周期状态，驱动前端的状态徽标与可用操作。

    生命周期对应 v1.0.1 的逆向工程工作流：
    上传 → 扫描元数据 → 推导关联(ER) → 解析规则库 → 校验(对照历史结果) → 固化为参数化技能。
    """

    CREATED = "created"                  # 已创建，尚未上传数据
    TABLES_UPLOADED = "tables_uploaded"  # 已上传业务表
    PROCESS_DRAFTED = "process_drafted"  # Phase 0：已生成业务流程文档，待用户审批
    PROCESS_APPROVED = "process_approved"  # Phase 0：业务流程文档已获用户批准（放行后续阶段）
    RELATIONS_DEDUCED = "relations_deduced"  # 已推导表关联（ER 模型）
    RULES_PARSED = "rules_parsed"        # 已从规则表解析出规则模板库
    FLOW_DEDUCED = "flow_deduced"        # 已推导业务流程（针对某规则的逻辑链）
    VALIDATED = "validated"              # 已用历史结果表校验通过某条规则
    SKILLS_GENERATED = "skills_generated"  # 已生成参数化审核技能
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
# 规则模板库（v1.0.1 核心：规则表 = 领域知识库）
# ===========================================================================
class RuleTemplate(BaseModel):
    """从规则表解析出的「一条可参数化的审核规则模板」。

    规则表是领域知识库（如医保违规审核），每一行/每一类违规对应一个模板。
    模板从「仅描述」逐步细化为「可执行 SQL」：
        parsed     → 仅有违规类型、关键词、逻辑描述（来自规则表，无 AI）
        unverified → 已具备可执行 SQL（DuckDB 方言），但尚无历史结果可校验
        verified   → 已用历史结果表校验通过（命中率达标）
        blocked    → 依赖外部数据（如标准价格表）尚未提供，已声明接口，运行时拒绝执行

    运行时执行（Engineer's Toolbox）一律走 `sql`（DuckDB），不调用 AI、不重新分析 schema。
    """

    rule_id: str
    seq: str = ""                       # 规则表中的序号/规则编号
    category: str = ""                  # 规则大类（如「政策通用类」/所属领域）
    violation_type: str = ""            # 违规类型（如「重复收费」「超标准收费」）——审核类型主键
    logic_description: str = ""         # 规则情形清单 / 违规逻辑描述
    policy_basis: str = ""              # 政策依据
    example: str = ""                   # 案例违规参考示例
    usage: str = ""                     # 用途（现场检查 / 大数据筛查 等）
    keywords: list[str] = Field(default_factory=list)

    # ---- 推导填充（逻辑口径）----
    required_tables: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    join_conditions: list[str] = Field(default_factory=list)
    filter_logic: str = ""
    aggregation: str = ""
    external_data_needed: list[str] = Field(default_factory=list)  # 缺失的外部数据接口

    # ---- 可执行（SQL / DuckDB）----
    sql: str = ""                       # 可执行 SQL 模板（DuckDB 方言；表名=注册视图名，标识符用双引号）
    output_columns: list[str] = Field(default_factory=list)  # 结果列（对齐历史结果表结构）
    strategy: str = ""                  # 生成该 SQL 所用策略（duplicate/over_standard/keyword/...）
    code: str = ""                      # 兼容旧版 pandas 代码（已弃用，仅历史数据回放用）
    status: str = "parsed"              # parsed/unverified/verified/blocked
    match_rate: Optional[float] = None  # 最近一次对照历史结果的命中率


class DomainColumn(BaseModel):
    name: str
    dtype: str = "unknown"


class DomainTable(BaseModel):
    """domain_knowledge.json 中的一张表：结构 + 角色 + 文件定位。"""

    table_name: str
    role: str = "input"                 # input / rule / result
    file: str = ""                      # 文件名（运行时按文件夹/同名定位新数据）
    row_count: int = 0
    header_row: int = 0
    columns: list[DomainColumn] = Field(default_factory=list)


class DomainKnowledge(BaseModel):
    """领域知识（Phase 1 产物）：数据字典 + ER 关系 + 结果表结构契约。

    这是 Skill 运行时的「字典」：execute_audit 据此定位/校验表与列，并保证输出列
    与历史结果表结构一致——运行时绝不重新分析 schema。
    """

    scenario: str = ""
    tables: list[DomainTable] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    result_schema: dict[str, list[str]] = Field(default_factory=dict)  # {违规类型|__default__: [列...]}
    field_semantics: dict[str, str] = Field(default_factory=dict)


class RuleLibrary(BaseModel):
    """规则模板库：规则表解析后的结构化知识库。"""

    source_table: str = ""              # 规则表的表名
    templates: list[RuleTemplate] = Field(default_factory=list)
    summary: str = ""

    @property
    def violation_types(self) -> list[str]:
        seen: list[str] = []
        for t in self.templates:
            if t.violation_type and t.violation_type not in seen:
                seen.append(t.violation_type)
        return seen


class ValidationReport(BaseModel):
    """执行某规则并与历史结果表对照后的「差异摘要」（AI 只看摘要，不看原始数据）。"""

    violation_type: str = ""
    rule_id: str = ""
    historical_table: str = ""
    produced_count: int = 0
    historical_count: int = 0
    matched: int = 0
    missing: int = 0           # 历史有、复刻无
    extra: int = 0             # 复刻有、历史无
    match_rate: float = 0.0    # matched / historical_count
    passed: bool = False
    key_columns: list[str] = Field(default_factory=list)
    sample_missing: list[dict[str, Any]] = Field(default_factory=list)
    sample_extra: list[dict[str, Any]] = Field(default_factory=list)
    executed_sql: str = ""     # 实际执行的 SQL（无论命中与否都记录，便于排查「0 结果」）
    error: str = ""            # 执行报错（若有）
    message: str = ""


# ===========================================================================
# Phase 0：业务流程发现（Business Process Discovery）
# ===========================================================================
class BusinessProcess(BaseModel):
    """Phase 0 产物：对业务场景「到底在做什么」的结构化记录。

    对应规范 Phase 0 的 `business_process.md`：业务问题的白话描述、处理步骤、
    输入表 / 规则（标准）表 / 结果表的识别，以及一张流程 Mermaid 图。
    完整 Markdown 另落盘为 business_process.md；本结构供前后端与 API 直接消费。
    **Gate**：必须经用户显式批准（approved=True）后，方可进入后续阶段。
    """

    description: str = ""                       # 业务问题的白话描述
    steps: list[str] = Field(default_factory=list)  # 处理步骤（数据进入→处理→规则→产出）
    input_tables: list[str] = Field(default_factory=list)   # 业务输入表
    rule_tables: list[str] = Field(default_factory=list)    # 规则/标准表（知识库）
    result_tables: list[str] = Field(default_factory=list)  # 历史结果表（样例）
    mermaid: str = ""                           # 流程图（Mermaid flowchart）
    markdown: str = ""                          # 完整 business_process.md 文本
    approved: bool = False                      # 是否已获用户批准（Gate）
    approved_at: Optional[float] = None         # 批准时间戳
    feedback: str = ""                          # 用户在审批时给出的修改意见（若有）


class Interaction(BaseModel):
    """结构化交互块（规范 Section 5）。

    当 AI 需要用户输入（Phase 0 审批、关联确认、规则歧义澄清等）时，
    不把问题混在正文里，而是发出本结构，由前端渲染为表单，用户以结构化数据回应。
    """

    type: str = "confirm"                       # choice / confirm / input
    question: str = ""
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = True
    context: str = ""                           # 关联的业务语境（如 phase0_approval）


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
    business_process: Optional[BusinessProcess] = None  # Phase 0 产物（含审批 Gate）
    relations: Optional[RelationResult] = None
    domain_knowledge: Optional[DomainKnowledge] = None  # Phase 1 产物：数据字典 + 结果契约
    rule_library: Optional[RuleLibrary] = None
    flow: Optional[FlowResult] = None
    validations: list[ValidationReport] = Field(default_factory=list)
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


class ProcessApprovalRequest(BaseModel):
    """Phase 0 审批：通过 / 打回（附修改意见）。"""

    approved: bool = True
    feedback: str = ""


class EvolveSkillRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1)


class ExecuteAuditRequest(BaseModel):
    """执行一次审核：指定违规类型；data_sources 缺省时复用场景已上传的数据表。"""

    violation_type: str = Field(..., min_length=1)
    data_sources: dict[str, str] = Field(default_factory=dict)  # {表名: 新数据文件路径}
