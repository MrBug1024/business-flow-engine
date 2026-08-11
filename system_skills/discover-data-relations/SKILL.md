---
name: discover-data-relations
description: >
  从任意业务场景的异构数据、规则和结果材料中推导宏观关系图与可执行数据契约。适用于 CSV、XLSX、JSONL、Parquet、SQLite、文本、PDF、DOCX、PPTX 和图片混合场景。先自动识别真实表头，以有界扫描和基数统计追踪字段链路，再由 Agent 综合业务数据域、规则、判定与结果节点；同时输出经证据排序的连接键、非结构化解析/OCR检索路径和质量门禁，供流程推导及能力蒸馏使用。
metadata:
  completion:
    triggers:
      - 推导数据关系
      - 挖掘数据关系
      - 生成关系图谱
      - 发现数据关系
      - discover-data-relations
    required_artifacts:
      - outputs/data-relations/scenario-relationship.json
      - outputs/data-relations/relations.mmd
      - outputs/data-relations/relation-report.md
      - outputs/data-relations/operational-data-contract.json
      - outputs/data-relations/trace-samples.json
    forbidden_artifacts:
      - outputs/data-relations/validation-errors.json
    status_checks:
      - artifact: outputs/data-relations/scenario-relationship.json
        field: status
        allowed: [complete]
      - artifact: outputs/data-relations/operational-data-contract.json
        field: status
        allowed: [ready]
      - artifact: outputs/data-relations/trace-samples.json
        field: status
        allowed: [complete, blocked]
  capability:
    id: discover-data-relations
    responsibility: 从业务材料中提取有界证据并生成可核验的宏观数据关系交付物。
    excludes:
      - 推导端到端业务流程或操作时序
      - 将完整业务场景蒸馏为新 Skill
      - 校验、复制或打包最终 Skill 能力包
---

# 业务场景宏观数据关系发现

目标不是列出字段匹配，也不是猜一条业务流程。目标是回答：

1. 场景有哪些稳定的数据域、规则和结果？
2. 它们如何关联、汇入判定并形成结果？
3. 每条宏观关系由哪些可定位证据支撑？

最终图建议 5-8 个节点，硬上限为 10 个节点、14 条边、3 个分支。后续流程 Skill 应能直接使用这些业务概念和数据依赖。

## 不可违反的边界

- 不直接读取 /workspace/data 的原始大文件；使用脚本生成的有界证据简报。
- 不把字段、工作表、单条记录、ID、代码、金额、日期或具体取值画成节点。
- _field-evidence/ 仅是内部字段证据，绝不是用户交付物。
- 精确字段指纹只证明可关联或可追溯，不能独立证明时序、触发或因果。
- 不能只凭同名字段选连接键。候选必须结合字段语义、基数、唯一性和结果反向追踪排序，并在运行时再次校验空值、未匹配与连接放大。
- 若存在结构化历史结果，必须生成同一结果锚点的 `trace-samples.json`：全量搜索来源表，只物化少量脱敏命中行，并把规则定位、键组、行号、基数和截断边界一并交给关系综合；禁止用各表独立前 N 行或随机行代替。
- 全量搜索、候选结果表和候选锚点行只属于本地确定性算法。它可在本地比较候选链路完整度，但 `trace-samples.json` 与 `synthesis-brief.json` 最终只能暴露**一条**选中的结果锚点链路；不得把多个锚点行拼成一条链，也不得把未追踪来源的原始行交给 Agent。
- Agent 的原始值输入只能来自该单一 `record_trace`；其它材料最多以无取值的文件/表结构和已使用连接键元数据出现。没有可执行的单一结果追踪时应返回 `blocked_trace_required`，不得退回独立表样本继续推导。
- TXT、Markdown、Word、PDF 和图片通过带页码/段落/行号的解析或 OCR 证据参与关系推导；语义相似不能替代结构化业务主键。
- 历史结果文件只能定义 `design_time_template`：保留格式、字段/章节、类型、定位和可选的有界脱敏示例，原文件不得成为第三方运行时依赖。CSV、Excel、PDF、图片、Word、TXT、Markdown 等所有格式都遵守同一生命周期规则。
- 外部知识库、爬虫、Web 检索和远程 API 必须建模为 `system` 能力节点，不得把本地结果样例或其他物理文件证据分配给它们。是否调用由 Agent 根据用户请求和完整规则记录判断。
- 不为了图看起来完整而补关系。证据不足就删边或明确待确认。
- main_chain 是兼容字段，语义上表示“主数据路径”，不是业务流程步骤。
- 只有顶层 scenario-relationship.json 状态为 complete、`operational-data-contract.json` 为 ready、无 validation-errors.json，且 relations.mmd、relation-report.md 均存在时才算完成。
- 本 Skill 的全部过程与交付文件只能位于 /workspace/outputs/data-relations。不得复制到 /workspace/deliverables/skill-package，也不得创建或填充任何最终 Skill 能力包目录；能力包是完整业务场景验收后的另一项独立任务。
- 命令运行环境使用宿主原生 Shell，不假设 Bash。每次只执行文档中的一条完整命令；不得使用 `|`、`&&`、重定向、`head`、`cat`、`wc`、heredoc 或内联 Python，也不得创建临时脚本来读取、裁剪或改写 JSON。

## 用户可见进展

若挂载了 report_task_progress，先报告一个简短计划，并在每次调用中填写 message。每条 message 都是独立 AI 回复，应说明业务理解、已得到的结果或下一步，不能罗列命令。

建议工作项：

1. 盘点材料并形成有界证据
2. 综合宏观数据关系
3. 校验图谱与文件覆盖
4. 交付关系图、说明和结构化 JSON

只在计划、工作项开始、取得可核验结果、真实受阻和最终完成时更新。不得为每个文件、节点、边、模型轮次或命令上报一条进展。

## 执行

Skill 目录为 /skills/discover-data-relations，可写工作区为 /workspace。

Studio 在首次执行本 Skill 或 `requirements.txt` 摘要变化时，自动把依赖串行安装到共享 venv 并记录摘要；不要为每次分析重复安装。若脱离 Studio 单独运行，必须先执行 `python -m pip install -r requirements.txt`。大型 XLSX 的定向扫描要求 `fastexcel` 与 `pyarrow` 可用；依赖准备失败时应立即报告运行时错误，不得静默使用逐单元格慢路径运行几十分钟。

### 1. 准备证据

报告计划和第一个工作项后直接运行：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py analyze \
  --input /workspace/data \
  --output /workspace/outputs/data-relations \
  --goal-file /workspace/description.md \
  --ocr-mode auto \
  --deadline-seconds 780 \
  --summary-limit 20
~~~

状态为 partial 时，用完全相同的命令恢复。状态为 ready_for_synthesis 后，返回值已经包含严格限长的 synthesis_brief。它的 `ai_input_policy.raw_value_scope` 必须为 `one_selected_result_anchored_trace`；只使用其中的单一追踪卡、链路表结构和已使用连接键综合，向用户说明识别到的材料类型、文件覆盖和下一步，不要重新扫描，也不要另写脚本压缩材料。

若需要恢复紧凑简报，只执行一次：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py brief \
  --brief /workspace/outputs/data-relations/synthesis-brief.json
~~~

只有某一条**已在该结果锚点链路中出现**的必要关系缺证据时，才按证据 ID 定向查询，单次不超过 20 条；不得用 `evidence` 读取链路范围外的原始行：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py evidence \
  --ids "E-xxxxxxxxxxxx,E-yyyyyyyyyyyy" --offset 0 --limit 10
~~~

### 2. 综合宏观关系

完整阅读 [references/scenario-synthesis.md](references/scenario-synthesis.md)。从简报一次性完成场景综合：

- 合并同一业务含义，避免“一张表一个节点”。
- 选择一条从核心业务数据到最终结果的主数据路径。
- 用 joins_with 表示跨数据域可关联，用 feeds 表示数据汇入处理/判定，用 governs 表示规则约束，用 derives 表示形成结果。
- 旧类型仅在证据明确且语义确实匹配时使用。
- 规则、辅助知识和格式模板通常是侧向依赖，不应机械塞进主数据路径。

将完整候选一次写入：

/workspace/outputs/data-relations/scenario-claims.candidate.json

候选必须是合法 JSON，使用 scenario-claims.template.json 的结构，内容保持在 20 KB 内。允许使用一次 write_file 写入这个候选文件；不要用几十个原子命令逐节点、逐边拼装，也不要覆盖正式的 scenario-claims.json。

### 3. 预检与定点修复

先预检候选，预检不会覆盖正式结果，也不会删除失败候选：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py preflight \
  --claims /workspace/outputs/data-relations/scenario-claims.candidate.json \
  --output /workspace/outputs/data-relations
~~~

状态为 valid 才进入 finalize。若为 validation_failed：

1. 直接使用返回的 repair_target、errors 和 repair_hints，不要搜索或阅读校验器源码；
2. 多个结构问题在内存中一次修正并用一次 write_file 重写同一候选；
3. 单条节点或边属性错误优先用对应的 claims-node / claims-edge 命令定点修改；
4. 只再执行一次 preflight 验证整体验修结果。

例如，只修正一条边的语义类型：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py claims-edge \
  --claims /workspace/outputs/data-relations/scenario-claims.candidate.json \
  --id e_example \
  --edge-type feeds
~~~

兼容类型只表示结构合法，最终选择仍必须符合该边引用的业务证据。不得一条错误跑一轮模型，不得创建辅助脚本，不得在没有新增证据或结构调整时重复相同尝试。相同根因连续失败时，报告真实阻塞和缺失证据。

若上下文中断时误把未完成的图写到了 `scenario-relationship.json`，而正式候选
`scenario-claims.candidate.json` 仍是旧草稿，只可执行一次显式恢复：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py claims-recover \
  --claims /workspace/outputs/data-relations/scenario-claims.candidate.json \
  --partial /workspace/outputs/data-relations/scenario-relationship.json \
  --output /workspace/outputs/data-relations
~~~

该命令不会直接交付或覆盖一个已通过结构校验的候选；它只会把不完整结果中的可编辑 claims
字段恢复到候选，并逐项报告补齐或移除的内容及理由。恢复成功后仍必须先执行一次正常
`preflight`，仅在其返回 `valid` 后执行 `finalize`。若恢复返回 `recovery_blocked`，不得反复
尝试；根据其中列出的缺失证据或未解决结构项人工修正候选。

### 4. 整体验收

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py finalize \
  --claims /workspace/outputs/data-relations/scenario-claims.candidate.json \
  --output /workspace/outputs/data-relations \
  --summary-limit 20
~~~

finalize 只负责将已经通过预检的候选提升为正式结果并生成交付文件。如果仍返回 validation_failed，候选会原样保留；按返回的 repair_target 恢复，不得重新初始化或覆盖检查点。

### 5. 交付

验收后读取有界摘要：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py summary \
  --result /workspace/outputs/data-relations/scenario-relationship.json \
  --offset 0 --limit 20
~~~

调用 report_task_progress(action="complete")，`artifacts` 必须显式列出上述四个最终产物；在 message 和最终答复中说明：

- 场景范围与主数据路径；
- 宏观节点数、关系数和文件覆盖；
- 哪些结论是直接证据、哪些仍有边界；
- 数据执行契约是否通过表头、输入网络、结果反向追踪和非结构化检索路径门禁；
- 三个主要交付文件的路径。

完成即停在 /workspace/outputs/data-relations，不执行复制、打包或“顺便生成 Skill 包”。

## 业务流程前的微观复现交接（仅在后续需要推导业务流程时执行）

关系图 `finalize` 完成不等于可以直接推导宏观业务流程。关系产物经平台正式批准后，Workbench 会将当前阶段切换为 `micro_process`；此时必须先生成并审阅**结果链路的微观复现契约**。它把一条已批准的结果锚点链路整理为可重放、无样本值依赖的处理原则，防止由单个历史样本直接跳到宏观流程。

只有同时满足以下条件才可生成候选：当前 Workbench 阶段为 `micro_process`、数据关联关系已获平台签名批准、`trace-review.json` 也已获平台签名批准。不要把关系图完成、聊天中的“可以继续”或 Agent 的判断当作批准。

使用唯一的候选生成命令和固定产物路径：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py micro-process-draft \
  --review /workspace/outputs/data-relations/trace-review.json \
  --output /workspace/outputs/data-relations/micro-process.json
~~~

命令返回 `pending_review` 后，可只读取有界摘要来说明待审内容：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py micro-process-summary \
  --micro-process /workspace/outputs/data-relations/micro-process.json
~~~

不要用 `--force` 覆盖一个已有候选来“试试看”；先审阅现有候选，只有平台已经将该阶段退回修订且确有内容修改时，才按新的阶段指令处理。

候选生成后必须停在正式审批交接处：由 Workbench 展示当前 `micro_process` 的签名审批对话框，供用户查看 `/workspace/outputs/data-relations/micro-process.json` 后批准或退回。平台通过 `POST /businesses/{business_id}/confirmations` 记录该决定，并由 `POST /businesses/{business_id}/confirmations/{confirmation_id}/continue/stream` 执行获准的续办；这些是平台专属操作，Agent 不得调用、构造或模拟。批准时平台会写入签名回执 `/workspace/outputs/data-relations/platform-approvals.json`，并把候选更新为 `status: approved`。

严禁调用 `micro-process-approve` CLI、手工修改 `status`/`approval`/`platform-approvals.json`、要求用户在聊天中说“跳过”“继续试试”来绕开审批，或在未批准时运行 `derive-business-flow`。若候选缺失、无效或仍待审，只报告确切阻塞并等待 Workbench 的正式动作。

## 恢复与上下文压缩

上下文不足时，先确保已有磁盘检查点，再报告 compact。新上下文按顺序检查：

1. prepare-status.json
2. synthesis-brief.json
3. scenario-claims.candidate.json 或 scenario-claims.json（对找到的文件执行 preflight）
4. validation-errors.json
5. scenario-relationship.json

已有 ready_for_synthesis 不得重扫数据；已有候选直接验收或整体验修；已有 complete 结果只读摘要并交付。压缩是为了继续同一任务，不是增加固定轮次。

## 最终产物

- relations.mmd：宏观 Mermaid 数据关系图。
- relation-report.md：逐关系说明、证据、置信度和覆盖边界。
- scenario-relationship.json：供后续 Skill 使用的结构化关系；同时含 main_chain 和同义的 primary_data_path。
- operational-data-contract.json：来源生命周期、运行时绑定、正确表头、列/章节、规则/结果角色、排序后的字段连接、外部增强能力、非结构化检索路径及质量门禁。
- trace-samples.json：从少量候选结果行反向追踪到规则和业务来源的同锚点样本包；大表全量搜索、Agent 上下文有界物化。
- relations.json：兼容副本。
- evidence.sqlite3：仅保存最终关系引用过的证据。
- evidence-cards.json、synthesis-brief.json：有界中间证据。
- _field-evidence/：内部探针产物，不得作为最终结果展示。

证据门槛和性能边界见 [references/evidence-model.md](references/evidence-model.md)。
