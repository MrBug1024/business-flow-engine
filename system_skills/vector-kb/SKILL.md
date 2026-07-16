---
name: vector-kb
description: "向量知识库检索技能。当用户需要查询知识库、搜索内部文档、检索相关片段、基于知识库回答问题时，必须使用此技能。触发词包括：查知识库、搜索知识库、从知识库找、检索文档、知识库问答等。即使用户没有明确说'知识库'，只要问题需要从内部资料中查找答案，也应触发此技能。"
compatibility: 需要可联网的 Studio Python 运行环境（Python 3.11+），以及由平台注入的 VECTOR_KB_API_KEY。
metadata:
  delivery_mode: explicit
---

# 向量知识库检索技能

本 Skill 是包含说明、依赖、配置和实现资源的完整能力包。激活时先阅读本文件，再通过 Studio 的通用执行能力按以下流程完成检索；`scripts/kb_client.py` 是包内实现资源，不是独立 Tool，也不会被注册或调用为 Tool。

## Studio 执行契约

- 完整 Skill 包通过只读文件系统映射暴露在 `/skills/vector-kb`。
- 当前项目工作区映射到 `/workspace`，可写并在同一业务项目的会话间持久保留。
- 所有命令和依赖安装必须通过 Studio 的通用执行能力完成；其中 `python` 始终指向 Studio 在场景目录外维护的系统级共享 venv，不使用系统全局 Python，也不把包内脚本注册成 Tool。
- 不得向 `/skills/vector-kb` 写入缓存、配置或结果。需要持久输出时写入 `/workspace`。

## 准备依赖

依赖安装到 Studio 系统级共享 venv。该环境由平台统一创建和维护，位于所有业务场景目录之外。首次使用以及 `requirements.txt` 变化后执行：

```bash
python -m pip install --disable-pip-version-check -r /skills/vector-kb/requirements.txt
```

后续命令统一使用 `python`；Studio 负责将其解析到共享 venv，并串行化依赖变更，Skill 不创建或管理自己的 venv。

## 包内默认配置

此 Skill 从 `config/defaults.json` 读取非敏感连接默认值。访问凭据由平台在执行命令时注入沙箱环境，不随 Skill 包分发，也不得写入包目录、工作区文件或命令参数。

| 配置项 | 用途 |
|---|---|
| `base_url` | 知识库服务地址 |
| `library_id` | 默认知识库 ID |
| `api_key` | 保持为空；实际凭据由 `VECTOR_KB_API_KEY` 安全注入 |
| `timeout_seconds` | 请求超时秒数 |

配置优先级为 Studio 执行环境变量、`references/scenario_binding.json` 中显式声明的 `runtime_overrides`、`config/defaults.json` 中的非敏感默认值。若缺少必需配置，客户端会在网络请求前返回 `configuration_required`；不得猜测、回显或从其他服务配置中借用凭据。

---

## 使用流程

### 场景 A：只检索切片（默认）

用于"帮我找找关于 XX 的片段"、"搜索 XX 相关内容"。

```bash
python /skills/vector-kb/scripts/kb_client.py "用户的问题" 5
```

读取 JSON 结果的 `data` 切片列表，格式化后展示标题、相关性分数和正文摘要；引用信息读取 `sources`。

### 场景 B：检索 + 综合回答

用于"根据知识库回答：XX 是什么"等需要总结的问题。

1. 执行脚本获取切片
2. 将切片正文作为上下文，结合用户问题生成回答
3. 回答末尾列出引用来源（标题 + document_id）

---

## 核心接口

**检索**：`POST {base_url}/libraries/{library_id}/query`

```json
{ "query": "问题文本", "limit": 5 }
```

**查看原文位置（可选）**：`GET {base_url}/libraries/{library_id}/documents/{document_id}/source?chunk_id={chunk_id}`

---

## 返回字段说明

| 字段 | 说明 |
|---|---|
| status | `success` / `no_results` / `configuration_required` / `auth_failed` / `provider_unavailable` / `error` |
| message | 不包含凭据或响应正文的状态说明 |
| data | 检索切片列表 |
| sources | 标题、document_id、chunk_id 和相似度组成的引用列表 |

---

## 错误处理

| 错误 | 处理 |
|---|---|
| 配置缺失 | 提示维护包内 defaults，或在场景 binding 中明确覆盖 |
| 连接失败 | 提示检查知识库服务是否可达 |
| HTTP 401 | 提示知识库访问凭据无效 |
| HTTP 404 | 提示知识库 ID 不存在 |
| results 为空 | 提示更换关键词重试 |
