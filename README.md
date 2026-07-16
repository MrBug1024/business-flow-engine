# AI Business Studio

AI Business Studio 是以模型为中心的业务能力工作区。平台代码只提供模型接入、项目存储、流式事件以及 Tool、Skill、MCP 三类能力的发现与运行边界，不写死具体业务流程。

## 能力边界

- **Tool**：自动扫描 `tools/` 中通过 LangChain `@tool` 声明的原子工具。
- **Skill**：自动扫描 `system_skills/<name>/SKILL.md`；激活后将整个目录作为一个能力包使用，内部脚本不会注册成 Tool。
- **MCP**：按配置连接标准 MCP server，与 Tool、Skill 独立注册和追踪。
- **Studio Runtime**：项目自动维护一个场景外的系统级共享 venv，并通过文件系统映射提供可写的 `/workspace` 与只读的 `/skills`。Skill 命令统一在该运行环境执行，不污染系统全局 Python，也不会在场景目录创建虚拟环境。

## 本地运行

1. `pip install -r requirements.txt`
2. 复制 `.env.example` 为 `.env`，配置模型和所需 Skill 凭据。
3. 后端运行 `python run.py`。首次需要执行 Skill 时，Studio 会自动准备系统级 Skill venv，无需额外的外部运行时。
4. 前端在 `frontend/` 下运行 `npm install` 和 `npm run dev`。

架构与产品约束见 [AI Business Studio Development Document](docs/AI_Business_Studio_Development_Document.md)。
