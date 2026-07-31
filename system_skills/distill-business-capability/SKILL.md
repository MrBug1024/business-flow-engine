---
name: distill-business-capability
description: >
  仅在 discover-data-relations 与 derive-business-flow 均已完成并验收后，将整个业务场景蒸馏为可脱离本平台、供第三方 Agent 使用的多 Skill 能力源码。为每个宏观流程节点生成且只生成一个带稳定 scripts 入口的阶段 Skill，同时生成带流程状态机的场景总控 Skill；根据上游真实文件格式和外部系统节点按需生成并定制 DuckDB 大表读取、通用文档读取、OCR 与知识库基础 Skill。适用于“把业务流程节点变成 Skill”“生成业务场景能力包源码”“为第三方 Agent 蒸馏可移植能力”等请求。不会从原始历史记录补微观规则，不会复制未定制的通用 SKILL.md，也不负责最终版本化、压缩、签名或发布安装包。
metadata:
  completion:
    triggers:
      - 蒸馏skill能力
      - 蒸馏 skill 能力
      - 生成skill能力包
      - 生成 skill 能力包
      - 生成业务场景能力包
      - 业务场景蒸馏
      - 业务场景skill
      - 业务场景的skill
      - skill能力包
      - skill技能包
      - skill技能
      - skill包生成
      - 蒸馏skill
      - 业务流程节点变成skill
      - distill-business-capability
    required_artifacts:
      - outputs/capability-distillation/capability-manifest.json
      - outputs/capability-distillation/capability-plan.json
      - outputs/capability-distillation/capability-map.mmd
      - outputs/capability-distillation/distillation-report.md
      - outputs/capability-distillation/agent_prompts.md
    forbidden_artifacts:
      - outputs/capability-distillation/validation-errors.json
    status_checks:
      - artifact: outputs/capability-distillation/capability-manifest.json
        field: status
        allowed: [complete]
      - artifact: outputs/capability-distillation/capability-manifest.json
        field: generator_contract_version
        allowed: [2]
    fingerprints:
      - artifact: outputs/capability-distillation/capability-manifest.json
        field: source.relation_fingerprint
        source: outputs/data-relations/scenario-relationship.json
      - artifact: outputs/capability-distillation/capability-manifest.json
        field: source.flow_fingerprint
        source: outputs/business-flow/business-flow.json
      - artifact: outputs/capability-distillation/capability-manifest.json
        field: source.operational_contract_fingerprint
        source: outputs/data-relations/operational-data-contract.json
      - artifact: outputs/capability-distillation/capability-manifest.json
        field: artifact_digests.agent_prompts
        source: outputs/capability-distillation/agent_prompts.md
  capability:
    id: distill-business-capability
    responsibility: 将已验收的场景关系、宏观流程、状态和控制蒸馏为可移植的场景总控、阶段及基础文件处理 Skill 源码。
    excludes:
      - 重新发现数据关系或推导业务流程
      - 从历史样本发明微观业务规则
      - 最终校验、版本化、压缩、签名或发布 Skill 安装包
---

# 业务场景多 Skill 能力蒸馏

生成的不是一份万能提示词，而是一组职责清晰、依赖可见、可由第三方 Agent 按场景调用的 Skill 源码：

1. 一个场景总控 Skill，负责识别请求、路由阶段并遵守主流程；
2. 每个已验收流程阶段恰好一个阶段 Skill；
3. 仅按实际文件格式生成需要的基础读取 Skill；
4. 一份机器可读清单和一份可直接复制到第三方 Agent 的 `agent_prompts.md`。

## 不可违反的边界

- 唯一上游是 `/workspace/outputs/data-relations/scenario-relationship.json` 和 `/workspace/outputs/business-flow/business-flow.json`，二者必须为 `complete`，配套图、报告和正式 claims 必须存在，且不得有 `validation-errors.json`。
- 流程产物引用的关系 fingerprint 必须与当前关系产物一致；任一上游变化后必须重新 `prepare`。
- 不读取 `/workspace/data`，不重新扫描原始文件，不用历史样本补步骤、条件、参与方或业务规则。
- 每个流程阶段恰好生成一个阶段 Skill。不得漏节点、合并多个节点为万能 Skill，或把一个节点拆成未经流程支撑的多个 Skill。
- 每个生成 Skill 必须包含 `scripts/*.py` 的稳定 CLI；阶段 Skill 使用工作单/交接运行器，总控 Skill 使用流程状态机。不得把文件读取、HTTP 调用、流程状态或重复数据处理留给第三方 Agent 临时编写 Python。
- 阶段 Skill 的目标、结果、输入、输出、控制、前后继和待确认项必须来自已验收流程；微观执行细节必须引用这些来源 ID。
- 按上游 evidence-cards 和 coverage 识别真实文件格式。没有 CSV/XLSX 等表格时不生成表格 Skill；没有 PDF/图片时不生成 OCR Skill。
- 表格基础 Skill 必须使用只读 DuckDB 路径、有界预览和大文件策略；全量结果只能由 `export-contract` 写入调用方指定的 CSV/Parquet，不能塞入 Agent 上下文。
- 表格基础 Skill 必须携带上游 `operational-data-contract.json` 的可移植副本，支持完整规则行检索、来源摘要预检、跨来源只读 SQL，以及单键/复合键的空值、未匹配、基数和放大校验；SQL 必须绑定并实际使用通过校验的键组，不得自行猜表头或连接键。
- 可移植契约必须把来源分为 `runtime_input`、`design_time_template`、`design_time_evidence`，把外部知识声明为 `optional_enrichment`。历史结果模板只保留结构元数据和可选脱敏示例，缺失原文件不得阻塞查询；所有格式均适用这一规则。
- 表格运行器只注册当前规则、阶段或 SQL 实际引用的 `runtime_input`，支持 `--bind <source-id>=<relative-path>` 绑定新批次文件，并校验字段兼容而不是历史大小/内容摘要。未引用的运行源或任何设计期模板缺失不得阻塞当前查询。
- TXT、Markdown、Word、可搜索 PDF 等必须先分块建索引再有界检索；图片和扫描 PDF 先 OCR 到 JSON，再进入同一证据索引。命中必须保留来源摘要、页码/段落/行号、chunk id 和文本摘要。
- PDF/图片基础能力必须保留 `ocr-parser` 的路径、URL、Base64、批量、配置和结构化输出能力，但重新生成业务场景专属描述、触发条件、文件角色和约束。不得复制原 `SKILL.md`。
- 流程或关系节点明确需要外部知识库时，必须生成知识库基础 Skill；完整继承 `vector-kb` 的脚本、依赖、配置、检索、原文定位和错误状态，只重建场景描述、触发条件、系统角色与场景绑定。Agent 根据用户请求和完整规则记录决定是否调用；必需知识无法取得时返回 `manual_intervention_required` 并停止相关判断。
- 定制已有系统 Skill 时，除 `SKILL.md`、UI 元数据和新增场景绑定/包装入口外，必须完整继承来源目录。不得挑选复制部分脚本、删除配置字段、清空地址、API Key 或改写已有默认值。
- 若系统 Skill 的凭据来自包内配置、平台 Skill 凭据存储或当前运行环境，生成器必须按原字段写入第三方 Skill 配置；manifest 只记录字段名、来源和是否已配置，不回显凭据值。第三方仍可用同名环境变量覆盖。
- 生成 Skill 内不得出现原平台固定目录、Tool/MCP 网关、进展 Tool 或持久会话假设；资源路径相对于各自 Skill。
- 本 Skill 的产物只能写入 `/workspace/outputs/capability-distillation`。不得写入 `/workspace/deliverables/skill-package`，不得生成 ZIP 或宣称完成发布；最终打包属于 `package-business-skill`。
- 命令运行环境使用宿主原生 Shell。每次只执行文档中的一条完整命令；不得使用管道、重定向、heredoc 或临时脚本修改候选 JSON。

## 用户可见进展

若挂载了 `report_task_progress`，报告以下工作项：

1. 验收关系、流程和文件格式清单
2. 规划场景总控、阶段与基础能力
3. 校验节点覆盖、依赖和可移植性
4. 生成并验收 Skill 源码树

只在计划、工作项开始、取得可核验结果、真实受阻和最终完成时更新。

## 执行

Skill 目录为 `/skills/distill-business-capability`，可写工作区为 `/workspace`。

### 1. 强制前置验收和格式识别

先执行：

~~~text
python /skills/distill-business-capability/scripts/distill_capabilities.py prepare --relations /workspace/outputs/data-relations/scenario-relationship.json --flow /workspace/outputs/business-flow/business-flow.json --output /workspace/outputs/capability-distillation --summary-limit 30
~~~

结果为 `blocked_missing_or_invalid_upstream` 时，报告需要修复的上游并停止。本 Skill 不代办关系发现或流程推导。

结果为 `ready_for_distillation` 时，直接使用返回的有界 `distillation_brief`。它已包含流程节点、控制、待确认项、文件格式、文件角色和应生成的基础能力。

上下文恢复时只执行：

~~~text
python /skills/distill-business-capability/scripts/distill_capabilities.py brief --brief /workspace/outputs/capability-distillation/distillation-brief.json --relations /workspace/outputs/data-relations/scenario-relationship.json --flow /workspace/outputs/business-flow/business-flow.json --output /workspace/outputs/capability-distillation
~~~

### 2. 综合能力计划

完整阅读：

- [references/distillation-protocol.md](references/distillation-protocol.md)
- [references/capability-plan-contract.md](references/capability-plan-contract.md)
- [references/portability-checklist.md](references/portability-checklist.md)

使用 `capability-plan.template.json` 的结构，一次写入：

`/workspace/outputs/capability-distillation/capability-plan.candidate.json`

只填写业务语义字段：Skill 名称和说明、调用条件、场景指令、有依据的阶段 procedure、失败策略和非职责。以下事实由 `prepare` 生成，不得改写：

- source artifact 和 fingerprint；
- 文件清单、扩展名、文件角色和基础能力类型；
- 流程阶段、输入输出节点、控制、前后继、主流程和待确认项；
- 一阶段一 Skill 的 capability ID 和场景总控路由。

所有 `description` 必须同时回答“做什么”和“当前业务场景什么时候调用”。基础 Skill 还必须说明哪些实际格式和业务文件角色会触发它。

### 3. 预检与修复

执行：

~~~text
python /skills/distill-business-capability/scripts/distill_capabilities.py preflight --claims /workspace/outputs/capability-distillation/capability-plan.candidate.json --relations /workspace/outputs/data-relations/scenario-relationship.json --flow /workspace/outputs/business-flow/business-flow.json --output /workspace/outputs/capability-distillation
~~~

只有 `valid` 才可进入 `finalize`。若失败：

1. 使用返回的 `errors`、`repair_hints` 和 `repair_target` 一次性修正同一候选；
2. 不读取校验器源码，不创建辅助脚本，不改写 prepare 生成的上游事实；
3. 规则不足时缩小阶段 Skill 职责或保留待确认项，不得借历史频次补逻辑；
4. 修改后再运行一次 `preflight`。

### 4. 生成可移植 Skill 源码

预检通过后执行：

~~~text
python /skills/distill-business-capability/scripts/distill_capabilities.py finalize --claims /workspace/outputs/capability-distillation/capability-plan.candidate.json --relations /workspace/outputs/data-relations/scenario-relationship.json --flow /workspace/outputs/business-flow/business-flow.json --output /workspace/outputs/capability-distillation --summary-limit 30
~~~

`finalize` 会再次验证候选，确定性生成源码，并扫描所有生成文件是否仍依赖原平台。基础能力生成规则：

- `tabular`：复制可移植 DuckDB/fastexcel 运行模板，再生成场景专属 `SKILL.md`；
- `document`：复制 DOCX、PPTX、文本和可搜索 PDF 提取模板，再生成场景专属 `SKILL.md`；
- `ocr`：完整继承 `ocr-parser` 的脚本、依赖和配置值，重建场景绑定和专属 `SKILL.md`；
- `knowledge`：完整继承 `vector-kb` 的运行资源与配置，增加稳定检索/原文 CLI，再生成场景专属 `SKILL.md`；
- `stage`：生成 `SKILL.md`、阶段契约及 `scripts/run_stage.py`，由脚本创建工作单、拒绝原始大文件输入并校验阶段交接；
- `orchestrator`：生成 `SKILL.md`、能力路由及 `scripts/orchestrate.py`，由脚本维护主流程顺序和可选分支交接。

不得手工绕过 `finalize` 直接拼装正式 `skills/` 目录。

### 5. 交付

执行：

~~~text
python /skills/distill-business-capability/scripts/distill_capabilities.py summary --result /workspace/outputs/capability-distillation/capability-manifest.json --relations /workspace/outputs/data-relations/scenario-relationship.json --flow /workspace/outputs/business-flow/business-flow.json --output /workspace/outputs/capability-distillation --offset 0 --limit 30
~~~

只有同时满足以下条件才算完成：

- `capability-manifest.json` 为 `complete` 且上游 fingerprint 未变化；
- manifest 明确列出各 Skill 的 Python 依赖和 OCR 等外部服务运行要求；
- `skills/` 中每个目录都有合法 `SKILL.md`，名称与目录一致；
- `skills/` 中每个目录都有至少一个可通过语法校验的 `scripts/*.py`，且 manifest 的 executables 清单一致；
- 阶段 Skill 数等于流程阶段数，并存在一个场景总控 Skill；
- 当前文件格式需要的基础 Skill 全部存在，不需要的基础 Skill 不存在；
- `distillation-report.md`、`capability-map.mmd` 和 `capability-plan.json` 存在；
- `agent_prompts.md` 存在、摘要与 manifest 一致，并清楚规定规则优先、大表 SQL、连接验证和非结构化证据索引的调用顺序；
- `agent_prompts.md` 明确区分运行时输入、设计期结果模板和可选外部增强，禁止因模板原文件缺失而阻塞，并规定必需外部知识不可用时转人工；
- 不存在 `validation-errors.json`；
- 可移植性扫描无平台固定路径或平台 Tool；系统 Skill 配置按 `preserve_system_skill_configuration` 策略完整继承，凭据状态已审计且值未进入 manifest、报告或提示词。

调用 `report_task_progress(action="complete")`，`artifacts` 必须显式列出 manifest、正式计划、能力图、蒸馏报告和 `agent_prompts.md`。最终答复说明 Skill 总数、流程节点覆盖、基础能力及格式、待确认边界、可移植性结果和主要产物路径。完成即停止，不执行最终打包或安装。

## 恢复顺序

1. `prepare-status.json`
2. `distillation-brief.json`
3. `capability-plan.candidate.json` 或 `capability-plan.json`
4. `validation-errors.json`
5. `capability-manifest.json`

已有候选直接预检；已有完整 manifest 只有在 fingerprint 未变化且 `generator_contract_version` 等于当前版本时才可只读摘要。版本缺失或过期时重新 `prepare` 和 `finalize`，不得交付旧源码，也不要求用户手工删除旧产物。

## 最终产物

- `skills/`：待最终打包的可移植多 Skill 源码树。
- `capability-manifest.json`：Skill、依赖、格式、摘要和内容摘要值。
- `capability-plan.json`：通过验收的正式能力计划。
- `capability-map.mmd`：基础能力、阶段能力和场景总控依赖图。
- `distillation-report.md`：节点覆盖、格式覆盖、可移植性与交付边界。
- `agent_prompts.md`：可直接复制为第三方场景 Agent 系统提示词的完整内容。
- `distillation-brief.json`、`prepare-status.json`：有界简报和恢复检查点。
