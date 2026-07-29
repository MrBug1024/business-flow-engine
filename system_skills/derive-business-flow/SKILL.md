---
name: derive-business-flow
description: >
  仅在 discover-data-relations 已生成并验收 complete 的 scenario-relationship.json、relations.mmd 和 relation-report.md 后，消费这些产出推导整个业务场景的宏观业务流程、阶段流转、状态、控制和分支，并生成可核验的业务流程图、报告与结构化 JSON。用于“根据关系图谱推导业务流程”“梳理端到端业务阶段”“识别宏观状态、决策、交接和例外”等请求。主数据路径只作依赖骨架，不直接视为流程；历史记录只用于验证、参照和对账，不能固化为逐记录、逐字段或偶然操作顺序。缺少或未验收上游关系产物时必须阻塞，不得直接从原始数据猜流程。
metadata:
  capability:
    id: derive-business-flow
    responsibility: 基于已验收的数据关系产物推导并验收一个场景级宏观业务流程交付物。
    excludes:
      - 扫描原始业务材料或重新发现数据关系
      - 将历史记录轨迹固化为微观标准流程
      - 将关系与流程蒸馏、复制或打包为最终 Skill 能力包
---

# 业务场景宏观流程推导

目标是回答整个场景“为什么启动、经过哪些稳定的业务责任、在哪里判断或交接、最终形成什么业务结果”。输出不是某批历史记录的操作回放，也不是把上游 `main_chain` 改名为流程。

## 不可违反的边界

- 唯一标准输入是 `/workspace/outputs/data-relations/scenario-relationship.json`；其 `status` 必须为 `complete`。
- 同目录必须存在 `relations.mmd` 和 `relation-report.md`，且不得存在 `validation-errors.json`。
- 缺少上游验收产物时只生成阻塞状态并停止。不得读取 `/workspace/data`，不得代替 `discover-data-relations` 扫描材料。
- 上游 `main_chain`/`primary_data_path` 是主数据依赖路径，不是现成的业务步骤或时序证据。
- 流程阶段必须是跨业务实例稳定成立的宏观责任或业务结果。文件、工作表、表、字段、记录、ID、金额、日期和具体值不得成为阶段或状态。
- 允许把已验收的方向性依赖综合为 `structural` 流程推断，但必须写明理由和置信度。没有支撑的顺序、参与方或分支只能进入 `open_questions`。
- `explicit` 时序只可由上游 `triggers`、`precedes`、`branches_to` 或 `returns_to` 关系支撑。
- 历史数据只验证覆盖、顺序一致性、可追溯性、分支合理性、控制符合性和结果对账；不得通过“多数记录恰好如此”定义标准流程。
- 规则是约束 `control`，数据域是阶段输入/输出或验证对象；不要机械地把每个关系节点变成一个流程阶段。
- 全部过程与交付文件只能位于 `/workspace/outputs/business-flow`。不得创建或填充能力蒸馏、最终 Skill 包或 `/workspace/deliverables/skill-package`。
- 命令运行环境使用宿主原生 Shell，不假设 Bash。每次只执行文档中的一条完整命令；不得使用管道、重定向、heredoc 或临时脚本裁剪 JSON。

## 用户可见进展

若挂载了 `report_task_progress`，先报告以下四项计划：

1. 验收关系图谱前置产物
2. 综合场景级宏观流程与状态
3. 校验推断依据、覆盖和历史验证边界
4. 交付流程图、报告和结构化 JSON

只在计划、工作项开始、得到可核验结果、真实受阻和最终完成时更新。进展应说明业务理解和结果，不罗列命令。

## 执行

Skill 目录为 `/skills/derive-business-flow`，可写工作区为 `/workspace`。

### 1. 强制前置验收

先执行且只能先执行：

~~~text
python /skills/derive-business-flow/scripts/derive_business_flow.py prepare --relations /workspace/outputs/data-relations/scenario-relationship.json --output /workspace/outputs/business-flow --summary-limit 20
~~~

结果为 `blocked_missing_or_invalid_relations` 时，报告缺少或无效的上游项并停止。建议用户先完成 `discover-data-relations`，但本 Skill 不自行读取原始数据代办上游。

结果为 `ready_for_synthesis` 时，返回值已包含严格有界的 `flow_brief`。直接使用它，不要重新读取原始材料，也不要把 `relation-report.md` 全文塞入上下文。

若上下文恢复时需要重新取得简报，只执行：

~~~text
python /skills/derive-business-flow/scripts/derive_business_flow.py brief --brief /workspace/outputs/business-flow/flow-brief.json
~~~

### 2. 综合宏观业务流程

完整阅读 [references/inference-protocol.md](references/inference-protocol.md) 和 [references/flow-contract.md](references/flow-contract.md)。使用 `flow-claims.template.json` 的结构，一次性综合候选并写入：

`/workspace/outputs/business-flow/flow-claims.candidate.json`

综合时：

- 先定义场景级业务结果，再识别 4-8 个左右的稳定阶段；硬上限为 10 个。
- 用上游业务节点作为阶段输入、输出、状态、规则或验证对象，而不是逐节点复制成步骤。
- 将 `feeds`、`derives` 等方向性关系作为结构性推断依据；`joins_with` 只能证明可关联，不能证明先后。
- 主流程每相邻阶段必须有 `normal` 或 `handoff` 流转。
- 条件或异常分支必须由规则、判定或明确 `branches_to` 支撑；返回流转必须有 `returns_to`。
- 每个阶段、流转和状态都声明 `explicit` 或 `structural`、置信度、推导理由和上游证据。
- 为全部上游节点和关系做覆盖交代：进入流程骨架的列入 `used`，只作上下文的列入 `context_only` 并说明原因。
- 把历史数据的作用写成 `validation_checks`；这些检查只能验证候选流程，不能生成新步骤。

允许使用一次 `write_file` 写完整候选。不要逐字段、逐记录或逐节点拼装，也不要覆盖 `flow-claims.template.json`。

### 3. 预检与修复

执行：

~~~text
python /skills/derive-business-flow/scripts/derive_business_flow.py preflight --claims /workspace/outputs/business-flow/flow-claims.candidate.json --relations /workspace/outputs/data-relations/scenario-relationship.json --output /workspace/outputs/business-flow
~~~

只有状态为 `valid` 才能进入 `finalize`。若为 `validation_failed`：

1. 直接使用返回的 `errors`、`repair_hints` 和 `repair_target`，不要搜索或读取校验器源码；
2. 在内存中一次性修正结构、引用、粒度和覆盖问题，并用一次 `write_file` 重写同一候选；
3. 未获支撑的流程主张应降级到 `open_questions`，不能只调高置信度或借历史频次补证据；
4. 只在内容真实调整后再运行一次 `preflight`。

若提示上游 fingerprint 过期，重新运行 `prepare` 并基于新简报综合，不得继续使用旧候选。

### 4. 整体验收与生成交付物

预检通过后执行：

~~~text
python /skills/derive-business-flow/scripts/derive_business_flow.py finalize --claims /workspace/outputs/business-flow/flow-claims.candidate.json --relations /workspace/outputs/data-relations/scenario-relationship.json --output /workspace/outputs/business-flow --summary-limit 20
~~~

`finalize` 会再次校验前置产物和候选，随后确定性生成 JSON、Mermaid 和报告。若仍失败，保留候选并按同一 `repair_target` 修复，不得跳过校验手写正式产物。

### 5. 交付

验收后读取有界摘要：

~~~text
python /skills/derive-business-flow/scripts/derive_business_flow.py summary --result /workspace/outputs/business-flow/business-flow.json --offset 0 --limit 20
~~~

只有同时满足以下条件才算完成：

- `business-flow.json` 顶层 `status` 为 `complete`；
- `business-flow.mmd` 与 `business-flow-report.md` 存在；
- `flow-claims.json` 存在；
- 不存在 `validation-errors.json`；
- `source.fingerprint` 与当前上游 `scenario-relationship.json` 一致。

调用 `report_task_progress(action="complete")`，并说明场景业务结果、宏观阶段数、主流程、分支与状态、显式/结构性推断边界、历史验证角色和三个主要交付路径。完成即停止，不执行蒸馏或打包。

## 恢复与上下文压缩

上下文不足时先确认磁盘检查点，再报告 compact。新上下文按顺序检查：

1. `prepare-status.json`
2. `flow-brief.json`
3. `flow-claims.candidate.json` 或 `flow-claims.json`
4. `validation-errors.json`
5. `business-flow.json`

已有 `ready_for_synthesis` 不得重复验收或读取原始材料；已有候选直接预检；已有 `complete` 且 source fingerprint 未变化时只读摘要并交付。

## 最终产物

- `business-flow.json`：供后续能力蒸馏使用的场景级结构化流程。
- `business-flow.mmd`：宏观主流程、分支、状态和控制的 Mermaid 图。
- `business-flow-report.md`：逐阶段推导依据、置信度、状态、控制、验证计划和边界。
- `flow-claims.json`：通过验收的正式流程主张。
- `flow-brief.json`：从上游产物生成的有界综合简报。
- `prepare-status.json`：前置门槛和恢复检查点。
