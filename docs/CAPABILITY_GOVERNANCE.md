# Agent 能力发现与单一职责规范

## 1. 目标

本规范解决三个长期问题：能力在首次启动和首轮任务中必须真实可见；Agent
应主动选择匹配能力而不是等待用户点名；Tool、Skill、MCP 的每个可调用单元
只承担一项清晰职责。

平台只实现通用发现、路由、调用、隔离、追踪和恢复。具体业务方法仍由独立
Skill 或外部 MCP 提供，不能写入 Studio Agent 主循环。

## 2. 首轮发现链路

1. Tool 注册表在模块导入时进行隔离扫描，保留每个模块的错误。
2. FastAPI lifespan 在应用完成导入后执行一次原子刷新，消除导入期依赖尚未
   就绪造成的短暂缺失。
3. 同一阶段清理并预热系统 Skill 缓存，形成平台能力就绪快照。
4. 如果 Agent 不是从 FastAPI lifespan 进入，首次 `discover_capabilities` 会执行
   相同的兜底初始化。
5. 每次 Agent 运行重新装载当前账户的 Skill 元数据，并从账户设置读取已验证的
   MCP tools 快照。
6. `/api/health` 提供平台级快照；登录后的 `/api/capabilities/readiness` 还包含
   当前账户的用户 Skill 与 MCP 就绪状态。

MCP 不在服务启动时擅自联网重试。启用但没有 `tools_discovered=true` 快照的服务
会显示为 degraded，用户需要在能力设置中重新测试并保存。这样既避免隐式外部
副作用，也不会让未就绪 MCP 静默消失。

## 3. Agent 能力选择

模型每轮会收到三层信息：直接挂载的基础 Tool schema、有界 Skill 元数据、
有界的可选 Tool/MCP 路由索引。完整 Skill 内容与可选 Tool/MCP schema 仍按需读取。

非简单任务开始前必须完成能力选择检查：

- 匹配 Skill 时，先完整读取对应 `SKILL.md`，再按包内边界执行。
- 匹配 Tool/MCP 时，先用 `discover_studio_capabilities` 窄查询并获取 schema，再通过
  `call_tool` 或 `call_mcp` 调用精确名称。
- 需要领域方法、特殊格式处理、当前外部数据或外部系统，但索引不确定时，必须
  先发现能力，不能先手写一个替代实现。
- 没有匹配能力时才使用通用文件系统和工作区能力。
- 简单对话或纯文本回答不需要为了产生调用记录而使用能力。

路由索引有严格数量和描述长度限制。能力很多时，模型通过发现接口继续检索，
避免把完整目录或全部 schema 塞进 System Prompt。

## 4. 单一职责契约

### 4.1 Tool

项目 Tool 在 `tool.metadata.studio.capability` 中声明：

```python
example.metadata = {
    "studio": {
        "capability": {
            "id": "one-stable-capability-id",
            "responsibility": "One concrete operation owned by this Tool.",
            "excludes": ["Adjacent work owned elsewhere"],
        }
    }
}
```

`id` 和 `responsibility` 必须非空，`excludes` 必须为列表。结构错误会阻止 Tool
挂载；旧 Tool 没有声明时仍可被扫描，但就绪状态为 degraded，必须补齐后再作为
系统能力交付。

### 4.2 Skill

系统 Skill 在 `SKILL.md` frontmatter 中声明：

```yaml
metadata:
  capability:
    id: one-stable-capability-id
    responsibility: 一项完整、可验收的业务能力。
    excludes:
      - 明确不负责的相邻能力
```

Skill 可以包含多个内部脚本、参考资料和步骤，但它们必须共同完成同一个可验收
结果。脚本不是独立 Tool。系统或用户 Skill 缺失职责声明时，就绪状态为 degraded；
为了兼容标准 Skill 包，安装流程不会只因缺少扩展字段而破坏包内容。

### 4.3 MCP

MCP Server 可以暴露多个远端 Tool，但 Studio 将每个远端 Tool 视为独立能力，
使用 `server + tool` 形成稳定路由身份。Studio 不把整个 Server 包装成一个万能
调用。远端 Tool 的 description 是其责任说明；schema 是调用事实来源。

## 5. 当前与未来能力边界

| 能力 | 状态 | 唯一责任 | 标准交接产物 |
| --- | --- | --- | --- |
| `discover-data-relations` | 已实现 | 从材料证据推导宏观数据关系 | `outputs/data-relations/scenario-relationship.json` |
| `derive-business-flow` | 待实现为独立 Skill | 基于已验收关系和业务证据推导流程、状态与分支 | `outputs/business-flow/business-flow.json` |
| `distill-business-capability` | 待实现为独立 Skill | 将已验收的场景关系、流程、规则和约束蒸馏为 Skill 源文件 | `outputs/capability-distillation/` |
| `package-business-skill` | 待实现为独立 Skill | 校验 Skill 结构、依赖、契约和可移植性并生成最终包 | `deliverables/skill-package/` |

禁止跨层代办：数据关系 Skill 不推流程；流程 Skill 不生成 Skill；蒸馏 Skill 不
冒充最终打包验收；打包 Skill 不重新推导业务事实。每层只读取上游已验收产物，
发现上游证据不足时返回明确阻塞或交回对应能力修正。

## 6. 新能力验收清单

- 名称、描述、责任和排除项能够让模型区分相邻能力。
- 输入来源、输出目录、结构化交接格式和完成条件明确。
- 不依赖用户必须说出能力名称才能触发。
- 失败结果有稳定状态与修复建议，不把失败伪装成完成。
- 大数据或长任务有有界摘要和磁盘检查点，不靠无限上下文。
- Skill 内部脚本不注册为 Tool；MCP 按远端 Tool 粒度调用。
- 相关发现、选择、契约和首轮可见性测试通过。
