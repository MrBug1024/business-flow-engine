# 业务流逆向工程引擎（Business Flow Reverse-Engineering Engine）

> 版本 v1.1.0 ｜ 一句话概括：**把「历史业务数据 + 业务知识/规则 + 历史处理结果」这三样东西，逆向蒸馏成一个任意 LLM Agent 都能调用的可复用业务能力（Skill），而不是再写一遍业务代码。**

---

## 一、这个项目要解决什么问题

几乎每一个成熟的业务团队（医保审计、财务稽核、风控、质检……）手上都攒着三样东西：

1. **历史业务数据**——就诊表、结算表、订单表、项目明细表……体量从几万到几十万行不等；
2. **业务知识/规则**——审计规则、定价标准、判定口径……可能有几十条，也可能有成百上千条，
   而且大多是自然语言描述（"不得同时收取 A 和 B""同一就诊超过 N 次视为异常"），不是现成的
   SQL 或 if/else；
3. **历史处理结果**——过去人工/系统跑出来的正确结果样例（如某一批违规费用清单）。

这三样东西凑在一起，其实就隐含了一条**完整但没人写下来的业务流程**：从原始数据出发，
应用某些规则、做某些关联和计算，才能得到那份历史结果。这条流程通常只存在于业务专家的
经验里，或者散落在无数版本的 Excel 宏 / 存量脚本里——换一个人、换一批数据，就得重新
摸索一遍。

**本项目要做的事**：让 AI 读懂这三样东西之间的因果关系，**逆向推导出这条业务流程**，
再把它**蒸馏固化成一个完全独立、可移植的 Skill 包**——这个 Skill 包不依赖本平台，
可以被任意支持工具调用的 LLM Agent（Claude / GPT / 其他）挂载执行，用来处理**任意新上传
的同类业务数据**，得到跟历史结果同样口径的产出。

## 二、终极目标与核心设计哲学

### 终极目标

> 让"一次逆向蒸馏出的业务能力"能够脱离平台、脱离原始数据、脱离编写者，被任何一个
> 具备推理能力的 LLM Agent 拿去处理**该业务场景下用户能提出的任何要求**——而不仅仅是
> 复刻历史样本里出现过的那几条记录。

围绕这个目标，项目在实践中沉淀出几条不可动摇的设计铁律：

**1. 值千变万化，结构永恒**
蒸馏阶段只学"结构"：哪张表跟哪张表怎么关联、哪一列是分派键、哪些列是自然语言条件、
字段的业务语义是什么。**具体的业务判断值**（某条规则的关键词、某个阈值、某条历史记录
的字面内容）永远不固化进代码或 SQL 模板里——那些是"数据"，不是"逻辑"。

**2. 🛑 铁律：永远不为某一条具体规则/记录写死一条专属 SQL、判断条件或计算公式**
这是本项目在实践中吃过教训后确立的最高纪律。真实业务场景里，知识表可能有成百上千条
规则，业务表可能有几十万行，历史结果可能只有几十条——如果蒸馏阶段试图为每一条规则都
"解题"、预先派生一条专属 SQL，既不可能穷尽真实使用时用户提出的各种变体要求，也完全
不现实（几百条规则 = 几百次预推导，还要在每次新数据/新规则上传时重新来一遍）。
正确做法是：**蒸馏阶段只产出结构化的知识地图（知识表长什么样、字段怎么对应业务表），
真正"这条规则该怎么判断、该怎么查"这件事，交给运行时读到规则原文的 LLM 现场推理**——
因为 LLM 是会思考的，不需要平台替它把所有可能性都提前写成代码。

**3. 因果链驱动采样，而不是"各表各拍脑袋抽几行"**
早期版本给 AI 看的样本是"每张表各自随机抽 2 行"，导致 AI 看到的业务行、规则行、
结果行之间根本没有真实关联，靠字段名硬猜出一堆无中生有的"关联关系"。现在改为
**Trace-Driven Sampling**：以历史结果表的某一行为锚点，反向在业务表/知识表中追踪出
真正因果相关的那几行，形成一条"结果行 → 业务行 → 知识行"的完整证据链，AI 看到的
永远是一条真实发生过的因果链条，而不是几张互不相干的截图拼在一起。

**4. 关联推导以「值证据」为主，「字段名相似」只是弱先验**
两个字段是否存在关联，最终看的是**真实取值是否重合**（尤其是小表对大表的包含率，而不是
简单的 Jaccard——否则"25 行结果表 vs 74 万行业务表"这种典型场景算出来的重合度会趋近于
0，误判成"无关联"）。字段名像不像，只作为兜底的弱先验参考。

**5. 蒸馏通道与验证通道彻底隔离**
"逆向推导 + 生成 Skill"和"挂载 Skill 包做执行验证"是两个物理隔离的 Agent，
工具集完全不重叠：蒸馏 Agent 碰不到执行工具，验证 Agent 碰不到平台内部的推导工具。
这保证了"验证通道证明的是 Skill 包本身能不能独立跑通"，而不是继续依赖平台内部状态。

**6. 历史结果只是格式模板，不是能力天花板**
上传的历史结果表**只代表一次产出的列结构**（用了哪些列、叫什么名、什么格式）。
逆向推导完成后，生成的 Skill 应该能对知识表中**任意条目**执行，得到全新的结果——
不应该被"历史样本里出现过的那几条"焊死，那正是本项目最初要解决的"只学会应付一条
历史记录"的核心症结。

## 三、系统架构

### 3.1 三层架构

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1／2：元数据 + 因果采样层（metadata.py / trace_sampling.py）│
│   只给 AI 看「表结构 + 字段语义 + 因果链样本」这份蓝图，        │
│   AI 绝不逐行翻看全量数据（几十万行的表不会被塞进上下文）。      │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 3：AI 推理层（inference.py + deepagents 工具编排）        │
│   读蓝图 → 推关联 + 字段语义 → 推业务流程节点 →                │
│   蒸馏知识表结构映射 → 细化模板算子参数                        │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 4：技能执行层（skill_builder.py 生成，与平台代码彻底解耦）  │
│   完全独立的 Python 脚本包，只依赖 pandas / duckdb / openpyxl， │
│   可被任意 AI Agent 或人工直接调用，不需要回连本平台。          │
└─────────────────────────────────────────────────────────┘
```

### 3.2 两条通道（Two Channels）

| | 蒸馏通道（Distillation） | 验证通道（Verification） |
|---|---|---|
| 入口 | `/`（`web/index.html`） | `/verify`（`web/verify.html`） |
| Agent | `app/agent.py` 的蒸馏 Agent | `app/verification_agent.py` 的验证 Agent |
| 能调用的工具 | `deduce_relations` / `deduce_flow` / `generate_skills` 等平台内部工具（`app/tools.py`） | 只能调用生成好的 Skill 包里的脚本（`list_outputs` / `execute_skill` / `search_knowledge` / `query_data` 等） |
| 数据 | 蒸馏阶段上传的 `uploads/` | 与蒸馏数据物理隔离的 `verify_uploads/`（证明 Skill 能在**新数据**上跑通） |
| 职责 | 推关联 → 推流程 → 生成 Skill，**到此为止，绝不执行** | 挂载 Skill 包，验证它能否独立完成业务场景要求的任何事 |

两条通道通过 `app/agent_guard.py` 的 `ExcludeBuiltinToolsMiddleware` 保证 `deepagents`
框架自带的内置工具（`write_todos`/`execute`/`task` 等）不会泄漏给模型，避免 Agent
绕过既定工具体系"自己搞一套"或谎报任务已完成。

### 3.3 技术栈

- **后端**：FastAPI + Uvicorn（纯 REST + SSE 流式接口，无 WebSocket）
- **AI 编排**：LangChain 1.x + `deepagents`（`create_deep_agent`，自带 ReAct 风格的工具调用循环）
- **LLM**：默认接入 MiniMax（OpenAI 兼容接口），可通过 `.env` 指向任意 OpenAI 兼容服务；
  **未配置 LLM 时全流程自动降级为纯启发式规则推导**，保证项目在无大模型环境下也能跑通
- **数据处理**：pandas + DuckDB（嵌入式 SQL 引擎，无需部署数据库服务）+ openpyxl / python-calamine
  （大 Excel 全量加载用，比 openpyxl 快 5 倍以上）
- **存储**：无数据库，纯文件系统（每个业务场景一个目录，`meta.json` + `chat.jsonl` + `uploads/` + `skills/`）
- **前端**：无构建工具的原生 HTML/CSS/JS 单页应用（`web/index.html` + `web/app.js`，`web/verify.html` 独立一份）

## 四、核心数据模型（`app/models.py`）

- **`Scenario`**：业务场景聚合根，持有一次逆向蒸馏的全部产物（表结构、关联、流程、
  技能、产出、校验记录）。
- **`TableMeta` / `ColumnMeta`**：表结构元信息。每张表有 `role`（`input`业务表 /
  `knowledge`知识表，`rule` 为旧称向后兼容 / `result`历史结果表）；每个字段有 `semantic`
  （业务含义一句话）+ `semantic_role`（`PK`/`FK`/`DIM`/`METRIC`/`TIME`/`NL_TEXT`/`CATEGORY` 七选一）。
- **`Relation`**：表间关联关系，支持**复合键**（`from_columns`/`to_columns` 多列组合，
  单列不足以唯一确定对应关系时使用），支持**人工确认**（`confirmed=True` 后重新推导
  也不会被覆盖）。
- **`RelationResult`**：关联推导的合并结果，附带 `trace_chain`——因果采样算出来的证据链，
  推流程阶段直接复用，不重新对大表搜一遍。
- **`KnowledgeSchemaMapping`**：知识表的结构映射（哪列是分派键、哪些列是自然语言条件、
  `field_role_map` 记录知识字段与业务表字段的对应关系）。**不包含**任何具体规则的执行
  SQL——这正是第二节"铁律"在数据模型层面的体现。
- **`FlowStep`**：业务流程中的一个节点，既有给人看的 `purpose`/`capability`/`data_in`/
  `data_out`，也有可执行的 `template_kind`/`params`/`sql`。结构性节点（过滤/聚合/连接等）
  用固定算子编译成 SQL；知识驱动节点（`knowledge_driven_join`）不预先编译 SQL，标记为
  "运行时由 LLM 现场判断"。
- **`Skill`**：技能索引项，每个流程节点对应一个子技能，另有一个串联全流程的"主技能"。
- **`OutputSpec`**：一种历史产出的复刻规格（列契约 + 输出格式 + 由 `FlowStep` 组成的
  可执行 pipeline）。

## 五、完整蒸馏工作流（5 步，每步由用户一条指令触发）

1. **上传数据并标注角色**：上传业务表/知识表/历史结果表，为每张表选择角色
   （`input`/`knowledge`/`result`）。
2. **推导关联关系**（`deduce_relations`）：以值重合证据为主、字段名相似为辅，推导表间
   关联与字段语义；同时跑一次 Trace-Driven Sampling 建立因果证据链；用户可在此确认/
   修正任意一条关联（`correct_relation` / 前端"确认关联"按钮），修正会**强制**重新在
   真实数据上验证并固化，不是嘴上"已采纳"。
3. **推导业务流程**（`deduce_flow`）：复用步骤 2 的因果证据链（而非重新采样），LLM
   反推出流程节点链，蒸馏出知识表的结构映射；每个节点都要说清楚"该做什么/能做什么/
   数据怎么流动"，任何疑点都会作为 `ambiguous_questions` 抛给用户确认，绝不自行拍板。
4. **生成技能**（`generate_skills`）：把流程节点固化为独立的 Skill 包（见第六节），
   蒸馏工作到此结束。
5. **验证执行**：切换到 `/verify` 验证通道，挂载刚生成的 Skill 包，用新上传的测试数据
   验证它能否独立完成场景要求的任务。

全程遵守"🛑 一步一停"：每条用户消息最多触发一个主工具，做完立刻停下汇报结果，
绝不自作主张连续执行下一步。

## 六、生成的 Skill 包长什么样

```
data/scenarios/<scenario_id>/skills/
├── SCENARIO_CONTEXT.md        # 给任意 AI Agent 的完整场景说明文档，可完全脱离平台阅读使用
├── main_skill/
│   ├── SKILL.md                # 人类可读的技能说明
│   ├── domain_knowledge.json    # 数据字典 + 表间关联 + 字段语义
│   ├── output_specs.json        # 产出规格（含可执行 pipeline SQL）
│   ├── dispatch_config.json     # 知识表结构配置（分派键列、field_role_map……不含任何具体 SQL）
│   ├── schema.json              # OpenAI function-calling 格式的工具定义
│   └── scripts/skill_executor.py  # 完全独立的执行脚本，只 import json/re/pathlib/duckdb/pandas
├── utils/scripts/{search,list}_knowledge.py   # 知识条目检索/浏览工具
├── skill_data_reader/          # 新数据读取与字段校验
├── skill_nl_rule_parser/       # 自然语言规则的粗粒度信号扫描（辅助理解，不驱动执行）
└── step_N_{节点名}/            # 每个流程节点单独固化的子技能
```

**执行模式分两种**：
- **结构性产出（pipeline 模式）**：过滤/聚合/连接等关系代数操作，蒸馏时就能编译出完整
  可执行 SQL，`skill_executor.py` 可以直接独立跑出最终结果。
- **知识驱动产出（`knowledge_engine` 模式）**：`execute_skill` 只会返回命中的规则原文 +
  `field_role_map`，明确告知调用方"本执行器不会替你猜每条规则该怎么判断"——由挂载
  这个 Skill 包的 LLM Agent 自己读规则原文、结合业务表真实字段现场构造查询
  （`query_data`），逐条规则得出结果。这正是第二节"铁律"的运行时落地形态。

## 七、关键技术机制一览

| 机制 | 所在文件 | 作用 |
|---|---|---|
| Trace-Driven Sampling | `trace_sampling.py` | 以单条结果行为锚点反向追踪因果链，而非多行随机拼凑 |
| 值重合证据（containment，非 Jaccard） | `heuristics.py` | 正确处理"小结果表 vs 大业务表"场景下的关联判定 |
| 复合键关联 + 交叉验证 | `heuristics.py` / `trace_sampling.py` / `models.py` | 单字段不足以唯一定位时自动建议/采用组合键 |
| 人工确认关联的强制持久化 | `validators.py` / `tools.py` / `api/graph.py` | 用户明确纠正后强制更新底层数据，不是嘴上采纳 |
| 关联"幻觉"清洗 | `validators.py: sanitize_relations` | 剔除引用不存在表/字段的编造关联 |
| 节点参数字面值硬编码检测 | `validators.py: detect_literal_params` | 防止技能只会处理历史样本里那一条具体记录 |
| 知识驱动执行的运行时下放 | `skill_builder.py` / `verification_agent.py` | 不预先派生规则 SQL，交给验证 Agent 现场推理 |
| 蒸馏/验证双通道隔离 | `agent.py` / `verification_agent.py` / `agent_guard.py` | 工具集完全不重叠，互不越权 |
| 验证查询按需加载 + 整表缓存 | `verification_agent.py: query_data` / `table_io.py` | 只加载 SQL 实际引用的表，calamine 快速引擎 + mtime 缓存；几十万行大表首载一次后秒级复用 |
| 验证轮次生命周期保障 | `verify_service.py` | 每轮必落盘助手消息（含失败/超时/中断），杜绝悬挂 user 消息把新规则误判为多规则并行；心跳帧 + 单轮总超时 + 步数上限 |
| 任务边界纪律 | `prompts/verification/system.md` | 一条用户消息=一个独立任务；失败任务作废不续跑；>10 条规则强制分批并逐条汇报 |
| 执行轨迹可追溯 | `verify_service.py` / `web/verify.html` | 每轮的工具调用（名称/参数/结果/耗时）随消息持久化并在前端可折叠展示，用户可查系统执行了什么、基于什么数据 |

## 八、目录结构

```
business-flow-engine/
├── app/
│   ├── main.py              # FastAPI 应用装配（蒸馏 / 验证双前端路由）
│   ├── models.py             # 领域模型（Pydantic）
│   ├── storage.py            # 文件型持久化（每场景一个目录）
│   ├── config.py             # 全局配置（.env）
│   ├── llm.py                 # LLM 工厂（未配置时返回 None，触发启发式降级）
│   ├── agent.py / agent_guard.py / verification_agent.py  # 蒸馏 / 验证 Agent 构建
│   ├── tools.py               # 蒸馏 Agent 的工具集
│   ├── chat_service.py / verify_service.py  # 两条通道各自的流式对话服务
│   ├── inference.py           # 核心推导逻辑（关联/字段语义/流程/知识结构映射）
│   ├── heuristics.py          # 无 LLM 时的启发式关联推导（也作为 AI 的先验参考）
│   ├── trace_sampling.py      # 因果链驱动采样
│   ├── knowledge_schema.py / rule_schema.py / nl_rule_analyzer.py  # 知识表结构相关
│   ├── validators.py          # 校验闭环（幻觉清洗/字面值硬编码检测/追踪连通性）
│   ├── transform_builder.py / strategies.py  # 确定性关系代数算子（结构性节点用）
│   ├── skill_builder.py       # 技能包生成（本项目体量最大的文件）
│   ├── executor.py / sql_engine.py  # 平台侧的 SQL 执行核心
│   ├── table_io.py            # 统一数据读取（表头探测、Excel/CSV 兼容、缓存）
│   ├── metadata.py            # 元数据蓝图生成（喂给 AI 的唯一信息来源）
│   ├── streaming.py           # SSE 帧封装 + `<think>` 标签解析
│   └── api/                   # FastAPI 路由（scenarios / files / chat / graph / verify）
├── prompts/
│   ├── distillation/system.md    # 蒸馏 Agent 系统提示词
│   ├── verification/system.md    # 验证 Agent 系统提示词
│   └── inference/                # 关联/流程/知识结构/字段语义等推导任务的提示词
├── web/
│   ├── index.html / app.js       # 蒸馏通道前端
│   └── verify.html               # 验证通道前端
├── data/scenarios/<id>/           # 运行时生成，每个业务场景一个独立目录
├── requirements.txt
├── run.py                         # 开发服务器入口
└── .env / .env.example
```

## 九、快速开始

```bash
pip install -r requirements.txt
cp .env.example .env        # 按需填写 LLM API Key；不填也能跑（启发式降级模式）
python run.py                # 默认监听 127.0.0.1:8000
```

浏览器打开 `http://127.0.0.1:8000/` 进入蒸馏通道，`http://127.0.0.1:8000/verify`
进入验证通道。

## 十、这个项目"不是"什么

- **不是**一套固定的行业解决方案（不内置"审计"/"医保"/"财务"等任何领域词汇于平台代码中，
  所有业务语义都作为数据存在于蒸馏出的知识包里）。
- **不是**试图在蒸馏阶段把每条业务规则都编译成确定性代码——真实业务规则的判断逻辑千差
  万别，这件事被有意识地留给运行时会思考的 LLM。
- **不是**要求历史结果与新执行结果逐行精确相等才算验证通过——历史结果本身可能只是一次
  不完整的抽样，验证通道判断的是"新执行结果是否覆盖历史结果"，而不是完全一致。
