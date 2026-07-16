---
name: ocr-parser
description: >
  文件内容解析技能，通过 OCR 服务提取 PDF、图片等文档的文字内容。
  当用户需要解析文件、读取 PDF/图片内容、提取文档文字、进行 OCR 识别时使用本技能。
  支持远程 URL、本地文件路径、Base64 内容三种输入方式，支持批量处理。
  凡涉及"文件解析"、"OCR"、"读取文档"、"提取文字"、"解析 PDF"、"识别图片文字"，均应使用本技能。
compatibility: 需要可联网的 Studio Python 运行环境（Python 3.11+），以及由平台注入的 OCR_API_KEY。
---

# OCR 文件解析技能

本 Skill 是包含说明、依赖、配置和实现资源的完整能力包。激活时先阅读本文件，再通过 Studio 的通用执行能力按以下流程完成 OCR；`scripts/parse.py` 是包内实现资源，不是独立 Tool，也不会被注册或调用为 Tool。

非敏感连接与行为默认值保存在 `config/defaults.json`。访问凭据由平台在执行命令时注入沙箱环境，不随 Skill 包分发，也不得写入包目录、工作区文件或命令参数。

---

## 文件结构

```
ocr-parser/
├── SKILL.md                  # 本文件（技能说明）
├── requirements.txt          # Python 运行依赖
├── config/
│   └── defaults.json         # 随包携带的非敏感默认配置
└── scripts/
    ├── ocr_service.py        # OCR 服务客户端
    └── parse.py              # Skill 内部命令行入口
```

---

## Studio 执行契约

- 完整 Skill 包通过只读文件系统映射暴露在 `/skills/ocr-parser`。
- 当前项目工作区映射到 `/workspace`，可写并在同一业务项目的会话间持久保留。
- 所有命令和依赖安装必须通过 Studio 的通用执行能力完成；其中 `python` 始终指向 Studio 在场景目录外维护的系统级共享 venv，不使用系统全局 Python，也不把包内脚本注册成 Tool。
- 本地输入文件应位于 `/workspace` 下。需要临时中间文件时使用 `/tmp/ocr-parser`；该路径由 Studio 映射到场景目录之外，不要尝试写入 `/skills/ocr-parser`。

## 准备依赖

依赖安装到 Studio 系统级共享 venv。该环境由平台统一创建和维护，位于所有业务场景目录之外。首次使用以及 `requirements.txt` 变化后执行：

```bash
python -m pip install --disable-pip-version-check -r /skills/ocr-parser/requirements.txt
```

后续命令统一使用 `python`；Studio 负责将其解析到共享 venv，并串行化依赖变更，Skill 不创建或管理自己的 venv。

---

## 使用方式

激活本 Skill 并确认依赖可用后，通过 Studio 通用命令运行包内入口。

### 参数说明

| 参数 | 说明 |
|------|------|
| `--url <url>` | 远程文件 URL，多个用英文逗号分隔 |
| `--path <path>` | 本地文件路径，多个用英文逗号分隔 |
| `--b64 <str>` | Base64 编码文件内容（单文件） |
| `--name <filename>` | 文件名（配合 `--b64` 使用，用于类型推断） |
| `--format text\|json` | 输出格式，默认 `text`，`json` 返回结构化结果 |

### 场景 1：解析远程 URL（单个）

```bash
python /skills/ocr-parser/scripts/parse.py --url "https://example.com/report.pdf"
```

### 场景 2：解析远程 URL（批量）

```bash
python /skills/ocr-parser/scripts/parse.py \
  --url "https://example.com/a.pdf,https://example.com/b.png" \
  --format json
```

### 场景 3：解析本地文件路径

```bash
python /skills/ocr-parser/scripts/parse.py --path "/workspace/data/scan.pdf"
```

### 场景 4：AI 提供多个本地路径

```bash
python /skills/ocr-parser/scripts/parse.py \
  --path "/workspace/data/doc1.pdf,/workspace/data/doc2.png" \
  --format json
```

### 场景 5：解析 Base64 内容

```bash
python /skills/ocr-parser/scripts/parse.py \
  --b64 "<base64编码字符串>" \
  --name "report.pdf"
```

### 场景 6：获取结构化 JSON 输出

```bash
python /skills/ocr-parser/scripts/parse.py --path "/workspace/data/report.pdf" --format json
```

JSON 输出结构：

```json
[
  {
    "source": "/workspace/data/report.pdf",
    "status": "success",
    "text": "提取的文本内容..."
  }
]
```

失败时：

```json
[
  {
    "source": "/workspace/data/report.pdf",
    "status": "error",
    "message": "文件不存在: /workspace/data/report.pdf"
  }
]
```

---

## 输入优先级

当多种输入方式同时传入时，按以下顺序取第一个有效值：

```
file_content_bytes  >  file_content_b64  >  file_path  >  file_url
```

---

## image_mode 自动推断

根据文件名扩展名自动选择对应的 OCR 参数组：

- 图片（`jpg/jpeg/png/gif/bmp/tiff/webp`） → `OCR_TABLE_ENABLE_IMAGE` / `OCR_AUTO_ROTATE_IMAGE`
- 其他（`pdf` 等）→ `OCR_TABLE_ENABLE_PDF` / `OCR_AUTO_ROTATE_PDF`

---

## 运行配置

| 配置项 | 默认值 | 说明 |
|----------|--------|------|
| `OCR_BASE_URL` | 读取包内默认配置 | OCR 服务地址 |
| `OCR_API_KEY` | 平台安全注入 | 鉴权凭据，不写入 Skill 包 |
| `OCR_BACKEND` | `hybrid-auto-engine` | 识别引擎 |
| `OCR_LANG_LIST` | `ch` | 识别语言 |
| `OCR_TABLE_ENABLE_PDF` | `true` | PDF 表格识别 |
| `OCR_TABLE_ENABLE_IMAGE` | `false` | 图片表格识别 |
| `OCR_AUTO_ROTATE_PDF` | `false` | PDF 自动旋转 |
| `OCR_AUTO_ROTATE_IMAGE` | `true` | 图片自动旋转 |
| `OCR_VERIFY_SSL` | `true` | SSL 验证；生产环境不得关闭 |
| `OCR_TIMEOUT_SECONDS` | `600` | 超时秒数 |

> 配置优先级为 Studio 执行环境变量、`references/scenario_binding.json` 中显式声明的 `runtime_overrides`、`config/defaults.json` 中的非敏感默认值。缺少服务地址或凭据时在网络请求前返回 `configuration_required`。

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 全部解析成功 |
| `1` | 全部失败或无有效输入 |
| `2` | 部分成功、部分失败 |
