# AI Business Studio

AI Business Studio 是以模型为中心的业务能力工作区。平台代码只提供模型接入、项目存储、流式事件以及 Tool、Skill、MCP 三类能力的发现与运行边界，不写死具体业务流程。

## 能力边界

- **Tool**：自动扫描 `tools/` 中通过 LangChain `@tool` 声明的原子工具。
- **Skill**：自动扫描 `system_skills/<name>/SKILL.md`；激活后将整个目录作为一个能力包使用，内部脚本不会注册成 Tool。
- **MCP**：按配置连接标准 MCP server，与 Tool、Skill 独立注册和追踪。
- **Studio Runtime**：项目自动维护一个场景外的系统级共享 venv，并通过文件系统映射提供可写的 `/workspace` 与只读的 `/skills`。Skill 命令统一在该运行环境执行，不污染系统全局 Python，也不会在场景目录创建虚拟环境。

## 当前功能

- 邮箱验证码注册、邮箱密码登录和账户级业务场景隔离。
- 多业务场景资源树及完整的工作区文件增删改查、导入、导出和预览。
- 基于 LangGraph 的流式 AI 任务、语义进展、人工确认、检查点恢复和长上下文续接。
- Tool、完整 Skill 包和 MCP 的发现、配置与按需运行。
- AI 文件写入事件驱动的动态编辑预览。

## 数据目录

- `data/accounts/<账户ID>/<场景ID>/`：只保存账户所属的业务场景、会话、上下文和工作区文件。
- `system/`：保存账户库、用户模型/MCP/Skill 设置、Agent 检查点、沙箱、日志和迁移记录。
- `system_skills/`：源码随附、所有账户共享的只读系统 Skill。

旧版 `data/business_studio/` 会在启动时自动迁移；系统运行文件不得重新写入 `data/`。

## 本地运行

1. `pip install -r requirements.txt`
2. 复制 `.env.example` 为 `.env`，配置模型和所需 Skill 凭据。
3. 后端运行 `python run.py`。首次需要执行 Skill 时，Studio 会自动准备系统级 Skill venv，无需额外的外部运行时。
4. 前端在 `frontend/` 下运行 `npm install` 和 `npm run dev`。

当前实现、架构、配置、数据、API、备份和已知边界见 [AI Business Studio 当前实现文档](docs/AI_Business_Studio_Implementation.md)。全部文档入口见 [docs/README.md](docs/README.md)。早期产品设想保留在 [历史设计文档](docs/AI_Business_Studio_Development_Document.md)，不作为当前实现完成状态的依据。
