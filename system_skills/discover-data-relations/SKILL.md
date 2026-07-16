---
name: discover-data-relations
description: >
  从任意业务场景的异构 data 文件中渐进发现可核验关系链路，适用于小型规则/字典/样例与超大事实表混合、文件数量或行数不可预知的情况。先盘点结构并从高信息密度小文件提取有界种子，再定向扫描大表候选列、多跳扩展已命中键，并用有界列摘要补充未连接关系；不让 Agent/LLM 读取原始大文件，也不建立全量值索引。用于数据关系、血缘、跨表连接、引用链、重复文件和证据网络分析；支持 CSV/TSV、XLSX、JSONL、Parquet、SQLite、文本、PDF、DOCX、PPTX 和图片，并可协同 ocr-parser。
---

# 渐进式数据关系发现

让脚本处理原始数据，让 Agent 只读取有界摘要。关系数量很少不代表输入必须很小；工作量和产物大小必须由候选列、种子预算、列摘要预算和实际命中控制。

## 强制边界

- 不得用 `read_file`、`cat`、PowerShell、搜索工具或等价方式直接读取 `/workspace/data` 原文。
- 不得先把所有文件转换成全文、逐值 SQLite 索引或向量切片。
- 不得把日期、金额、普通词语、列名相似或单个短数字直接判定为关系。
- 每条关系必须来自精确值指纹、完整文件哈希或显式文件引用，并带两端定位。
- `supported_hypothesis` 必须明确是待业务确认的猜想；不得写成事实。
- 覆盖未完成时必须报告 `partial` 和未完成表，继续断点执行，不得声称无关系。

## Studio 契约

- Skill 包只读映射到 `/skills/discover-data-relations`；工作区可写映射到 `/workspace`。
- 默认输入 `/workspace/data`，默认输出 `/workspace/outputs/data-relations`。
- 首次使用或依赖变化后执行：

```bash
python -m pip install --disable-pip-version-check -r /skills/discover-data-relations/requirements.txt
```

- PDF/图片通过 `/skills/ocr-parser` 协同。脚本把 OCR 原文重定向到临时文件，原文不进入 Agent 上下文。

## 执行

直接运行，不要先预览数据：

```bash
python /skills/discover-data-relations/scripts/analyze_relations.py analyze \
  --input /workspace/data \
  --output /workspace/outputs/data-relations \
  --goal-file /workspace/description.md \
  --ocr-mode auto \
  --deadline-seconds 780 \
  --summary-limit 20
```

执行阶段：

1. 只读取文件元数据、工作表维度和表头，生成 `catalog.json`。
2. 自动选择小表、小文档和规则/字典/模板/样例类文件作为种子；选择依据通用结构和文件角色，不依赖具体行业字段。
3. 从种子文件的 ID、编号、代码、引用、邮箱、电话、URL、名称等候选列提取有界 bottom-k 值。
4. 按列语义兼容度和规模排序大表，只扫描候选列；只保存命中证据、命中行产生的下一跳键和有界列摘要。
5. 比较兼容列的 bottom-k 摘要；只有出现精确共享指纹才输出补充关系。
6. 每 50,000 行写入 `progress.json`。返回 `partial` 时原命令再次执行会自动续跑。

XLSX 优先使用 Rust Calamine 驱动的 `fastexcel` 按列读取；DuckDB 和 `openpyxl` 自动作为后备路径。不得因高性能引擎暂不可用而退回全量逐值索引。

## 查询结果

禁止直接读取完整 `relations.json`。使用分页摘要：

```bash
python /skills/discover-data-relations/scripts/analyze_relations.py summary \
  --result /workspace/outputs/data-relations/relations.json \
  --offset 0 --limit 20
```

按 ID 核查一条关系或链路：

```bash
python /skills/discover-data-relations/scripts/analyze_relations.py relation R-xxxxxxxxxxxx
python /skills/discover-data-relations/scripts/analyze_relations.py chain C-xxxxxxxxxxxx --offset 0 --limit 50
```

最终回答必须给出执行状态、覆盖表数、未完成表、关系 ID、两端文件与列、判定、置信度和证据摘要，并提供完整报告路径。

## 输出

- `catalog.json`：文件、表、行列规模和候选列，不含数据行。
- `progress.json`：有界种子、列摘要、进度和关系，用于断点续跑。
- `relations.json`：完整关系、链路和覆盖边界。
- `relation-report.md`：逐关系证据报告。
- `relations.mmd`：完整 Mermaid 关系图。
- `evidence.sqlite3`：只保存最终关系和有限证据，不保存全量原始值。

需要解释关系门槛、覆盖承诺或性能策略时读取 [references/evidence-model.md](references/evidence-model.md)。
