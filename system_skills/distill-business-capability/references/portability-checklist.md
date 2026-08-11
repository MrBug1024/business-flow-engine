# 第三方 Agent 可移植性清单

生成的 `skills/` 将在后续任务中被独立打包。当前蒸馏必须先满足以下条件。

## Skill 结构

- 每个 Skill 目录包含 `SKILL.md`；frontmatter 只依赖标准 `name` 和 `description`。
- `name` 与目录名一致，使用小写 kebab-case。
- description 同时说明能力和调用时机，不依赖用户记住内部流程 ID。
- 可选 `agents/openai.yaml` 只提供 UI 元数据，不承担业务契约。
- 详细输入输出契约放在 `references/`，每个 Skill 都必须有 `scripts/*.py` 稳定入口。

## 路径和运行时

- 不出现 `/workspace`、`/skills/` 或 Windows 绝对路径。
- 命令使用 `<this-skill>/scripts/...` 表示相对于当前 Skill 的资源。
- 不调用原平台 `report_task_progress`、Tool/MCP 网关或能力发现接口。
- 不依赖原会话、检查点、账户 ID 或平台数据目录。
- Python 版本和第三方依赖写入 manifest 与各 Skill 的 `requirements.txt`。

## 配置、凭据和网络

- 定制系统 Skill 时完整复制公开配置字段和值；不得清空服务地址、知识库 ID、超时或更改 TLS 默认值。API Key 等秘密字段保留字段名但必须为空。
- 平台 Skill 凭据存储或当前环境已经为来源 Skill 配置的凭据，只记录字段名、来源、`configured` 和 `exported=false`，不得物化到第三方 Skill；第三方运行环境通过同名环境变量注入。
- manifest、报告、提示词和命令输出只声明凭据字段名、来源、`configured` 和 `exported` 状态，不回显值。
- 第三方环境可用来源 Skill 已支持的同名环境变量覆盖包内配置。
- 真实 Base64 业务内容、Cookie 和与来源 Skill 无关的账号信息不得进入能力包。
- 网络能力明确声明服务地址、凭据和失败状态，不把网络可用性当作默认事实。

## 业务边界

- 一流程阶段一 Skill，目标和结果与流程产物一致。
- 基础 Skill 只读取文件，不作业务判断。
- 阶段 Skill 不复制相邻阶段责任。
- 场景总控只路由，不成为万能业务执行器。
- 阶段和总控必须通过包内工作单/状态机脚本执行重复控制逻辑，不让 Agent 手写临时脚本。
- 历史数据只作验证或运行时输入，不固化为规范规则。
- 历史结果样例只作 `design_time_template`，其原文件不属于第三方运行时必需输入；模板不得同时承担外部知识或爬虫角色。
- 待确认项可随源码交付，但不能伪装成已实现分支。

## 格式和大数据

- 文件格式来自上游清单，不按常识猜测。
- CSV、Parquet、JSONL 等优先使用 DuckDB 过滤和聚合。
- XLSX 优先使用 DuckDB `read_xlsx`，fastexcel 后备可能物化工作表时必须告警。
- 所有 sample/query/extract 都有硬上限；完整查询结果只落调用方指定的 CSV/Parquet，不进入 Agent 上下文。
- 每个表格基础 Skill 携带经过脱敏的 `operational-data-contract.json`，只保留相对数据路径和上游摘要。
- 规则检索返回完整行；只预检当前操作引用的 `runtime_input`，用 `--bind` 绑定新批次相对路径并校验字段兼容；不得以历史文件名、大小或内容摘要要求新数据完全相同。跨来源查询前检查单键/复合键的空值、未匹配、基数和连接放大，并验证 SQL 使用了同一键组。
- 有界查询和全量导出返回查询摘要；全量导出还返回行数、文件大小和结果文件摘要。
- 文本、Markdown、Word、PDF 先建分块索引再检索，不能先读取全文后仅截断返回。
- PDF 文本层稀疏时转交 OCR；OCR 正文写入 JSON 后建索引，不直接打印到 Agent 上下文。
- `agent_prompts.md` 的摘要写入 manifest，内容声明场景路由、规则优先、数据访问和证据边界。
- 外部知识节点生成完整定制的知识库 Skill；检索和原文定位使用继承的客户端及场景包装 CLI。
- Agent 根据用户请求和完整规则决定是否使用知识库、爬虫或其他外部增强；必需外部知识不可用或无结果时明确转人工，不得猜测或把本地模板冒充外部参考。

## 当前层与最终打包层

当前层交付可移植源码、manifest、摘要值和依赖图。它不负责：

- 语义版本号选择；
- 许可证归集；
- SBOM、漏洞扫描和依赖锁定；
- ZIP/TAR 生成、签名和校验和发布；
- 在第三方环境安装或运行冒烟测试。

这些属于后续 `package-business-skill` 的独立验收责任。
