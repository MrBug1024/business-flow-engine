# 业务流逆向工程引擎（Business Flow Reverse-Engineering Engine）

基于业务历史数据，**逆向复刻完整业务流程**，并将业务能力**固化为可复用的技能库（Skills）**，
使其能对新传入的同结构数据，产出同逻辑、同结构的业务结果。

> 核心理念：不是写死「某几列得到某个结果」，而是像一名工程师那样理解
> 「有哪些表、哪些字段、哪些是键、表如何关联、流程如何流转」，从而具备对新数据
> 增删改查与业务计算的通用能力。

---

## 能力概览

1. **结构扫描**：上传业务表后只读「表头 + 1~3 条随机样本」，绝不遍历上万行全量数据。
2. **关联推导**：基于字段名语义、类型兼容、样本值重叠率，推导表间关联并生成**关系图谱**。
3. **流程重建**：以结果表为终点逆向追溯（过滤→关联→规则→聚合→计算），生成**流程图谱**。
4. **技能固化**：把每个流程步骤生成为 `SKILL.md + scripts/run.py`，并提供总执行器。
5. **持续进化**：可手动为业务场景追加「进化技能」，扩展业务能力。

全程以**对话式交互**完成，AI 以**流式（SSE）**返回，**思考过程**与**工具调用**实时可见。

---

## 技术栈

| 层 | 选型 |
|----|------|
| Agent | `deepagents` + `langgraph` + `langchain` |
| LLM | OpenAI 兼容接口（默认 MiniMax，可换本地/代理）；未配置时自动降级为启发式推导 |
| Web | `FastAPI` + `sse-starlette`（流式）|
| 数据 | `pandas` / `openpyxl`；文件型存储 |
| 前端 | 原生 HTML/JS 单页，SSE 流式渲染 |

---

## 快速开始

```bash
conda activate counter_flow_envs

# 1. 配置（可选，不配也能跑启发式模式）
cp .env.example .env      # 填入 OPENAI_API_KEY 等

# 2. 启动
python run.py
# 浏览器打开 http://127.0.0.1:8000
```

---

## 接口设计

> 约定：**与 AI 协作完成任务**走流式接口；**记录/图谱/增删场景**走专用 REST 接口。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 健康检查（含是否启用 LLM）|
| `GET` | `/api/scenarios` | 业务场景列表 |
| `POST` | `/api/scenarios` | 新建业务场景 |
| `GET` | `/api/scenarios/{id}` | 场景详情 |
| `DELETE` | `/api/scenarios/{id}` | 删除场景 |
| `POST` | `/api/scenarios/{id}/uploads` | 上传业务表（多文件）|
| `GET` | `/api/scenarios/{id}/tables` | 表结构元信息 |
| **`POST`** | **`/api/scenarios/{id}/chat`** | **流式对话（SSE）核心接口** |
| `GET` | `/api/scenarios/{id}/messages` | 历史对话记录 |
| `GET` | `/api/scenarios/{id}/relations` | 关系图谱数据 |
| `GET` | `/api/scenarios/{id}/flow` | 流程图谱数据 |
| `GET` | `/api/scenarios/{id}/skills` | 技能库 |
| `POST` | `/api/scenarios/{id}/skills/evolve` | 新增进化技能 |

### SSE 事件协议（`/chat`）

每帧一个 JSON，含 `type` 字段：

| type | 载荷 | 含义 |
|------|------|------|
| `thinking` | `{delta}` | 思考过程增量 |
| `content` | `{delta}` | 正式回答增量 |
| `tool_call` | `{name, args}` | 开始调用工具/技能 |
| `tool_result` | `{name, result}` | 工具返回 |
| `refresh` | `{resource}` | 资源更新（relations/flow/skills），前端据此刷新 |
| `status` | `{status}` | 业务场景状态变更 |
| `error` | `{message}` | 出错 |
| `done` | `{}` | 本轮结束 |

---

## 目录结构

```
app/
  config.py          配置（pydantic-settings）
  models.py          领域模型与 API 契约
  storage.py         文件型持久化层
  table_io.py        表读取（仅表头+样本，绝不整表遍历）
  heuristics.py      启发式推导（关联/流程/技能；LLM 降级路径）
  llm.py             LLM 工厂
  tools.py           Agent 工具集（绑定场景）
  agent.py           deep agent 装配
  streaming.py       SSE 帧 + <think> 标签解析
  chat_service.py    流式对话编排（LLM 路径 + 启发式路径）
  skill_builder.py   技能落盘（SKILL.md + scripts/）
  api/               REST + SSE 路由
  main.py            FastAPI 应用
web/                 前端单页（index.html + app.js）
run.py               开发服务器入口
```

---

## 设计要点

- **不整表读取**：`table_io` 用缓冲计数估算行数、限量扫描抽样，内存恒定。
- **思考可见**：MiniMax-M2 把思考内联在 `<think>...</think>`，`streaming.ThinkParser`
  在 token 级别拆分思考与回答。
- **降级可用**：未配置 LLM 时，`heuristics` 给出确定性推导，交互形态与 LLM 路径一致。
- **可迭代**：技能以 `SKILL.md + scripts/run.py` 落盘，流程参数记录于 `flow_spec.json`，
  确认业务口径后可逐步把骨架细化为精确实现。
```
