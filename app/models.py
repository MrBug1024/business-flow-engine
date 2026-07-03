"""领域模型与 API 数据契约（Pydantic）。

v1.0.3 通用化重构：
* 工作流从 7 步压缩为 **5 步**：上传(含标角色) → 推关联(含字段语义) → 推流程(含节点能力)
  → 生成技能 → 执行（含校验）。状态机 5 态，无审批 Gate。
* 字段语义成为画像核心：`ColumnMeta` 新增 `semantic` / `semantic_role`，作为关联推导与
  模板算子参数选择的依据。
* 规则范式重构：不再逐条解析规则为关键词字典；改为蒸馏 `RuleSchemaMapping`——只学
  「规则表的列角色 + 规则字段→业务字段映射 + 分派值→模板算子」，规则在运行时按行迭代，
  几万条也撑得住。
* 流程节点丰富化：`FlowStep` 新增 `purpose / capability / data_in / data_out / template_kind`，
  让节点说清「该做什么、能做什么、数据怎么变化」，不再是文件名连线。
* 框架场景无关：表名 / 字段 / 规则 / 输出格式只作为「数据」存在于知识包，绝不写进逻辑。
"""

from __future__ import annotations

from enum import Enum
from time import time
from typing import Any, Optional

from pydantic import BaseModel, Field


# ===========================================================================
# 业务场景状态机（5 步骤）
# ===========================================================================
class ScenarioStatus(str, Enum):
    """业务场景的生命周期状态。5 步通用蒸馏工作流：

    上传(含标角色) → 推关联(含字段语义) → 推流程(含节点能力) → 生成技能 → 执行（含校验）
    """

    CREATED = "created"                    # 已创建，尚未上传数据
    TABLES_UPLOADED = "tables_uploaded"    # 已上传业务表（用户在上传时即已标角色）
    RELATIONS_DEDUCED = "relations_deduced"  # 已推导表关联 + 字段语义
    FLOW_DEDUCED = "flow_deduced"          # 已推导业务流程（每节点带能力描述）
    SKILLS_GENERATED = "skills_generated"  # 已固化为可复用技能
    ACTIVE = "active"                      # 已执行至少一次产出（含校验）


class TableRole(str, Enum):
    """表在业务中的角色。上传时由用户选择，是后续所有推导的起点。"""
    INPUT = "input"          # 业务输入表（流程原始数据）
    KNOWLEDGE = "knowledge"  # 知识表（规则/标准/目录/公式/分类……领域知识库）v1.0.4
    RULE = "rule"            # 知识表的旧称（向后兼容，等同 knowledge）
    RESULT = "result"        # 历史结果表（产出样例，用于学习与对照）
    UNKNOWN = "unknown"      # 尚未标注（一般不该出现）


class SemanticRole(str, Enum):
    """字段在业务中的语义角色（通用，场景无关）。"""
    PK = "PK"              # 主键 / 业务实体标识
    FK = "FK"              # 外键 / 关联键
    DIM = "DIM"            # 维度（分类、可分组）
    METRIC = "METRIC"      # 度量（数值，可聚合）
    TIME = "TIME"          # 时间
    NL_TEXT = "NL_TEXT"    # 自然语言长文本（描述、说明）
    CATEGORY = "CATEGORY"  # 离散类别（分派候选，如分类标签、等级）
    UNKNOWN = "UNKNOWN"


# ===========================================================================
# 表结构 + 字段语义
# ===========================================================================
class ColumnMeta(BaseModel):
    """字段元信息（含业务语义，仅基于表头与少量样本推断，绝不遍历全量数据）。"""

    name: str
    dtype: str = "unknown"
    null_rate: float = 0.0
    sample_values: list[Any] = Field(default_factory=list)
    # ---- 业务语义（蒸馏阶段一次性推断，运行时仅查询） ----
    semantic: str = ""                                # 业务含义（如「就诊编号」「金额」「项目名称」）
    semantic_role: str = SemanticRole.UNKNOWN.value   # PK / FK / DIM / METRIC / TIME / NL_TEXT / CATEGORY


class TableMeta(BaseModel):
    """单张业务表的结构元信息（含角色，上传时由用户选择）。"""

    table_name: str
    display_name: str
    file_path: str
    role: str = TableRole.UNKNOWN.value
    role_confirmed: bool = False
    row_count: int = 0
    col_count: int = 0
    header_row: int = 0
    columns: list[ColumnMeta] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


# ===========================================================================
# 关联关系
# ===========================================================================
class Relation(BaseModel):
    from_table: str
    from_column: str  # 复合键时 = from_columns[0]，兼容只认单字段的旧代码
    to_table: str
    to_column: str    # 复合键时 = to_columns[0]
    relation_type: str = "foreign_key"  # foreign_key / possible_link / rule_mapping
    confidence: float = 0.0
    evidence: str = ""
    # 复合关联键：单字段不足以唯一确定对应关系时（如需要 结算ID+项目编码 才能定位一行），
    # 这里存完整字段列表；长度为 1 时等价于普通单字段关联。
    from_columns: list[str] = Field(default_factory=list)
    to_columns: list[str] = Field(default_factory=list)
    # 人工确认：一旦为 True，deduce_relations 重新推导时必须原样保留，不能被 AI 覆盖/丢弃。
    confirmed: bool = False

    def model_post_init(self, __context: Any) -> None:  # noqa: D401
        if not self.from_columns:
            self.from_columns = [self.from_column]
        if not self.to_columns:
            self.to_columns = [self.to_column]


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
    """关联推导 + 字段语义推导的合并结果（合并为一个步骤）。"""

    relations: list[Relation] = Field(default_factory=list)
    ambiguous_questions: list[str] = Field(default_factory=list)
    graph_data: GraphData = Field(default_factory=GraphData)
    summary: str = ""
    # 字段语义在 ColumnMeta 上原地更新；此处冗余收集一份便于前端快速展示
    field_semantics: dict[str, dict[str, str]] = Field(default_factory=dict)
    # 结构：{表名: {字段名: "语义描述 · 语义角色"}}
    # 追踪采样得到的因果链（结果行→业务行→知识行），推关联时算过一次就存下来，
    # 推流程时直接复用，不重新搜一遍大表；人工修正关联后会被强制刷新。
    # 结构与 trace_sampling.trace_sampling() 的返回值一致。
    trace_chain: dict[str, Any] = Field(default_factory=dict)


# ===========================================================================
# 知识表结构映射（v1.0.4 通用化：取代 v1.0.3 的 RuleSchemaMapping）
# ===========================================================================
class KnowledgeSchemaMapping(BaseModel):
    """知识表的结构映射（蒸馏一次，运行时按行迭代知识条目）。

    v1.0.4 通用化：
      - knowledge_table（原 rule_table）：不假设是"规则"表，也可以是标准/目录/公式表
      - dispatch_key_column（原 discriminator_column）：不假设是"违规类型"，任何分类维度皆可
      - dispatch_map（原 discriminator_to_template）：{分派值: 简短说明}，仅供人读，不驱动执行
      - condition_columns（原 nl_description_columns）：自然语言条件列
      - field_role_map（原 business_field_map）：{语义角色标签: 业务表名.列名}

    v1.0.7 铁律：不再为每个分派值预先派生 + 固化一条专属 SQL。真实业务场景里知识表
    可能有成百上千条规则，每条的判断逻辑千差万别，蒸馏阶段逐条"解题"既跑不完，也不可能
    穷尽用户在真实使用时提出的各种要求。这里只固化**结构**（哪列是分派键、哪列是自然
    语言条件、字段语义怎么对应业务表），不固化"每条规则该怎么判断"这件事本身——
    那件事交给运行时会思考的 LLM，读到知识表原文 + 真实业务表 schema 后现场构造查询。

    思想：值千变万化，结构永恒。
      ① 知识表里哪一列承担「分派键」（决定走哪套判断逻辑）
      ② 哪些列是自然语言条件 / 哪些列是直接参数
      ③ 知识字段语义角色 → 业务表字段的对应关系
    运行时：LLM 读知识表原文 + field_role_map，自行推理判断逻辑并现场查询业务表。
    """

    knowledge_table: str = ""                              # 知识表名
    dispatch_key_column: str = ""                          # 分派键列名（决定走哪套判断逻辑）
    item_id_column: str = ""                               # 条目编号/序号列
    condition_columns: list[str] = Field(default_factory=list)   # 自然语言条件列
    parameter_columns: list[str] = Field(default_factory=list)   # 直接参数列（阈值/系数等）
    dispatch_map: dict[str, str] = Field(default_factory=dict)
    # ↑ {分派值: 一句话说明}，人类可读，仅供 LLM 理解知识表全貌，不驱动执行。
    field_role_map: dict[str, str] = Field(default_factory=dict)
    # ↑ {语义角色标签: 业务表名.列名}，如 {"item_name": "明细表.项目名称"}
    summary: str = ""


# 向后兼容：RuleSchemaMapping 作为 KnowledgeSchemaMapping 的别名（带旧字段名的 shim）
class RuleSchemaMapping(BaseModel):
    """向后兼容 v1.0.3 的规则结构映射（新代码请使用 KnowledgeSchemaMapping）。"""

    rule_table: str = ""
    discriminator_column: str = ""
    rule_id_column: str = ""
    nl_description_columns: list[str] = Field(default_factory=list)
    parameter_columns: list[str] = Field(default_factory=list)
    discriminator_to_template: dict[str, str] = Field(default_factory=dict)
    business_field_map: dict[str, str] = Field(default_factory=dict)
    summary: str = ""

    def to_knowledge_schema(self) -> KnowledgeSchemaMapping:
        """转换为 KnowledgeSchemaMapping（v1.0.4 通用结构）。"""
        return KnowledgeSchemaMapping(
            knowledge_table=self.rule_table,
            dispatch_key_column=self.discriminator_column,
            item_id_column=self.rule_id_column,
            condition_columns=self.nl_description_columns,
            parameter_columns=self.parameter_columns,
            dispatch_map=self.discriminator_to_template,
            field_role_map=self.business_field_map,
            summary=self.summary,
        )


# ===========================================================================
# 业务流程：节点带「能力描述」
# ===========================================================================
class FlowStep(BaseModel):
    """业务流程中的一个**可执行节点 + 可读能力描述**。

    一个节点 = 一项业务能力 = 一个 skill。节点说清楚：
      * purpose      —— 该做什么（业务目标，给人看的）
      * capability   —— 能做什么 / 输出什么样的数据（给人看的）
      * data_in      —— 输入数据的"画像式"说明（哪些表/字段）
      * data_out     —— 输出数据的"画像式"说明
      * template_kind—— 走哪种执行方式（结构性算子 aggregate/join/... 或 knowledge_driven_join）
      * sql / params —— 可执行的转换逻辑（DuckDB）
    节点串联成管线，上一节点的 `output` 作为下一节点的输入。
    """

    step_id: int
    step_name: str
    operation: str  # FILTER / JOIN / RULE_DRIVEN / AGGREGATE / CALCULATE / FORMAT ...

    # ---- 可读的能力描述（前端展示，让节点不再是空壳）----
    purpose: str = ""                                  # 该做什么
    capability: str = ""                               # 能做什么 / 输出什么
    data_in: list[str] = Field(default_factory=list)   # 输入数据画像（可读串）
    data_out: list[str] = Field(default_factory=list)  # 输出数据画像（可读串）
    template_kind: str = ""                            # 使用的模板算子名

    # ---- 执行相关 ----
    input_tables: list[str] = Field(default_factory=list)
    output: str = ""                                   # 本节点输出视图名（供下游引用）
    output_columns: list[str] = Field(default_factory=list)
    logic: str = ""
    description: str = ""
    pseudo_sql: str = ""
    strategy: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    sql: str = ""
    status: str = "draft"                              # draft / executable / blocked
    external_data_needed: list[str] = Field(default_factory=list)
    row_count: Optional[int] = None


class FlowResult(BaseModel):
    flow_steps: list[FlowStep] = Field(default_factory=list)
    flow_graph: GraphData = Field(default_factory=GraphData)
    mermaid: str = ""                                  # 业务流程图（Mermaid，由节点生成）
    ambiguous_questions: list[str] = Field(default_factory=list)
    knowledge_schema: Optional[KnowledgeSchemaMapping] = None  # v1.0.4 通用知识表结构映射
    rule_schema: Optional[RuleSchemaMapping] = None            # v1.0.3 向后兼容
    summary: str = ""


# ===========================================================================
# 技能（Skill）：每个流程节点 → 一个技能
# ===========================================================================
class Skill(BaseModel):
    skill_id: str
    name: str
    operation: str = ""
    description: str = ""
    step_id: Optional[int] = None         # 关联的流程节点
    is_main: bool = False                 # 主技能（统一调度器）
    is_evolved: bool = False
    status: str = "generated"
    path: str = ""
    capability: str = ""                  # 节点级能力描述（来自 FlowStep.capability）


# ===========================================================================
# 产出规格（保留：作为「最终产出」的概念入口）
# ===========================================================================
class OutputSpec(BaseModel):
    """一种「这个业务会产出的结果」。

    一份历史结果文件蒸馏一个产出：学到它的结构(列契约) + 输出格式 + 由 flow_steps
    组成的可执行管线。运行时执行 pipeline（DuckDB），按 fmt 复刻为文件。
    """

    output_id: str
    name: str
    description: str = ""
    fmt: str = "csv"
    result_table: str = ""
    columns: list[str] = Field(default_factory=list)
    required_tables: list[str] = Field(default_factory=list)
    # ---- 产出 = 一段流程节点管线 ----
    pipeline: list[FlowStep] = Field(default_factory=list)
    # ---- 兼容：单步 SQL（pipeline 为空时回退执行）----
    strategy: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    sql: str = ""
    external_data_needed: list[str] = Field(default_factory=list)
    status: str = "draft"  # draft / executable / verified / blocked
    match_rate: Optional[float] = None


# ===========================================================================
# 领域知识（数据字典）—— 给执行技能使用
# ===========================================================================
class DomainColumn(BaseModel):
    name: str
    dtype: str = "unknown"
    semantic: str = ""
    semantic_role: str = SemanticRole.UNKNOWN.value


class DomainTable(BaseModel):
    table_name: str
    role: str = "input"
    file: str = ""
    row_count: int = 0
    header_row: int = 0
    columns: list[DomainColumn] = Field(default_factory=list)


class DomainKnowledge(BaseModel):
    scenario: str = ""
    tables: list[DomainTable] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    result_schema: dict[str, list[str]] = Field(default_factory=dict)
    field_semantics: dict[str, str] = Field(default_factory=dict)
    knowledge_schema: Optional[KnowledgeSchemaMapping] = None  # v1.0.4 通用知识表结构映射
    rule_schema: Optional[RuleSchemaMapping] = None            # v1.0.3 向后兼容


# ===========================================================================
# 校验报告
# ===========================================================================
class ValidationReport(BaseModel):
    output_id: str = ""
    output_name: str = ""
    historical_table: str = ""
    produced_count: int = 0
    historical_count: int = 0
    matched: int = 0
    missing: int = 0
    extra: int = 0
    match_rate: float = 0.0
    passed: bool = False
    key_columns: list[str] = Field(default_factory=list)
    sample_missing: list[dict[str, Any]] = Field(default_factory=list)
    sample_extra: list[dict[str, Any]] = Field(default_factory=list)
    node_counts: list[dict[str, Any]] = Field(default_factory=list)
    executed_sql: str = ""
    artifact_path: str = ""
    artifact_url: str = ""
    error: str = ""
    message: str = ""


# ===========================================================================
# 结构化交互（前端可渲染为表单）—— 所有 AI 疑问都走这里
# ===========================================================================
class Interaction(BaseModel):
    """AI 向用户提问时，发出本结构由前端渲染为表单（不混在正文里）。"""

    type: str = "confirm"                       # choice / confirm / input
    question: str = ""
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = True
    context: str = ""                           # 关联的业务语境（如 relation_confirm / field_semantic）


# ===========================================================================
# 业务场景聚合根
# ===========================================================================
class Scenario(BaseModel):
    id: str
    name: str
    description: str = ""
    status: ScenarioStatus = ScenarioStatus.CREATED
    created_at: float = Field(default_factory=time)
    updated_at: float = Field(default_factory=time)
    tables_meta: list[TableMeta] = Field(default_factory=list)
    relations: Optional[RelationResult] = None
    flow: Optional[FlowResult] = None
    domain_knowledge: Optional[DomainKnowledge] = None
    outputs: list[OutputSpec] = Field(default_factory=list)
    validations: list[ValidationReport] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)


# ===========================================================================
# 对话消息
# ===========================================================================
class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ToolTrace(BaseModel):
    name: str
    args_summary: str = ""
    result_summary: str = ""


class ChatMessage(BaseModel):
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


class TableRoleRequest(BaseModel):
    role: str = Field(..., pattern="^(input|rule|result|unknown)$")


class EvolveSkillRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1)


class ProduceRequest(BaseModel):
    output_id: str = Field(..., min_length=1)
    data_sources: dict[str, str] = Field(default_factory=dict)
    rule_filter: Any = None      # None=全量 / 关键词字符串 / {"列名":"列值"} 精确过滤


class RelationConfirmRequest(BaseModel):
    """人工确认/新增一条关联关系。留空 from_columns/to_columns 时按单字段处理。"""
    from_table: str = Field(..., min_length=1)
    from_column: str = Field(..., min_length=1)
    to_table: str = Field(..., min_length=1)
    to_column: str = Field(..., min_length=1)
    from_columns: list[str] = Field(default_factory=list)
    to_columns: list[str] = Field(default_factory=list)
    relation_type: str = "foreign_key"
