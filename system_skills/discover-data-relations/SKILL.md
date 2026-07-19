---
name: discover-data-relations
description: >
  从任意业务场景的异构数据、规则和结果材料中推导一张宏观、可核验、可供后续流程推导使用的数据关系图。适用于 CSV、XLSX、JSONL、Parquet、SQLite、文本、PDF、DOCX、PPTX 和图片混合场景。先以有界扫描蒸馏字段级证据，再由 Agent 综合为少量业务数据域、规则、判定与结果节点；字段、记录值和底层匹配只作证据，不进入最终图。用于整体数据关系、业务数据血缘、跨材料关联、规则约束和审计输入输出分析。
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
- 不为了图看起来完整而补关系。证据不足就删边或明确待确认。
- main_chain 是兼容字段，语义上表示“主数据路径”，不是业务流程步骤。
- 只有顶层 scenario-relationship.json 状态为 complete、无 validation-errors.json，且 relations.mmd、relation-report.md 均存在时才算完成。

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

状态为 partial 时，用完全相同的命令恢复。状态为 ready_for_synthesis 后，向用户说明识别到的材料类型、文件覆盖和下一步；不要重新扫描。

若需要恢复紧凑简报，只执行一次：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py brief \
  --brief /workspace/outputs/data-relations/synthesis-brief.json
~~~

只有某一条必要关系缺证据时，才按证据 ID 或文件定向查询，单次不超过 20 条：

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

### 3. 整体验收

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py finalize \
  --claims /workspace/outputs/data-relations/scenario-claims.candidate.json \
  --output /workspace/outputs/data-relations \
  --summary-limit 20
~~~

若返回 validation_failed：

1. 一次读取 validation-errors.json；
2. 在内存中同时修正全部问题；
3. 一次重写完整候选；
4. 再次执行 finalize。

不得一条错误跑一次命令，也不得在没有新增证据或结构调整时重复相同尝试。相同根因连续失败时，报告真实阻塞和缺失证据。

### 4. 交付

验收后读取有界摘要：

~~~bash
python /skills/discover-data-relations/scripts/analyze_relations.py summary \
  --result /workspace/outputs/data-relations/scenario-relationship.json \
  --offset 0 --limit 20
~~~

调用 report_task_progress(action="complete")，在 message 和最终答复中说明：

- 场景范围与主数据路径；
- 宏观节点数、关系数和文件覆盖；
- 哪些结论是直接证据、哪些仍有边界；
- 三个主要交付文件的路径。

## 恢复与上下文压缩

上下文不足时，先确保已有磁盘检查点，再报告 compact。新上下文按顺序检查：

1. prepare-status.json
2. synthesis-brief.json
3. scenario-claims.candidate.json 或 scenario-claims.json
4. validation-errors.json
5. scenario-relationship.json

已有 ready_for_synthesis 不得重扫数据；已有候选直接验收或整体验修；已有 complete 结果只读摘要并交付。压缩是为了继续同一任务，不是增加固定轮次。

## 最终产物

- relations.mmd：宏观 Mermaid 数据关系图。
- relation-report.md：逐关系说明、证据、置信度和覆盖边界。
- scenario-relationship.json：供后续 Skill 使用的结构化关系；同时含 main_chain 和同义的 primary_data_path。
- relations.json：兼容副本。
- evidence.sqlite3：仅保存最终关系引用过的证据。
- evidence-cards.json、synthesis-brief.json：有界中间证据。
- _field-evidence/：内部探针产物，不得作为最终结果展示。

证据门槛和性能边界见 [references/evidence-model.md](references/evidence-model.md)。
