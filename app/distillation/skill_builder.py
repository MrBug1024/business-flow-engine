"""技能落盘（v1.0.31：生成标准 Skill 源 + 独立 MCP 元数据）。

# 核心原则
平台代码（此文件）**零业务术语**。不出现"违规"、"规则"、"就诊ID"、"医保"等任何
业务场景下才有的概念。所有业务语义通过 scenario 元数据传入，生成的文件里可以有。

# 生成产物（发布前源目录）
    {scenario_id}/skills/
        system_prompt.md           ── 给业务子 Agent 的系统提示词
        manifest.json              ── MCP/Agent 平台使用的场景元数据
        mcp.json                   ── MCP 工具清单和能力卡片
        main_skill/
            SKILL.md               ── 主业务 Skill 说明
            domain_knowledge.json  ── 数据字典 + ER + 字段语义
            output_specs.json      ── 产出规格（含 pipeline SQL）
            dispatch_config.json   ── 知识表驱动配置（若无知识表则为空 {}）
            schema.json            ── OpenAI function-calling 格式的工具定义
            scripts/
                skill_executor.py  ── 完全独立的执行脚本（无平台依赖）
        skill_knowledge_search/
            SKILL.md
            scripts/{search,list}_knowledge.py
        skill_query_data/
            SKILL.md
            scripts/query_data.py
        step_N_{name}/
            SKILL.md               ── 节点能力说明
            scripts/
                run.py             ── 节点单步执行器（节点元数据内嵌，不额外污染 Skill 根目录）

# 独立性保证
生成的 skill_executor.py：
  - 只依赖 json / re / logging / pathlib / duckdb / pandas
  - 支持「知识表驱动」和「pipeline SQL」两种模式
  - 可直接命令行运行：python skill_executor.py OUTPUT_ID DATA_DIR OUT_DIR
  - 可被任意 AI Agent 工具调用（参见 schema.json）
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.distillation import transform_builder
from app.core.config import settings
from app.domain.models import DomainKnowledge, FlowStep, KnowledgeSchemaMapping, Scenario, Skill, TableRole
from app.domain.storage import store

_MAIN_SKILL_ID = "main_skill"
_KNOWLEDGE_SKILL_ID = "skill_knowledge_search"
_QUERY_SKILL_ID = "skill_query_data"
_DATA_READER_SKILL_ID = "skill_data_reader"
_NL_RULE_SKILL_ID = "skill_nl_rule_parser"

_RESERVED_DIRS = {"uploads", "outputs", "skills"}
_SKILL_PREFIXES = ("main_", "step_")
_GENERATED_SKILL_DIRS = {
    _MAIN_SKILL_ID,
    _KNOWLEDGE_SKILL_ID,
    _QUERY_SKILL_ID,
    _DATA_READER_SKILL_ID,
    _NL_RULE_SKILL_ID,
    "agents",
    "skill_runtime_setup",
    "utils",
    "scripts",
}

_LEGACY_ROOT_FILES = {
    "SKILL.md",
    "SCENARIO_CONTEXT.md",
    "SUBAGENT_SYSTEM_PROMPT.md",
    "TOOLKIT.md",
    "CAPABILITY.md",
    "CAPABILITY.json",
}


def _slug(name: str) -> str:
    s = re.sub(r"[\s/\\]+", "_", str(name).strip())
    s = re.sub(r"[^0-9A-Za-z_一-鿿]", "", s)
    return s[:60] or "node"


def _skill_meta_name(raw: str) -> str:
    """生成公共 Skill frontmatter 兼容名称：小写字母/数字/连字符。"""
    safe = re.sub(r"[^0-9a-z-]+", "-", str(raw).replace("_", "-").lower()).strip("-")
    if not safe or safe[0].isdigit():
        safe = f"skill-{safe or 'scenario'}"
    return safe[:63].rstrip("-")


def _skill_frontmatter(name: str, description: str) -> str:
    desc = str(description or "").replace("\n", " ").replace('"', "'")
    return f"---\nname: {_skill_meta_name(name)}\ndescription: \"{desc}\"\n---\n"


def _cleanup_legacy_root_files(base: Path) -> None:
    """Remove old mixed Skill/MCP root files from previously generated scenarios."""
    for name in _LEGACY_ROOT_FILES:
        p = base / name
        if p.exists() and p.is_file():
            p.unlink()


# ===========================================================================
# 独立执行脚本模板（完全域无关；域特定值通过 JSON sidecar 注入）
# ===========================================================================
_EXECUTOR_TEMPLATE = r'''"""业务技能执行器（由「零号.奇点工坊」自动生成）。

本脚本完全独立——不依赖任何平台代码，可在任意环境下直接运行。

# 接口
    list_outputs() -> list[dict]
    produce(output_id, data_path, out_dir=None, params=None, max_rows=20000) -> dict

# 运行方式
    python skill_executor.py                             # 查看可用产出
    python skill_executor.py OUT_ID /data ./out          # 执行
    python skill_executor.py OUT_ID /data ./out '"关键词"'         # 文本过滤
    python skill_executor.py OUT_ID /data ./out '{"col":"val"}'   # 精确列过滤

# 数据放置
    data_path 可以是：
    ① 目录路径：目录里放各业务数据文件，文件名（不含后缀）= 表名
    ② dict：{表名: 文件路径} 精确指定
"""

import json
import re
import logging
from pathlib import Path

import duckdb
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("skill_executor")


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def _jload(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _domain():
    return _jload(_ROOT / "domain_knowledge.json")


def _specs():
    return _jload(_ROOT / "output_specs.json").get("outputs", [])


def _dispatch():
    p = _ROOT / "dispatch_config.json"
    return _jload(p) if p.exists() else {}


def list_outputs():
    return [{"output_id": s.get("output_id"), "name": s.get("name"),
             "format": s.get("fmt"), "status": s.get("status")} for s in _specs()]


# ---------------------------------------------------------------------------
# 数据读取（含 pickle 缓存）
# ---------------------------------------------------------------------------
def _read_excel_fast(path, skip):
    """优先用 calamine 引擎读 Excel（几十万行的大表比 openpyxl 快 5 倍以上）。
    未安装 python-calamine 或 pandas 版本不支持时，静默退回默认引擎。"""
    try:
        return pd.read_excel(path, skiprows=skip, engine="calamine")
    except Exception:
        return pd.read_excel(path, skiprows=skip)


def _read_raw(path, header_row=0):
    p = str(path).lower()
    skip = header_row or None
    if p.endswith((".xlsx", ".xls")):
        return _read_excel_fast(path, skip)
    if p.endswith(".json"):
        return pd.json_normalize(json.loads(Path(path).read_text(encoding="utf-8")))
    sep = "\t" if p.endswith(".tsv") else ","
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(path, skiprows=skip, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, skiprows=skip, sep=sep)


def _read_table(path, header_row=0):
    return _read_raw(path, header_row)


def _resolve_files(data_path, required_tables):
    if isinstance(data_path, dict):
        return dict(data_path)
    folder = Path(data_path)
    if not folder.is_dir():
        raise FileNotFoundError(f"数据路径无效：{data_path}")
    by_stem = {f.stem: str(f) for f in folder.iterdir()
               if f.suffix.lower() in (".csv", ".tsv", ".xlsx", ".xls", ".json")}
    return {name: by_stem[name] for name in (required_tables or by_stem) if name in by_stem}


def _header_rows():
    return {t["table_name"]: t.get("header_row", 0) for t in _domain().get("tables", [])}


# ---------------------------------------------------------------------------
# SQL 工具
# ---------------------------------------------------------------------------
def _q(ident):
    return '"' + str(ident).replace('"', '""') + '"'


def _lit(v):
    return "'" + str(v).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# 参数过滤（通用：支持关键词 / 精确列值）
# ---------------------------------------------------------------------------
_SPLIT_RE = re.compile(r"[、，,；;。:：_\-\s（）()\\/\"'“”‘’]+")


def _tokenize(s, strip_prefixes=()):
    s = str(s).strip()
    if not s:
        return []
    parts = [p.strip() for p in _SPLIT_RE.split(s) if p.strip()]
    tokens = []
    for p in parts:
        for pfx in strip_prefixes:
            if p.startswith(pfx) and len(p) > len(pfx) + 1:
                p = p[len(pfx):]
                break
        if 2 <= len(p) <= 14 and p not in tokens:
            tokens.append(p)
    return tokens[:12]


def _apply_params_filter(df, params):
    """通用行过滤：params 可以是 None / 字符串关键词 / dict。"""
    if params is None:
        return df
    if isinstance(params, str):
        params = {"keyword": params}
    if not isinstance(params, dict):
        return df

    strip_pfx = tuple(_dispatch().get("filter_strip_prefixes", []))

    if "keyword" in params:
        kw = str(params["keyword"]).strip()
        if kw:
            text_cols = [c for c in df.columns if df[c].dtype == object]
            corpus = (df[text_cols].fillna("").astype(str).agg(" ".join, axis=1)
                      if text_cols else pd.Series([""] * len(df), index=df.index))
            mask = corpus.str.contains(kw, na=False, regex=False)
            if mask.sum() > 0:
                df = df[mask]
            else:
                tokens = _tokenize(kw, strip_pfx)
                if tokens:
                    scores = pd.Series(0, index=df.index)
                    for t in tokens:
                        scores += corpus.str.contains(t, na=False, regex=False).astype(int)
                    need = max(1, (len(tokens) + 1) // 2)
                    hit_mask = scores >= need
                    if hit_mask.sum() == 0:
                        hit_mask = scores >= 1
                    if hit_mask.sum() > 0:
                        df = df.loc[scores[hit_mask].sort_values(ascending=False).index].head(5)
                    else:
                        df = df.iloc[0:0]

    for col, val in params.items():
        if col == "keyword":
            continue
        if col not in df.columns:
            continue
        if isinstance(val, (list, tuple, set)):
            wanted = {str(v).strip() for v in val}
            df = df[df[col].astype(str).str.strip().isin(wanted)]
        else:
            df = df[df[col].astype(str).str.strip() == str(val).strip()]
    return df


# ---------------------------------------------------------------------------
# 输出写文件
# ---------------------------------------------------------------------------
def _render(df, fmt, out_dir, base_name):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "".join(c if (c.isalnum() or ord(c) > 0x2E80 or c in "._-") else "_"
                   for c in str(base_name))[:80] or "output"
    fmt = (fmt or "csv").lower()
    try:
        if fmt in ("xlsx", "xls"):
            p = out_dir / (stem + ".xlsx")
            df.to_excel(p, index=False)
            return p
        if fmt == "tsv":
            p = out_dir / (stem + ".tsv")
            df.to_csv(p, index=False, sep="\t", encoding="utf-8-sig")
            return p
        if fmt == "json":
            p = out_dir / (stem + ".json")
            df.to_json(p, orient="records", force_ascii=False, indent=2)
            return p
        if fmt in ("md", "markdown"):
            p = out_dir / (stem + ".md")
            try:
                body = df.to_markdown(index=False)
            except Exception:
                cols = list(df.columns)
                head = "| " + " | ".join(cols) + " |"
                sep = "| " + " | ".join("---" for _ in cols) + " |"
                body = "\n".join([head, sep] + [
                    "| " + " | ".join("" if pd.isna(r[c]) else str(r[c])
                                      for c in df.columns) + " |"
                    for _, r in df.head(1000).iterrows()])
            p.write_text(f"# {stem}\n\n共 {len(df)} 行。\n\n{body}\n", encoding="utf-8")
            return p
    except Exception:
        pass
    p = out_dir / (stem + ".csv")
    df.to_csv(p, index=False, encoding="utf-8-sig")
    return p


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def produce(output_id, data_path, out_dir=None, params=None, max_rows=20000):
    """执行产出（自动选择模式）。

    params 格式：
        None                   全部
        "关键词"               文本列模糊匹配
        {"col": "val"}         精确列过滤
        {"keyword": "xxx"}     同字符串
    """
    spec = next((s for s in _specs() if s.get("output_id") == output_id), None)
    if spec is None:
        raise ValueError(f"未知产出：{output_id}。可用：{[s.get('output_id') for s in _specs()]}")

    dispatch = _dispatch()
    knowledge_table = dispatch.get("knowledge_table", "")

    if knowledge_table:
        return _knowledge_engine(output_id, spec, dispatch, data_path, out_dir, params, max_rows)
    return _pipeline(output_id, spec, data_path, out_dir, params, max_rows)


def _pipeline(output_id, spec, data_path, out_dir, params, max_rows):
    """Pipeline SQL 模式：直接执行流程节点 SQL。"""
    pipeline = spec.get("pipeline") or []
    sql = spec.get("sql", "").strip()

    if pipeline:
        parts, last_view = [], ""
        for step in pipeline:
            ssql = (step.get("sql") or "").strip().rstrip(";")
            view = step.get("output") or f"_step{step.get('step_id', len(parts))}_out"
            if not ssql:
                continue
            if ssql.lstrip().upper().startswith("CREATE"):
                parts.append(ssql + ";")
            else:
                parts.append(f'CREATE OR REPLACE TEMP VIEW "{view}" AS\n{ssql};')
            last_view = view
        if parts and last_view:
            parts.append(f'SELECT * FROM "{last_view}"')
        if parts:
            sql = "\n\n".join(parts)

    if not sql:
        raise RuntimeError(f"产出「{spec.get('name', output_id)}」无可执行 SQL。")

    required = spec.get("required_tables") or []
    files = _resolve_files(data_path, required or None)
    header_rows = _header_rows()
    frames = {n: _read_table(files[n], header_rows.get(n, 0)) for n in files}

    con = duckdb.connect()
    try:
        for n, df in frames.items():
            con.register(n, df)
        stmts = [s.strip() for s in sql.split(";") if s.strip()]
        result = pd.DataFrame()
        for stmt in stmts:
            result = con.execute(stmt).fetchdf()
    finally:
        con.close()

    cols = spec.get("columns") or []
    if cols and not result.empty:
        result = result.reindex(columns=cols)

    artifact = ""
    if out_dir:
        artifact = str(_render(result, spec.get("fmt", "csv"), out_dir, spec.get("name", output_id)))
    return {"rows": int(len(result)), "artifact": artifact,
            "columns": list(result.columns)[:60], "mode": "pipeline"}


def _knowledge_engine(output_id, spec, dispatch, data_path, out_dir, params, max_rows):
    """知识表驱动模式：知识表里每条规则的判断逻辑千差万别（自然语言描述、阈值、
    共现关系、公式……组合各异），真实场景里可能有成百上千条规则，不可能在蒸馏阶段
    为每一条都预先写死一条 SQL 去猜——写得完也穷举不完，新规则一来照样失灵。

    因此本函数只负责通用、场景无关的一步：按 params 过滤出待处理的知识行原文。
    "这条规则该怎么去业务表里查"这件事本身，交还给调用方（有推理能力的 LLM）：
    读到这里返回的知识行原文 + field_role_map（知识字段 → 业务表字段对应关系）+
    真实业务表 schema 后，自己构造并执行查询（例如调用 query_data 工具逐条规则查）。
    """
    knowledge_table = dispatch.get("knowledge_table", "")
    dispatch_key_col = dispatch.get("dispatch_key_column", "")
    nl_columns = dispatch.get("nl_columns", [])
    field_role_map = dispatch.get("field_role_map", {})

    required = spec.get("required_tables") or []
    files = _resolve_files(data_path, required or None)
    if knowledge_table not in files:
        raise FileNotFoundError(
            f"缺少知识表「{knowledge_table}」文件。已有：{list(files.keys())}")

    header_rows = _header_rows()
    knowledge_df = _read_table(files[knowledge_table], header_rows.get(knowledge_table, 0))
    knowledge_sub = _apply_params_filter(knowledge_df, params)
    log.info("params 过滤：%d / %d 条知识行", len(knowledge_sub), len(knowledge_df))
    if len(knowledge_sub) == 0:
        raise RuntimeError(f"params={params!r} 未匹配到任何知识行。")

    knowledge_sub = knowledge_sub.head(max_rows)
    matched_rows = json.loads(knowledge_sub.to_json(orient="records", force_ascii=False))

    guidance = (
        f"以上是知识表「{knowledge_table}」中命中的 {len(matched_rows)} 条规则/知识行原文，"
        "本执行器不会替你猜每条规则该怎么判断——真实业务规则的判断逻辑千差万别，不可能"
        "提前为每一条写死 SQL。请逐条阅读规则内容，结合 field_role_map（知识字段 → "
        f"业务表字段的对应关系：{field_role_map}）与业务表的真实 schema，自行构造针对"
        "该规则的查询（例如调用 query_data 工具执行 SQL），得到该规则命中的业务数据行。"
    )
    log.info("已筛出 %d 条知识行，等待调用方（LLM）逐条构造查询。", len(matched_rows))

    return {
        "mode": "knowledge_rows",
        "knowledge_table": knowledge_table,
        "dispatch_key_column": dispatch_key_col,
        "nl_columns": nl_columns,
        "field_role_map": field_role_map,
        "matched_rows": matched_rows,
        "matched_count": len(matched_rows),
        "guidance": guidance,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:
        print("可用产出：")
        for o in list_outputs():
            print(f"  {o['output_id']}  {o['name']}  [{o['format']}]  {o['status']}")
    elif len(sys.argv) >= 3:
        p = None
        if len(sys.argv) >= 5:
            try:
                p = json.loads(sys.argv[4])
            except Exception:
                p = sys.argv[4]
        r = produce(sys.argv[1], sys.argv[2],
                    sys.argv[3] if len(sys.argv) > 3 else None, params=p)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("用法：python skill_executor.py [OUTPUT_ID DATA_DIR [OUT_DIR [PARAMS_JSON]]]")
'''


# ===========================================================================
# 节点子技能模板
# ===========================================================================
_NODE_RUNNER_TEMPLATE = '''"""节点子技能执行器（自动生成）。

接口：run(tables: dict, out_dir=None) -> dict
    tables: {表名: DataFrame 或 文件路径}
"""

import json
from pathlib import Path

import duckdb
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_NODE = json.loads(__NODE_JSON_LITERAL__)


def _to_df(v):
    if isinstance(v, pd.DataFrame):
        return v
    p = str(v).lower()
    if p.endswith((".xlsx", ".xls")):
        return pd.read_excel(v)
    if p.endswith(".json"):
        return pd.json_normalize(json.loads(Path(v).read_text(encoding="utf-8")))
    sep = "\\t" if p.endswith(".tsv") else ","
    return pd.read_csv(v, sep=sep, encoding="utf-8-sig")


def run(tables: dict, out_dir=None) -> dict:
    sql = _NODE.get("sql") or ""
    if not sql:
        raise RuntimeError("本节点无可执行 SQL：" + str(_NODE.get("external_data_needed") or []))
    con = duckdb.connect()
    try:
        for name, v in tables.items():
            con.register(name, _to_df(v))
        df = con.execute(sql).fetchdf()
    finally:
        con.close()
    out_cols = _NODE.get("output_columns") or []
    if out_cols:
        df = df.reindex(columns=out_cols)
    return {"rows": int(len(df)), "columns": list(df.columns), "dataframe": df}


if __name__ == "__main__":
    print("节点：", _NODE.get("step_name"))
    print("能力：", _NODE.get("capability"))
    print("SQL：\\n", _NODE.get("sql"))
'''


def _render_node_runner(step: FlowStep) -> str:
    return _NODE_RUNNER_TEMPLATE.replace(
        "__NODE_JSON_LITERAL__",
        repr(step.model_dump_json(indent=2)),
    )


# ===========================================================================
# 工具脚本模板（独立、无平台依赖）
# ===========================================================================
_SEARCH_KNOWLEDGE_TEMPLATE = r'''"""知识表搜索工具（自动生成）。

完全独立，不依赖平台代码。

接口：
    search(keyword="", filters=None, limit=20) -> list[dict]
    main() 命令行入口

用法：
    python search_knowledge.py "关键词"
    python search_knowledge.py "关键词" 20
    python search_knowledge.py "关键词" 20 /path/to/data
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def _jload(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _read_table(path):
    p = str(path).lower()
    if p.endswith((".xlsx", ".xls")):
        import pandas as pd
        return pd.read_excel(path)
    if p.endswith(".json"):
        import json as _j, pandas as pd
        return pd.json_normalize(_j.loads(Path(path).read_text(encoding="utf-8")))
    import pandas as pd
    sep = "\t" if p.endswith(".tsv") else ","
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(path, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, sep=sep)


def _dispatch():
    p = _ROOT / "main_skill" / "dispatch_config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _get_knowledge_file(data_dir=None):
    """定位知识表文件：优先在 data_dir 里按表名找同名文件（新数据场景），
    找不到再退回 domain_knowledge.json 记录的路径（可能是相对/展示用路径，不一定存在）。
    """
    dispatch = _dispatch()
    knowledge_table = dispatch.get("knowledge_table", "")
    if not knowledge_table:
        return "", None

    if data_dir:
        folder = Path(data_dir)
        if folder.is_dir():
            by_stem = {f.stem: f for f in folder.iterdir()
                       if f.suffix.lower() in (".csv", ".tsv", ".xlsx", ".xls", ".json")}
            kn_file = by_stem.get(knowledge_table)
            if kn_file:
                return knowledge_table, kn_file

    domain_path = _ROOT / "main_skill" / "domain_knowledge.json"
    if domain_path.exists():
        domain = _jload(domain_path)
        kn_meta = next((t for t in domain.get("tables", [])
                        if t.get("table_name") == knowledge_table), None)
        if kn_meta and kn_meta.get("file") and Path(kn_meta["file"]).exists():
            return knowledge_table, Path(kn_meta["file"])
    return knowledge_table, None


def search(keyword="", filters=None, limit=20, data_dir=None):
    """在知识表中搜索条目。

    参数：
        keyword   关键词（空=全量）
        filters   dict {列名: 值} 精确过滤（叠加在 keyword 之上）
        limit     返回条数上限（默认 20）
        data_dir  数据目录（默认从 dispatch_config 的知识表路径推断）
    返回：匹配的行列表（dict 格式）
    """
    knowledge_table, kn_file = _get_knowledge_file(data_dir)
    if not knowledge_table:
        return []

    if not kn_file or not Path(kn_file).exists():
        raise FileNotFoundError(f"知识表文件未找到：{knowledge_table}（请通过 data_dir 指定数据目录）")

    df = _read_table(kn_file)

    if keyword:
        kw = str(keyword).strip()
        corpus = df.fillna("").astype(str).agg(" ".join, axis=1)
        mask = corpus.str.contains(kw, case=False, na=False, regex=False)
        df = df[mask]

    if filters and isinstance(filters, dict):
        for col, val in filters.items():
            if col in df.columns:
                df = df[df[col].astype(str).str.strip() == str(val).strip()]

    return df.head(int(limit)).to_dict(orient="records")


def main():
    kw = sys.argv[1] if len(sys.argv) > 1 else ""
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    data_dir = sys.argv[3] if len(sys.argv) > 3 else None
    rows = search(keyword=kw, limit=lim, data_dir=data_dir)
    print(f"找到 {len(rows)} 条：")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''


_LIST_KNOWLEDGE_TEMPLATE = r'''"""知识表浏览工具（自动生成）。

完全独立，不依赖平台代码。

接口：
    list_all(limit=50) -> list[dict]
    get_columns() -> list[str]
    main() 命令行入口

用法：
    python list_knowledge.py
    python list_knowledge.py 100
    python list_knowledge.py 100 /path/to/data
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def _jload(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _read_table(path):
    p = str(path).lower()
    if p.endswith((".xlsx", ".xls")):
        import pandas as pd
        return pd.read_excel(path)
    if p.endswith(".json"):
        import json as _j, pandas as pd
        return pd.json_normalize(_j.loads(Path(path).read_text(encoding="utf-8")))
    import pandas as pd
    sep = "\t" if p.endswith(".tsv") else ","
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(path, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, sep=sep)


def _get_knowledge_file(data_dir=None):
    """定位知识表文件：优先在 data_dir 里按表名找同名文件（新数据场景），
    找不到再退回 domain_knowledge.json 记录的路径（可能是相对/展示用路径，不一定存在）。
    """
    dispatch = _jload(_ROOT / "main_skill" / "dispatch_config.json") \
        if (_ROOT / "main_skill" / "dispatch_config.json").exists() else {}
    knowledge_table = dispatch.get("knowledge_table", "")
    if not knowledge_table:
        return "", None

    if data_dir:
        folder = Path(data_dir)
        if folder.is_dir():
            by_stem = {f.stem: f for f in folder.iterdir()
                       if f.suffix.lower() in (".csv", ".tsv", ".xlsx", ".xls", ".json")}
            kn_file = by_stem.get(knowledge_table)
            if kn_file:
                return knowledge_table, kn_file

    domain_path = _ROOT / "main_skill" / "domain_knowledge.json"
    if domain_path.exists():
        domain = _jload(domain_path)
        kn_meta = next((t for t in domain.get("tables", [])
                        if t.get("table_name") == knowledge_table), None)
        if kn_meta and kn_meta.get("file") and Path(kn_meta["file"]).exists():
            return knowledge_table, Path(kn_meta["file"])
    return knowledge_table, None


def get_columns(data_dir=None):
    """返回知识表的字段列表。"""
    _, kn_file = _get_knowledge_file(data_dir)
    if not kn_file or not kn_file.exists():
        return []
    df = _read_table(kn_file)
    return list(df.columns)


def list_all(limit=50, data_dir=None):
    """列出知识表所有条目。

    参数：
        limit     返回条数上限（默认 50）
        data_dir  数据目录（覆盖 domain_knowledge.json 中的路径）
    返回：条目列表（dict 格式）
    """
    knowledge_table, kn_file = _get_knowledge_file(data_dir)
    if not knowledge_table:
        return []

    if not kn_file or not Path(kn_file).exists():
        raise FileNotFoundError(f"知识表文件未找到：{knowledge_table}（请通过 data_dir 指定数据目录）")

    df = _read_table(kn_file)
    return df.head(int(limit)).to_dict(orient="records")


def main():
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    data_dir = sys.argv[2] if len(sys.argv) > 2 else None
    rows = list_all(limit=lim, data_dir=data_dir)
    cols = get_columns(data_dir=data_dir)
    print(f"知识表字段（{len(cols)} 列）：{cols}")
    print(f"前 {len(rows)} 条：")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''


_QUERY_DATA_TEMPLATE = r'''"""业务数据即席查询工具（自动生成）。

完全独立，不依赖平台代码。给 Codex / Claude 等 Skill 宿主使用：
读取本能力包的 domain_knowledge.json，按 SQL 实际引用到的表加载业务数据，
用 DuckDB 执行查询，并把结果落盘为 CSV。

用法：
    python skill_query_data/scripts/query_data.py --data-dir /path/to/data --sql 'SELECT * FROM "表名" LIMIT 10'
    python skill_query_data/scripts/query_data.py --data-dir /path/to/data --sql-file query.sql --out-dir ./outputs
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import duckdb
import pandas as pd

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_ROOT = _SKILL_ROOT.parent if (_SKILL_ROOT / "SKILL.md").exists() and not (_SKILL_ROOT / "main_skill").exists() else _SKILL_ROOT
_SUPPORTED = (".csv", ".tsv", ".xlsx", ".xls", ".json")


def _jload(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _domain():
    return _jload(_ROOT / "main_skill" / "domain_knowledge.json")


def _read_excel_fast(path, skip):
    try:
        return pd.read_excel(path, skiprows=skip, engine="calamine")
    except Exception:
        return pd.read_excel(path, skiprows=skip)


def _read_raw(path, header_row=0):
    p = str(path).lower()
    skip = header_row or None
    if p.endswith((".xlsx", ".xls")):
        return _read_excel_fast(path, skip)
    if p.endswith(".json"):
        return pd.json_normalize(json.loads(Path(path).read_text(encoding="utf-8")))
    sep = "\t" if p.endswith(".tsv") else ","
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(path, skiprows=skip, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, skiprows=skip, sep=sep)


def _read_table(path, header_row=0):
    return _read_raw(path, header_row)


def _find_table_file(data_dir, table_name):
    folder = Path(data_dir)
    if not folder.is_dir():
        raise FileNotFoundError(f"数据目录不存在：{data_dir}")
    for suffix in _SUPPORTED:
        p = folder / f"{table_name}{suffix}"
        if p.exists():
            return p
    matches = [p for p in folder.iterdir() if p.is_file() and p.stem == table_name and p.suffix.lower() in _SUPPORTED]
    return matches[0] if matches else None


def _preview(df, rows=20, width=2400):
    try:
        return df.head(rows).to_markdown(index=False)[:width]
    except Exception:
        return df.head(rows).to_string(index=False)[:width]


def query(sql, data_dir=None, out_dir=None):
    sql = (sql or "").strip()
    if not sql:
        raise ValueError("sql 不能为空")

    data_dir = data_dir or os.getenv("BFE_DATA_DIR") or str(_ROOT / "data")
    out_dir = Path(out_dir or os.getenv("BFE_OUT_DIR") or (_ROOT / "outputs"))
    out_dir.mkdir(parents=True, exist_ok=True)

    domain = _domain()
    tables = domain.get("tables", [])
    table_names = [t.get("table_name", "") for t in tables if t.get("table_name")]
    referenced = [name for name in table_names if name and name in sql]
    if not referenced:
        raise ValueError("SQL 中没有识别到本场景表名。可用表：" + "、".join(table_names))

    header_rows = {t.get("table_name"): t.get("header_row", 0) for t in tables}
    loaded = []
    start = time.perf_counter()
    con = duckdb.connect()
    try:
        for name in referenced:
            path = _find_table_file(data_dir, name)
            if path is None:
                raise FileNotFoundError(f"找不到表「{name}」的数据文件。请把文件命名为「{name}.xlsx/csv/json」")
            t0 = time.perf_counter()
            df = _read_table(path, header_rows.get(name, 0))
            con.register(name, df)
            loaded.append({"table": name, "rows": int(len(df)), "seconds": round(time.perf_counter() - t0, 2)})
        result = con.execute(sql).fetchdf()
    finally:
        con.close()

    artifact = out_dir / f"query_{int(time.time())}.csv"
    result.to_csv(artifact, index=False, encoding="utf-8-sig")
    return {
        "rows": int(len(result)),
        "columns": list(result.columns),
        "artifact": str(artifact),
        "loaded": loaded,
        "seconds": round(time.perf_counter() - start, 2),
        "preview": _preview(result),
    }


def main():
    parser = argparse.ArgumentParser(description="对业务场景数据执行 DuckDB SQL")
    parser.add_argument("--data-dir", default=None, help="业务数据目录；文件名不含后缀须等于表名")
    parser.add_argument("--out-dir", default=None, help="查询结果输出目录")
    parser.add_argument("--sql", default="", help="要执行的 SQL")
    parser.add_argument("--sql-file", default="", help="读取 SQL 的文件路径")
    args = parser.parse_args()

    sql = args.sql
    if args.sql_file:
        sql = Path(args.sql_file).read_text(encoding="utf-8")
    result = query(sql, data_dir=args.data_dir, out_dir=args.out_dir)
    print(f"查询完成：{result['rows']} 行，耗时 {result['seconds']}s")
    print("已加载表：" + "；".join(f"{x['table']}({x['rows']}行,{x['seconds']}s)" for x in result["loaded"]))
    print("结果文件：" + result["artifact"])
    print("预览：")
    print(result["preview"])


if __name__ == "__main__":
    main()
'''


# ===========================================================================
# 独立运行环境清单（脱离平台后，目标机器只需按此安装依赖，无需数据库服务）
# ===========================================================================
_REQUIREMENTS_TXT = """# 本技能包的运行依赖（脱离「零号.奇点工坊」后独立安装）
# 安装：pip install -r requirements.txt
#
# 说明：duckdb 是嵌入式 SQL 引擎（类似 sqlite），以 Python 库形式运行在
# 本进程内，不需要另外部署/连接任何数据库服务器；skill_executor.py 里的
# `duckdb.connect()` 打开的是一个进程内临时数据库，数据来自你指定目录下的
# 业务文件（读进 pandas DataFrame 后 `con.register()` 注册为可查询的表），
# 执行完即释放，不产生任何需要维护的持久化数据库文件。
pandas>=2.2.0
duckdb>=1.1.0
openpyxl>=3.1.0
tabulate>=0.9.0
# 可选但强烈建议：大 Excel 表（几十万行）加载提速 5 倍以上；未安装时自动退回 openpyxl
python-calamine>=0.7.0
"""


# ===========================================================================
# 基础技能模板
# ===========================================================================

_DATA_READER_TEMPLATE = r'''"""数据读取技能（自动生成）。

完全独立，不依赖平台代码。读取任意格式的业务数据文件并返回 DataFrame。

# 接口
    read(data_path, table_name=None) -> dict[str, pd.DataFrame]
    validate_schema(frames, required_tables, strict=False) -> dict

# 用法
    python skill_data_reader.py /path/to/data
    python skill_data_reader.py /path/to/data 业务明细表
"""

import json
import sys
from pathlib import Path

import pandas as pd

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_ROOT = _SKILL_ROOT.parent if (_SKILL_ROOT / "SKILL.md").exists() and not (_SKILL_ROOT / "main_skill").exists() else _SKILL_ROOT
_SUPPORTED = (".csv", ".tsv", ".xlsx", ".xls", ".json")


def _jload(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _read_one(path, header_row=0):
    p = str(path).lower()
    skip = header_row or None
    if p.endswith((".xlsx", ".xls")):
        return pd.read_excel(path, skiprows=skip)
    if p.endswith(".json"):
        return pd.json_normalize(json.loads(Path(path).read_text(encoding="utf-8")))
    sep = "\t" if p.endswith(".tsv") else ","
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(path, skiprows=skip, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, skiprows=skip, sep=sep)


def _header_rows():
    domain_file = _ROOT / "main_skill" / "domain_knowledge.json"
    if domain_file.exists():
        d = _jload(domain_file)
        return {t["table_name"]: t.get("header_row", 0) for t in d.get("tables", [])}
    return {}


def read(data_path, table_name=None):
    """读取数据目录中的所有（或指定）业务数据文件。

    Args:
        data_path:  目录路径（文件名=表名）或 {表名: 文件路径} dict
        table_name: 仅读取该表（None=全部）

    Returns:
        {表名: DataFrame}
    """
    if isinstance(data_path, dict):
        sources = dict(data_path)
    else:
        folder = Path(data_path)
        if not folder.is_dir():
            raise FileNotFoundError(f"数据路径无效：{data_path}")
        sources = {f.stem: str(f) for f in folder.iterdir() if f.suffix.lower() in _SUPPORTED}

    if table_name:
        sources = {k: v for k, v in sources.items() if k == table_name}

    hr = _header_rows()
    frames = {}
    for name, path in sources.items():
        try:
            frames[name] = _read_one(path, hr.get(name, 0))
        except Exception as e:
            print(f"⚠️ 读取「{name}」失败：{e}", file=sys.stderr)
    return frames


def validate_schema(frames, required_tables=None, strict=False):
    """校验读入的 DataFrame 是否包含必要的表和字段。

    Args:
        frames:          {表名: DataFrame}
        required_tables: [表名] 或 {表名: [必要字段名]} 或 None（从 domain_knowledge.json 自动读取）
        strict:          True=缺字段则报错，False=只警告

    Returns:
        {"ok": bool, "missing_tables": [...], "missing_columns": {表名: [字段名]}, "warnings": [...]}
    """
    # 从 domain_knowledge.json 读取必要字段
    if required_tables is None:
        domain_file = _ROOT / "main_skill" / "domain_knowledge.json"
        if domain_file.exists():
            d = _jload(domain_file)
            required_tables = {
                t["table_name"]: [c["name"] for c in t.get("columns", [])]
                for t in d.get("tables", [])
            }
        else:
            required_tables = {}

    if isinstance(required_tables, list):
        required_tables = {t: [] for t in required_tables}

    missing_tables, missing_cols, warnings = [], {}, []
    for tbl, cols in required_tables.items():
        if tbl not in frames:
            missing_tables.append(tbl)
            continue
        df = frames[tbl]
        missing = [c for c in cols if c not in df.columns]
        if missing:
            missing_cols[tbl] = missing
            warnings.append(f"表「{tbl}」缺少字段：{missing}")

    ok = not missing_tables and not missing_cols
    return {"ok": ok, "missing_tables": missing_tables, "missing_columns": missing_cols,
            "warnings": warnings}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    name = sys.argv[2] if len(sys.argv) > 2 else None
    frames = read(path, name)
    print(f"已读取 {len(frames)} 张表：")
    for n, df in frames.items():
        print(f"  {n}: {len(df)} 行 × {len(df.columns)} 列  {list(df.columns[:8])}")
    result = validate_schema(frames)
    if result["ok"]:
        print("✅ 数据结构校验通过")
    else:
        print("⚠️ 数据结构校验警告：")
        for w in result["warnings"]:
            print(f"  {w}")
'''


_NL_RULE_PARSER_TEMPLATE = r'''"""NL 规则解析技能（自动生成）。

完全独立，不依赖平台代码。
在执行时对知识表中的自然语言规则文本做运行时增强解析：
识别文本信号、阈值和实体，辅助宿主 Agent 理解知识行原文。
它不替代真实业务判断逻辑，也不驱动 skill_executor.py 选择固定 SQL 模板。

# 接口
    parse_rule(text, dispatch_type=None) -> dict
    classify_batch(rows, nl_columns, dispatch_col=None) -> list[dict]

# 用法
    python skill_nl_rule_parser.py "不得同时申报A和B"
    python skill_nl_rule_parser.py knowledge_file.csv nl_col_name dispatch_col
"""

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 模式信号（与蒸馏阶段 nl_rule_analyzer.py 保持一致）
# ---------------------------------------------------------------------------
_PATTERN_SIGNALS = {
    "co_occurrence": [
        r"同时.*申报", r"同时.*开具", r"同时.*使用", r"同时.*出现",
        r"不得同时", r"互斥", r"冲突", r"同一.*同时",
    ],
    "frequency_overflow": [
        r"\d+次[/／每]", r"每[月年日周]\w{0,4}次", r"次数.*超过", r"超过.*次",
        r"不超过.*次", r"频次", r"频率", r"限.*次", r"\d+次以[内上下]",
    ],
    "threshold": [
        r"超过.*[元分角万千百]", r"不超过.*[元分角万千百]",
        r"限额", r"上限", r"下限", r"金额.*超", r"费用.*超",
        r">\s*\d+", r">=\s*\d+", r"≥\s*\d+",
    ],
    "exclusive_conflict": [
        r"禁止", r"不得(?!同时)", r"不能.*同时", r"排斥", r"禁用",
        r"不允许", r"不应.*同时",
    ],
    "dedup": [
        r"重复", r"不得重复", r"唯一", r"同一.*不得.*多次", r"仅限.*一次",
        r"不重复", r"去重",
    ],
    "keyword": [],
}

_SPLIT_RE = re.compile(r"[，、,；;。:：！\s]+|或|和|与|及|以及")
_NUM_RE = re.compile(r"\d+\.?\d*")


def _detect_pattern(text):
    """识别文本最明显的信号类型，返回 (pattern, signals, confidence)。"""
    if not text:
        return "keyword", [], 0.0
    for pattern, signals in _PATTERN_SIGNALS.items():
        if not signals:
            continue
        matched = []
        for sig in signals:
            if re.search(sig, str(text)):
                matched.append(sig)
        if matched:
            conf = min(1.0, 0.5 + 0.1 * len(matched))
            return pattern, matched, conf
    return "keyword", [], 0.3


def _extract_threshold(text):
    """从文本提取数值阈值（如 '每月不超过3次' → {'value': 3, 'unit': '次', 'op': '<='}）。"""
    nums = _NUM_RE.findall(str(text))
    if not nums:
        return None
    val = float(nums[0])
    op = "<=" if re.search(r"不超过|以内|最多|上限", text) else ">="
    unit = ""
    m = re.search(r"\d+\.?\d*\s*([次元分角千万])", text)
    if m:
        unit = m.group(1)
    return {"value": val, "op": op, "unit": unit}


def _extract_entities(text):
    """从文本提取关键实体（项目名称、分类值等）。"""
    entities = []
    # 从引号/书名号/括号中提取
    for l, r in [("「", "」"), ("《", "》"), ('"', '"'), ("'", "'"), ("(", ")"), ("（", "）")]:
        pat = re.compile(re.escape(l) + r"([^" + re.escape(r) + r"]{2,20})" + re.escape(r))
        for m in pat.finditer(text):
            e = m.group(1).strip()
            if e and e not in entities:
                entities.append(e)
    return entities


def parse_rule(text, dispatch_type=None):
    """解析单条 NL 规则文本，返回增强的结构化描述。

    Args:
        text:          规则文本（自然语言）
        dispatch_type: 已知的分派类型（来自 dispatch_map，可为 None）

    Returns:
        {
            "pattern": str,           # 识别到的文本信号类型
            "confidence": float,      # 0-1 置信度
            "signals": list[str],     # 匹配到的信号词
            "threshold": dict|None,   # 数值阈值（若有）
            "entities": list[str],    # 关键实体列表
            "override_template": str|None,  # 兼容字段：若文本信号与 dispatch_type 不同，给出信号差异
        }
    """
    pattern, signals, conf = _detect_pattern(text)
    threshold = _extract_threshold(text) if pattern in ("threshold", "frequency_overflow") else None
    entities = _extract_entities(text)
    override = pattern if (dispatch_type and dispatch_type != pattern and conf > 0.6) else None
    return {
        "pattern": pattern,
        "confidence": conf,
        "signals": signals,
        "threshold": threshold,
        "entities": entities,
        "override_template": override,
    }


def classify_batch(rows, nl_columns, dispatch_col=None):
    """批量解析知识表规则行，返回带增强模式标注的行列表。

    Args:
        rows:        知识表行列表（dict）
        nl_columns:  NL 描述列名列表
        dispatch_col: 分派键列名（可为 None）

    Returns:
        [{"_pattern": str, "_confidence": float, "_override": str|None, ...原始行...}]
    """
    result = []
    for row in rows:
        text = " ".join(str(row.get(c, "")) for c in nl_columns if row.get(c))
        dispatch_type = str(row.get(dispatch_col, "")).strip() if dispatch_col else None
        parsed = parse_rule(text, dispatch_type)
        enhanced = dict(row)
        enhanced["_pattern"] = parsed["pattern"]
        enhanced["_confidence"] = parsed["confidence"]
        enhanced["_signals"] = parsed["signals"]
        enhanced["_override"] = parsed["override_template"]
        if parsed["threshold"]:
            enhanced["_threshold"] = parsed["threshold"]
        if parsed["entities"]:
            enhanced["_entities"] = parsed["entities"]
        result.append(enhanced)
    return result


if __name__ == "__main__":
    if len(sys.argv) == 2:
        result = parse_rule(sys.argv[1])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 3:
        import pandas as pd
        path = sys.argv[1]
        nl_col = sys.argv[2]
        dispatch_col = sys.argv[3] if len(sys.argv) > 3 else None
        p = str(path).lower()
        if p.endswith((".xlsx", ".xls")):
            df = pd.read_excel(path)
        else:
            df = pd.read_csv(path, encoding="utf-8-sig")
        rows = df.head(20).to_dict("records")
        results = classify_batch(rows, [nl_col], dispatch_col)
        for r in results:
            print(json.dumps(
                {k: v for k, v in r.items() if k.startswith("_") or k in (nl_col, dispatch_col)},
                ensure_ascii=False))
    else:
        print("用法：python skill_nl_rule_parser.py '规则文本'")
        print("      python skill_nl_rule_parser.py knowledge.csv nl列名 [dispatch列名]")
'''


# ===========================================================================
# dispatch_config.json 构造（完全从 scenario 元数据推导，不预置任何业务默认值）
# ===========================================================================
def _build_dispatch_config(scenario: Scenario) -> dict:
    """从 scenario 元数据推导知识表分派配置。

    完全域无关：所有字段名均来自 scenario.flow.knowledge_schema。v1.0.7 起不再固化
    任何"分派值 → SQL"的映射——这里只交代**结构**（分派键列、自然语言条件列、
    知识字段与业务表字段的语义对应关系 field_role_map），具体"这条规则该怎么判断"
    留给运行时读到规则原文的 LLM 现场推理、现场查询。
    """
    ks: KnowledgeSchemaMapping | None = None
    if scenario.flow:
        if scenario.flow.knowledge_schema:
            ks = scenario.flow.knowledge_schema
        elif scenario.flow.rule_schema:
            ks = scenario.flow.rule_schema.to_knowledge_schema()

    knowledge_table = ""
    dispatch_key_col = ""
    knowledge_id_col = ""
    nl_columns: list[str] = []
    dispatch_map: dict[str, str] = {}
    field_role_map: dict[str, str] = {}

    if ks:
        knowledge_table = ks.knowledge_table
        dispatch_key_col = ks.dispatch_key_column
        knowledge_id_col = ks.item_id_column
        nl_columns = list(ks.condition_columns or [])
        dispatch_map = dict(ks.dispatch_map or {})
        field_role_map = dict(ks.field_role_map or {})

    # 找不到知识表名 → 从 tables_meta 里找角色为 rule/knowledge 的表
    if not knowledge_table:
        kt = next((t for t in scenario.tables_meta
                   if t.role in (TableRole.RULE.value, "knowledge")), None)
        if kt:
            knowledge_table = kt.table_name

    if not knowledge_table:
        return {}  # 无知识表 → pipeline 模式，dispatch_config 为空

    # ---- annotation_columns：知识行注解到结果列的映射 ----
    # 注解列名来自 knowledge_schema 的字段，不预置任何中文列名
    annotation_columns: dict[str, str] = {}
    if dispatch_key_col:
        annotation_columns["dispatch_key"] = dispatch_key_col
    if knowledge_id_col:
        annotation_columns["knowledge_id"] = knowledge_id_col
    if nl_columns:
        annotation_columns["knowledge_desc"] = nl_columns[0]

    return {
        "knowledge_table": knowledge_table,
        "dispatch_key_column": dispatch_key_col,
        "knowledge_id_column": knowledge_id_col,
        "nl_columns": nl_columns,
        "dispatch_map": dispatch_map,
        "field_role_map": field_role_map,
        "annotation_columns": annotation_columns,
    }


# ===========================================================================
# SCENARIO_CONTEXT.md 生成（给任意 AI Agent 的完整提示词）
# ===========================================================================
def _generate_scenario_context(skill_dir: Path, scenario: Scenario,
                                domain: DomainKnowledge, dispatch: dict) -> None:
    """Deprecated: root scenario documents are no longer generated."""
    return


# ===========================================================================
# schema.json 生成（OpenAI function-calling 格式）
# ===========================================================================
def _generate_tool_schema(skill_dir: Path, scenario: Scenario, dispatch: dict) -> None:
    """生成 schema.json：任意 LLM Agent 可按此定义调用本场景的全套工具。

    工具按场景能力生成：基础场景只有 execute/query_data；存在知识表时才暴露
    search_knowledge/list_knowledge。
    """
    outputs = scenario.outputs or []
    output_ids = [o.output_id for o in outputs]
    output_desc = "; ".join(f"{o.output_id}={o.name}" for o in outputs[:5])
    slug = _slug(scenario.name)
    has_knowledge = bool(dispatch.get("knowledge_table"))

    execute_tool = {
        "name": f"execute_{slug}",
        "description": (
            f"执行业务场景「{scenario.name}」的数据处理，产出结果文件。\n"
            f"可用产出：{output_desc}\n"
            + (
                "params 可过滤知识表条目：空=全量；字符串=关键词；对象={列名:值}=精确匹配。"
                if has_knowledge else
                "本场景无知识表，execute 按流程管线直接处理业务数据。"
            )
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "output_id": {
                    "type": "string",
                    "description": f"产出ID，可选值：{output_ids}",
                    "enum": output_ids,
                },
                "data_dir": {
                    "type": "string",
                    "description": "包含业务数据文件的目录路径（文件名=表名）",
                },
                "out_dir": {
                    "type": "string",
                    "description": "产出文件保存目录",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "最大产出行数（默认 20000）",
                    "default": 20000,
                },
            },
            "required": ["output_id", "data_dir"],
        },
        "returns": {
            "type": "object",
            "properties": {
                "rows": {"type": "integer", "description": "产出行数"},
                "artifact": {"type": "string", "description": "产出文件路径"},
                "mode": {"type": "string", "description": "执行模式（pipeline/knowledge_engine）"},
            },
        },
    }
    if has_knowledge:
        execute_tool["parameters"]["properties"]["params"] = {
            "type": ["string", "object", "null"],
            "description": "知识条目过滤参数：字符串=关键词；对象={列名:列值}；null=全部条目",
        }

    search_tool = {
        "name": f"search_knowledge_{slug}",
        "description": (
            f"在「{scenario.name}」场景的知识表中搜索匹配条目。"
            "用于在执行前了解有哪些可用的知识条目。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（对知识表所有列做模糊匹配，空=全量）",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限（默认 20）",
                    "default": 20,
                },
                "data_dir": {
                    "type": "string",
                    "description": "数据目录（可选，覆盖默认路径）",
                },
            },
            "required": [],
        },
        "returns": {
            "type": "array",
            "items": {"type": "object"},
            "description": "匹配的知识条目列表",
        },
    }

    list_tool = {
        "name": f"list_knowledge_{slug}",
        "description": (
            f"列出「{scenario.name}」场景知识表的所有条目（分页）。"
            "用于全览可执行的知识条目范围。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限（默认 50）",
                    "default": 50,
                },
                "data_dir": {
                    "type": "string",
                    "description": "数据目录（可选）",
                },
            },
            "required": [],
        },
        "returns": {
            "type": "array",
            "items": {"type": "object"},
            "description": "知识条目列表",
        },
    }

    query_tool = {
        "name": f"query_data_{slug}",
        "description": (
            f"对「{scenario.name}」场景的新业务数据执行 DuckDB SQL，即席查询任意表、字段、JOIN、聚合。"
            "命令行入口：skill_query_data/scripts/query_data.py。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "要执行的 DuckDB SQL。表名建议用双引号引用。",
                },
                "data_dir": {
                    "type": "string",
                    "description": "包含业务数据文件的目录路径（文件名=表名）",
                },
                "out_dir": {
                    "type": "string",
                    "description": "查询结果保存目录",
                },
            },
            "required": ["sql", "data_dir"],
        },
        "returns": {
            "type": "object",
            "properties": {
                "rows": {"type": "integer", "description": "查询结果行数"},
                "artifact": {"type": "string", "description": "CSV 结果文件路径"},
                "preview": {"type": "string", "description": "结果预览"},
            },
        },
    }

    tools = [execute_tool, query_tool]
    if has_knowledge:
        tools[1:1] = [search_tool, list_tool]
    schema = {"tools": tools}
    (skill_dir / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ===========================================================================
# SKILL.md 生成
# ===========================================================================
def _write_main_skill_md(skill_dir: Path, scenario: Scenario,
                          domain: DomainKnowledge, dispatch: dict) -> None:
    outputs = scenario.outputs or []
    tables_hint = "\n".join(
        f"- `{t.table_name}`（{t.role}）：{t.row_count or '?'} 行；"
        + ", ".join(c.name for c in t.columns[:12])
        + ("…" if len(t.columns) > 12 else "")
        for t in domain.tables
    ) or "（无）"
    outputs_hint = "\n".join(
        f"- {o.name}（`{o.output_id}`，格式 `{o.fmt}`，状态 `{o.status}`）"
        for o in outputs[:40]
    ) or "（尚无产出规格）"

    mode_desc = "知识表驱动（Knowledge Engine）" if dispatch.get("knowledge_table") else "Pipeline SQL"
    first_id = outputs[0].output_id if outputs else "OUTPUT_ID"
    has_knowledge = bool(dispatch.get("knowledge_table"))
    params_doc = (
        '- `params`：`None`（全部）| `"关键词"` | `{"col": "val"}` | `{"keyword": "xxx"}`，'
        '用于过滤知识表条目。\n'
        if has_knowledge else
        "- 本场景无知识表，`params` 不参与 Pipeline SQL 产出。\n"
    )
    keyword_command = (
        f"python scripts/skill_executor.py {first_id} /data ./out '\"关键词\"'\n"
        if has_knowledge else ""
    )

    md = _skill_frontmatter(
        "scenario-main",
        (
            f"Execute the {scenario.name} business scenario outputs. Use when an agent needs packaged outputs "
            f"and knowledge-row inspection for {scenario.name} business data."
            if has_knowledge else
            f"Execute the {scenario.name} business scenario outputs. Use when an agent needs deterministic "
            f"pipeline processing for {scenario.name} business data."
        ),
    ) + f"""

# 主技能

**执行模式**：{mode_desc}

## 接口

```python
produce(output_id, data_path, out_dir=None, params=None, max_rows=20000) -> dict
```

{params_doc}

## 数据表
{tables_hint}

## 可执行产出
{outputs_hint}

## 命令行
```bash
python scripts/skill_executor.py
python scripts/skill_executor.py {first_id} /data ./out
{keyword_command.rstrip()}
```

## 上下文资源
- 所有结构化信息（表结构、字段语义、关联关系、产出规格、知识表配置）**已通过 action tools 提供**，
  不应直接读取 JSON 配置文件。
- 宿主 Agent 平台负责附件上传、文件预览和结果文件下载。
"""
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")


def _write_node_skill_md(skill_dir: Path, scenario: Scenario, step: FlowStep) -> None:
    in_lines = "\n".join("- " + s for s in step.data_in) if step.data_in else "- （未填写）"
    out_lines = "\n".join("- " + s for s in step.data_out) if step.data_out else "- （未填写）"
    md = _skill_frontmatter(
        f"step-{step.step_id}",
        f"Perform the {step.step_name} step for the {scenario.name} business scenario. "
        f"Use when this specific flow node is needed: {step.purpose or step.capability or step.step_name}.",
    ) + f"""

# {step.step_name}

## 目标（purpose）
{step.purpose or "（未填写）"}

## 能力（capability）
{step.capability or "（未填写）"}

## 数据输入
{in_lines}

## 数据输出
{out_lines}

## 策略线索 & 参数
- 策略线索：`{step.template_kind or step.strategy or "—"}`
- 参数：

```json
{json.dumps(step.params, ensure_ascii=False, indent=2)}
```

## 接口
`scripts/run.py` → `run(tables: dict, out_dir=None) -> dict`
"""
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")


# ===========================================================================
# 落盘
# ===========================================================================
def materialize_skills(scenario: Scenario) -> list[Skill]:
    """生成标准子 Skill + MCP 元数据。保留用户进化技能。"""
    base = store.skills_dir(scenario.id)
    evolved = [s for s in scenario.skills if s.is_evolved]
    evolved_ids = {s.skill_id for s in evolved}
    _cleanup_legacy_root_files(base)

    for child in base.iterdir():
        if not child.is_dir():
            continue
        if child.name in _RESERVED_DIRS:
            continue
        # Generated standard skill directories are rebuilt each time.
        is_generated = (
            any(child.name.startswith(p) for p in _SKILL_PREFIXES)
            or child.name in _GENERATED_SKILL_DIRS
        )
        if is_generated and child.name not in evolved_ids:
            shutil.rmtree(child, ignore_errors=True)

    # 刷新数据字典与产出规格
    domain = transform_builder.build_domain_knowledge(scenario)
    scenario.domain_knowledge = domain
    scenario.outputs = transform_builder.build_outputs(scenario, domain)

    # ---- 独立运行依赖清单（脱离平台部署时 pip install -r requirements.txt 即可） ----
    (base / "requirements.txt").write_text(_REQUIREMENTS_TXT, encoding="utf-8")

    # ---- 主技能 ----
    main_dir = base / _MAIN_SKILL_ID
    main_scripts = main_dir / "scripts"
    main_scripts.mkdir(parents=True, exist_ok=True)

    (main_dir / "domain_knowledge.json").write_text(
        domain.model_dump_json(indent=2), encoding="utf-8"
    )
    (main_dir / "output_specs.json").write_text(
        json.dumps({"outputs": [o.model_dump() for o in scenario.outputs]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    dispatch = _build_dispatch_config(scenario)
    has_knowledge = bool(dispatch.get("knowledge_table"))
    (main_dir / "dispatch_config.json").write_text(
        json.dumps(dispatch, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (main_scripts / "skill_executor.py").write_text(_EXECUTOR_TEMPLATE, encoding="utf-8")

    _write_main_skill_md(main_dir, scenario, domain, dispatch)
    _generate_tool_schema(main_dir, scenario, dispatch)

    knowledge_dir = base / _KNOWLEDGE_SKILL_ID
    nl_parser_dir = base / _NL_RULE_SKILL_ID
    if has_knowledge:
        # ---- 基础技能：知识条目检索（仅知识表场景生成）----
        knowledge_scripts = knowledge_dir / "scripts"
        knowledge_scripts.mkdir(parents=True, exist_ok=True)
        (knowledge_scripts / "search_knowledge.py").write_text(_SEARCH_KNOWLEDGE_TEMPLATE, encoding="utf-8")
        (knowledge_scripts / "list_knowledge.py").write_text(_LIST_KNOWLEDGE_TEMPLATE, encoding="utf-8")
        (knowledge_dir / "SKILL.md").write_text(
            _skill_frontmatter(
                "knowledge-search",
                f"Search and list knowledge or rule rows for the {scenario.name} business scenario. "
                "Use before rule-driven execution to inspect applicable knowledge entries and their source text.",
            ) + f"""

# 知识条目检索

本 skill 负责浏览和搜索本业务场景的知识/规则/标准条目。它是标准 Skill，脚本只存在于本目录 `scripts/` 下。

## 何时使用
- 需要查看本场景有哪些知识/规则条目。
- 需要按关键词定位某条业务规则原文。
- `main_skill` 返回知识行后，需要进一步理解规则文本。

## 脚本
```bash
python scripts/list_knowledge.py 50 /path/to/data
python scripts/search_knowledge.py "关键词" 20 /path/to/data
```

## 共享上下文
读取包根目录下的 `main_skill/dispatch_config.json` 和 `main_skill/domain_knowledge.json`。
""",
            encoding="utf-8",
        )

    # ---- 基础技能：业务数据即席查询 ----
    query_dir = base / _QUERY_SKILL_ID
    query_scripts = query_dir / "scripts"
    query_scripts.mkdir(parents=True, exist_ok=True)
    (query_scripts / "query_data.py").write_text(_QUERY_DATA_TEMPLATE, encoding="utf-8")
    (query_dir / "SKILL.md").write_text(
        _skill_frontmatter(
            "business-data-query",
            f"Run ad hoc DuckDB SQL over {scenario.name} business scenario data. "
            "Use for field checks, joins, aggregations, and output verification after reading the schema.",
        ) + f"""

# 业务数据即席查询

本 skill 负责对宿主已经提供的数据目录执行 DuckDB SQL 查询。文件上传、附件解析和结果下载由宿主平台负责。

## 何时使用
- 已读取 `main_skill/domain_knowledge.json`，需要现场构造 SQL。
- 需要核查流程节点、字段、JOIN、聚合或样本数据。

## 脚本
```bash
python scripts/query_data.py --data-dir /path/to/data --sql 'SELECT * FROM "表名" LIMIT 10'
python scripts/query_data.py --data-dir /path/to/data --sql-file query.sql --out-dir ./outputs
```

## 注意
表名必须与 `main_skill/domain_knowledge.json` 中的 `table_name` 完全一致，建议用双引号引用。
""",
        encoding="utf-8",
    )

    # ---- 基础技能：数据读取器 ----
    reader_dir = base / _DATA_READER_SKILL_ID
    reader_scripts = reader_dir / "scripts"
    reader_scripts.mkdir(parents=True, exist_ok=True)
    (reader_scripts / "skill_data_reader.py").write_text(_DATA_READER_TEMPLATE, encoding="utf-8")
    (reader_dir / "SKILL.md").write_text(
        _skill_frontmatter(
            "data-reader",
            f"Read and validate business data files for the {scenario.name} scenario. "
            "Use when an agent needs to inspect xlsx, csv, tsv, or json tables before running scenario skills.",
        ) + f"""

# 数据读取与结构校验

**功能**：读取任意格式业务数据文件（xlsx/csv/tsv/json），校验字段完整性。

## 接口

```python
read(data_path, table_name=None) -> dict[str, pd.DataFrame]
validate_schema(frames, required_tables=None, strict=False) -> dict
```

## 支持格式
- `.xlsx` / `.xls` / `.csv` / `.tsv` / `.json`
- 自动探测编码（utf-8 / gbk / gb18030）
- 自动从 `domain_knowledge.json` 读取必要字段列表做校验

## 命令行
```bash
python scripts/skill_data_reader.py /path/to/data
python scripts/skill_data_reader.py /path/to/data 表名
```
""",
        encoding="utf-8",
    )

    if has_knowledge:
        # ---- 基础技能：NL 规则解析器（仅知识表场景生成）----
        nl_parser_scripts = nl_parser_dir / "scripts"
        nl_parser_scripts.mkdir(parents=True, exist_ok=True)
        (nl_parser_scripts / "skill_nl_rule_parser.py").write_text(
            _NL_RULE_PARSER_TEMPLATE, encoding="utf-8"
        )
        (nl_parser_dir / "SKILL.md").write_text(
            _skill_frontmatter(
                "rule-parser",
                f"Parse natural-language knowledge or rule text for the {scenario.name} scenario. "
                "Use to inspect text signals in knowledge rows before querying data.",
            ) + f"""

# 自然语言规则解析

**功能**：对知识表中的自然语言规则文本做运行时语义识别，
辅助宿主 Agent 理解知识行文本线索；不预置或替代真实业务判断逻辑。

## 接口

```python
parse_rule(text, dispatch_type=None) -> dict
classify_batch(rows, nl_columns, dispatch_col=None) -> list[dict]
```

## 返回字段
- `_pattern`：识别到的模式（co_occurrence/frequency_overflow/threshold/exclusive_conflict/keyword）
- `_confidence`：置信度 0-1
- `_threshold`：提取的数值阈值（如频次限制、金额上限）
- `_entities`：关键实体（从引号/括号中提取）
- `_override`：若与 dispatch_map 不一致，给出文本信号差异提示

## 命令行
```bash
python scripts/skill_nl_rule_parser.py "不得同时申报A和B"
python scripts/skill_nl_rule_parser.py knowledge.csv nl_col dispatch_col
```
""",
            encoding="utf-8",
        )

    mode = "knowledge_engine" if dispatch.get("knowledge_table") else "pipeline"
    main_skill = Skill(
        skill_id=_MAIN_SKILL_ID,
        name=f"主技能（{mode}）",
        operation="PRODUCE",
        description=(
            f"自包含执行器：{mode} 模式，{len(scenario.tables_meta)} 张表，"
            f"{len(scenario.outputs)} 个产出。"
            "可脱离平台独立运行。"
        ),
        is_main=True,
        path=str(main_dir),
        capability=f"produce(output_id, data_path, out_dir, params) → 结果文件（{mode}）",
    )

    # ---- 节点子技能 ----
    node_skills: list[Skill] = []
    for s in (scenario.flow.flow_steps if scenario.flow else []):
        sid = f"step_{s.step_id}_{_slug(s.step_name)}"
        sdir = base / sid
        scripts = sdir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "run.py").write_text(_render_node_runner(s), encoding="utf-8")
        _write_node_skill_md(sdir, scenario, s)
        node_skills.append(Skill(
            skill_id=sid,
            name=s.step_name,
            operation=s.operation,
            description=f"{s.purpose or s.step_name}（策略线索：{s.template_kind or s.strategy or '—'}）",
            step_id=s.step_id,
            is_main=False,
            path=str(sdir),
            capability=s.capability,
            status="generated" if s.status != "blocked" else "blocked",
        ))

    knowledge_skill = None
    if has_knowledge:
        # ---- 工具技能（知识表查询工具，独立可执行）----
        knowledge_skill = Skill(
            skill_id=_KNOWLEDGE_SKILL_ID,
            name="知识条目检索",
            operation="QUERY",
            description=(
                "独立可执行的知识表查询工具：search_knowledge.py（关键词搜索）+ "
                "list_knowledge.py（全览条目）。脱离平台直接运行。"
            ),
            is_main=False,
            path=str(knowledge_dir),
            capability="search_knowledge(keyword, limit) / list_all(limit) → list[dict]",
        )

    # ---- 基础技能（完整业务生命周期所需）----
    reader_skill = Skill(
        skill_id=_DATA_READER_SKILL_ID,
        name="数据读取与校验",
        operation="READ",
        description="读取任意格式业务数据（xlsx/csv/tsv/json），自动探测编码，校验字段完整性。",
        is_main=False,
        path=str(reader_dir),
        capability="read(data_path) → {表名: DataFrame}；validate_schema(frames) → {ok, warnings}",
    )
    nl_parser_skill = None
    if has_knowledge:
        nl_parser_skill = Skill(
            skill_id=_NL_RULE_SKILL_ID,
            name="NL 文本线索解析",
            operation="PARSE",
            description=(
                "对知识表自然语言文本做运行时线索识别，辅助宿主 Agent 理解知识行；"
                "不替代真实业务判断逻辑。"
            ),
            is_main=False,
            path=str(nl_parser_dir),
            capability="parse_rule(text) → {pattern, confidence, threshold, entities}",
        )
    query_skill = Skill(
        skill_id=_QUERY_SKILL_ID,
        name="业务数据即席查询",
        operation="QUERY",
        description="对本场景业务数据执行 DuckDB SQL，支持任意表、字段、JOIN、聚合并落盘结果。",
        is_main=False,
        path=str(query_dir),
        capability="query(sql, data_dir, out_dir) → CSV 结果 + 预览",
    )
    all_skills = [
        main_skill,
        query_skill,
        reader_skill,
        *node_skills,
        *evolved,
    ]
    if knowledge_skill is not None:
        all_skills.insert(1, knowledge_skill)
    if nl_parser_skill is not None:
        all_skills.insert(4 if knowledge_skill is not None else 3, nl_parser_skill)

    # ---- manifest.json（Agent 平台发现入口）----
    _write_manifest(base, scenario, all_skills, dispatch, mode)

    # ---- MCP 描述符 + Skill-only system_prompt.md ----
    _write_mcp_descriptor(base, scenario, domain, dispatch, mode)

    return all_skills


# ===========================================================================
# manifest.json 生成（Agent 平台发现与调用的统一入口）
# ===========================================================================
def _write_manifest(
    skill_dir: Path, scenario: Scenario,
    skills: list[Skill], dispatch: dict, mode: str,
) -> None:
    """生成 manifest.json：验证 Agent / 第三方 Agent 的场景能力描述清单。

    包含：场景描述、技能索引、执行入口、工具定义（OpenAI function-calling 格式）。
    脱离平台后，第三方 Agent 只需读此文件即可了解本场景全套能力。
    """
    now = datetime.now(timezone.utc).isoformat()

    # 技能索引
    skills_index = []
    for s in skills:
        entry = {
            "skill_id": s.skill_id,
            "name": s.name,
            "operation": s.operation,
            "is_main": s.is_main,
            "capability": s.capability or "",
            "description": s.description or "",
        }
        if s.skill_id == _MAIN_SKILL_ID:
            entry["entry_point"] = "main_skill/scripts/skill_executor.py"
        elif s.skill_id == _KNOWLEDGE_SKILL_ID:
            entry["entry_points"] = {
                "search": "skill_knowledge_search/scripts/search_knowledge.py",
                "list": "skill_knowledge_search/scripts/list_knowledge.py",
            }
        elif s.skill_id == _QUERY_SKILL_ID:
            entry["entry_point"] = "skill_query_data/scripts/query_data.py"
        elif s.skill_id == _DATA_READER_SKILL_ID:
            entry["entry_point"] = "skill_data_reader/scripts/skill_data_reader.py"
        elif s.skill_id == _NL_RULE_SKILL_ID:
            entry["entry_point"] = "skill_nl_rule_parser/scripts/skill_nl_rule_parser.py"
        else:
            entry["entry_point"] = f"{s.skill_id}/scripts/run.py"
        skills_index.append(entry)

    # 产出规格摘要
    outputs_summary = [
        {"output_id": o.output_id, "name": o.name, "fmt": o.fmt, "status": o.status}
        for o in (scenario.outputs or [])
    ]

    # 读 schema.json 作为工具定义
    schema_file = skill_dir / "main_skill" / "schema.json"
    tools_def = []
    if schema_file.exists():
        try:
            tools_def = json.loads(schema_file.read_text(encoding="utf-8")).get("tools", [])
        except Exception:
            pass
    entry_points = {
        "executor": "main_skill/scripts/skill_executor.py",
        "query_data": "skill_query_data/scripts/query_data.py",
        "data_reader": "skill_data_reader/scripts/skill_data_reader.py",
        "requirements": "requirements.txt",
    }
    if dispatch.get("knowledge_table"):
        entry_points.update({
            "search_knowledge": "skill_knowledge_search/scripts/search_knowledge.py",
            "list_knowledge": "skill_knowledge_search/scripts/list_knowledge.py",
            "nl_rule_parser": "skill_nl_rule_parser/scripts/skill_nl_rule_parser.py",
        })

    manifest = {
        "version": "1.1.0",
        "generated_at": now,
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "namespace": _ascii_namespace(scenario.name, scenario.id),
        "skill_name": _skill_name_from_namespace(_ascii_namespace(scenario.name, scenario.id)),
        "description": scenario.description or "",
        "execution_mode": mode,
        "has_knowledge_table": bool(dispatch.get("knowledge_table")),
        "knowledge_table": dispatch.get("knowledge_table", ""),
        # MCP 能力包指针；Skill-only 发布物由 release builder 单独生成。
        "mcp": {
            "descriptor": "mcp.json",
            "config_example": "mcp_config.example.json",
        },
        "skills": skills_index,
        "outputs": outputs_summary,
        "tools": tools_def,
        "entry_points": entry_points,
        "verify_instructions": (
            "将本场景 skills/ 目录挂载到验证 Agent，Agent 即可调用上述工具执行业务场景。"
            "Skill-only 发布包只包含标准 Skill 子目录和 system_prompt.md；"
            "MCP/Docker 发布包另带 requirements.txt 和 runtime。"
        ),
    }

    (skill_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ===========================================================================
# 标准 Skill 能力包生成（MCP 只是可选兼容入口）
# ===========================================================================
# 命名空间化工具的固定动作集：任意场景包只暴露业务能力 action。
# 工具名 = f"{namespace}__{action}"，用命名空间前缀保证多场景同时挂载不撞名。
_MCP_ACTIONS = (
    "describe_capability",
    "describe_schema",
    "list_outputs",
    "execute",
    "query_data",
)


def _ascii_namespace(name: str, scenario_id: str) -> str:
    """生成 ASCII 安全的命名空间（MCP/OpenAI 工具名要求 ^[A-Za-z0-9_-]+$）。

    中文场景名无法直接做工具名前缀，故：优先取名称里的 ASCII 字母数字；
    若为空（纯中文名），回退用场景 id 派生一个稳定短标识，保证跨进程一致且唯一。
    """
    ascii_part = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_")
    if ascii_part and not ascii_part[0].isdigit():
        return ascii_part[:40]
    sid = re.sub(r"[^0-9A-Za-z]+", "", scenario_id) or "scn"
    return f"s_{sid[-8:]}"


def _skill_name_from_namespace(ns: str) -> str:
    """生成 Agent Skill 兼容名称：小写字母/数字/连字符，适合作为技能目录名。"""
    safe = re.sub(r"[^0-9a-z-]+", "-", ns.replace("_", "-").lower()).strip("-")
    if not safe or safe[0].isdigit():
        safe = f"s-{safe or 'scenario'}"
    return f"bfe-{safe}"[:63].rstrip("-")


def _yaml_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', "'").replace("\n", " ") + '"'


def _write_openai_agent_metadata(skill_dir: Path, skill_name: str, card: dict) -> None:
    """生成 Codex/OpenAI Skill UI 元数据。缺失也能用，但有它安装体验更接近原生 Skill。"""
    agents_dir = skill_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    display = card.get("display_name") or skill_name
    summary = (card.get("summary") or display).strip()
    short = summary
    if len(short) > 64:
        short = short[:61].rstrip() + "..."
    default_prompt = f"Use ${skill_name} to inspect my business data and complete the matching scenario task."
    text = (
        "interface:\n"
        f"  display_name: {_yaml_quote(display)}\n"
        f"  short_description: {_yaml_quote(short)}\n"
        f"  default_prompt: {_yaml_quote(default_prompt)}\n"
        "\n"
        "policy:\n"
        "  allow_implicit_invocation: true\n"
    )
    (agents_dir / "openai.yaml").write_text(text, encoding="utf-8")


def _write_subagent_system_prompt(
    skill_dir: Path,
    scenario: Scenario,
    skill_name: str,
    card: dict,
    tools: list[dict],
) -> None:
    """生成第三方创建业务子 Agent 时可直接粘贴的 System Prompt。

    此 prompt 是自包含的——粘贴到任何第三方 Agent 平台（Claude Desktop、
    Cursor、Cline 等）或本平台的 agent_config.json 中都无需再手动修改。
    """
    required_tables = "、".join(card.get("required_tables") or []) or "调用 describe_schema 获取表结构"
    tool_descriptions = "\n".join(
        f"- `{t.get('name', '')}`：{t.get('description', '')[:150]}"
        for t in tools if t.get("name")
    ) or "（无可用业务动作）"
    tool_names = [t.get("name", "") for t in tools if t.get("name")]
    tool_list_bullet = "\n".join(f"  - `{name}`" for name in tool_names)
    when = "\n".join(f"- {item}" for item in card.get("when_to_use", [])) or "- 用户诉求明确落入本业务场景"
    not_for = "\n".join(f"- {item}" for item in card.get("not_for", [])) or "- 与本场景无关的任务"
    has_knowledge = bool(card.get("knowledge_table"))

    prompt = f"""# {scenario.name} 子 Agent System Prompt

你是 `{skill_name}` 业务场景子 Agent，只负责处理「{scenario.name}」相关任务。

## 业务职责
{card.get('summary', '')}

## 何时接管
{when}

## 何时拒绝接管
{not_for}

## 需要的业务数据
必需业务表：{required_tables}

附件上传、文件预览、结果文件下载由宿主 Agent 平台负责。不要假设本 Skill 可以直接写宿主平台目录。

## 🛑 工具使用纪律（硬性约束）

当前已注册以下可直接调用的业务 action 工具：

{tool_list_bullet}

你必须严格遵守以下纪律：

1. **必须优先调用已注册的 action tools** 完成所有业务操作。所有业务信息（表结构、字段语义、知识条目、关联关系、产出规格）一律通过 action tools 获取。
2. **禁止**使用 `read_file` 或其他内置文件工具读取技能包目录下的 JSON/配置/脚本文件（如 `domain_knowledge.json`、`output_specs.json`、`dispatch_config.json` 等）来替代调用 action tools。
3. **禁止**临时创建 Python/SQL 脚本或使用 shell/execute 重写业务逻辑——所有业务处理都必须通过上述 action tools 完成。
4. **禁止**直接推理或编造表结构、字段名、关联关系——必须先用 `describe_schema` 获取准确结构。
5. 涉及业务数据查询时，先用 `describe_schema` 确认表名和字段，再用对应的 action tool 执行。

## 可用业务动作

{tool_descriptions}

## 工作方式
1. 先确认用户诉求是否匹配本业务场景，不匹配则交还主 Agent。
2. 先调用 action tools 获取能力说明和 schema，再判断需要哪些表、字段、知识条目和流程节点。""" + (
    "3. 知识/规则驱动任务要先通过 action tools 定位规则原文，再结合业务数据逐条判断。"
    if has_knowledge else
    "3. 按流程节点和表间关系处理业务数据，不假设存在额外知识/规则表。"
) + """
4. 不编造字段、表名、流程节点或业务结论；缺少关键数据时说明缺什么。
5. 输出结果时说明依据""" + (
    "、命中规则" if has_knowledge else ""
) + """、关键字段和可复核的数据范围。
"""
    (skill_dir / "system_prompt.md").write_text(prompt, encoding="utf-8")


def _write_toolkit_doc(skill_dir: Path, scenario: Scenario, card: dict, tools: list[dict]) -> None:
    """Deprecated: Skill-only packages no longer generate toolkit documents."""
    return


def _synthesize_capability_card(
    scenario: Scenario, domain: DomainKnowledge, dispatch: dict, mode: str,
) -> dict:
    """确定性地合成「能力卡片」——第三方 Agent 据此发现本场景做什么、何时该/不该调用。

    全部字段从已蒸馏的场景元数据推导，不依赖 LLM，保证一定有值。
    """
    name = scenario.name
    ns = _ascii_namespace(name, scenario.id)

    input_tables = [t.table_name for t in domain.tables if t.role == "input"]
    knowledge_table = dispatch.get("knowledge_table", "")
    dispatch_key = dispatch.get("dispatch_key_column", "")
    # 必需表 = 业务输入表 + 知识表（结果表只是输出列模板，不算必需输入）
    required_tables: list[str] = []
    for t in input_tables + ([knowledge_table] if knowledge_table else []):
        if t and t not in required_tables:
            required_tables.append(t)

    outputs = scenario.outputs or []
    output_names = [o.name for o in outputs]
    steps = scenario.flow.flow_steps if scenario.flow else []
    purposes = [s.purpose for s in steps if s.purpose]

    # ---- summary：一句话说清这个能力包干什么 ----
    if scenario.description:
        summary = f"『{name}』业务能力：{scenario.description}"
    elif purposes:
        summary = f"『{name}』业务能力：" + "；".join(purposes[:3])
    else:
        summary = f"『{name}』业务能力包（{mode} 模式）"
    if output_names:
        summary += f"。产出：{'、'.join(output_names[:3])}"

    # ---- when_to_use：触发判据 ----
    when_to_use = [f"用户要处理与「{name}」直接相关的业务需求"]
    if required_tables:
        w = f"用户提供了与本场景同结构的数据（必需表：{'、'.join(required_tables)}）并希望据此产出结果"
        if output_names:
            w += f"，例如「{output_names[0]}」"
        when_to_use.append(w)
    if knowledge_table:
        detail = f"（分派键列「{dispatch_key}」）" if dispatch_key else ""
        when_to_use.append(
            f"用户希望依据知识表「{knowledge_table}」{detail}中的条目对业务数据做逐条判定/筛查"
        )

    # ---- not_for：反触发（多场景并存时避免误调的关键）----
    not_for = ["与本场景无关的通用数据分析、闲聊、或其他业务领域的任务"]
    if required_tables:
        not_for.append(
            f"用户数据缺少本场景必需表（{'、'.join(required_tables)}），或表结构与本场景明显不匹配"
        )

    # ---- keywords：粗粒度匹配用 ----
    keywords: list[str] = [name]
    for k in list((dispatch.get("dispatch_map") or {}).keys())[:12]:
        if k and k not in keywords:
            keywords.append(k)
    for t in required_tables:
        if t not in keywords:
            keywords.append(t)

    return {
        "namespace": ns,
        "skill_name": _skill_name_from_namespace(ns),
        "display_name": name,
        "summary": summary,
        "when_to_use": when_to_use,
        "not_for": not_for,
        "keywords": keywords,
        "required_tables": required_tables,
        "knowledge_table": knowledge_table,
        "execution_mode": mode,
        "requires_host_llm_reasoning": bool(dispatch.get("knowledge_table")),
        "primary_install_mode": "skill_directory",
    }


def _mcp_tool_defs(ns: str, card: dict, scenario: Scenario) -> list[dict]:
    """构建命名空间化 MCP 工具定义（name / action / description / inputSchema）。

    这里仅声明业务能力本身。文件上传、下载、产物分发由第三方 Agent 平台的
    标准文件能力负责，不作为本业务 Skill 的能力。
    """
    name = scenario.name
    output_ids = [o.output_id for o in (scenario.outputs or [])]
    has_knowledge = bool(card.get("knowledge_table"))
    trigger = f"（本工具属于「{name}」能力包；仅当用户诉求匹配该能力时才调用）"

    def tool(action: str, desc: str, props: dict, required: list[str]) -> dict:
        return {
            "name": f"{ns}__{action}",
            "action": action,
            "description": desc + trigger,
            "inputSchema": {"type": "object", "properties": props, "required": required},
        }

    data_dir_prop = {
        "type": "string",
        "description": "可选：本地脚本执行时的新业务数据目录。第三方平台文件上传/下载由宿主平台处理。",
    }

    defs = [
        tool(
            "describe_capability",
            f"首次接入或不确定能力用途时先调用：说明「{name}」业务场景是什么、能做什么、需要哪些业务数据、有哪些产出、有哪些工具以及推荐调用流程。",
            {}, [],
        ),
        tool(
            "list_outputs",
            f"列出「{name}」场景可执行的业务产出、output_id、结果格式和当前可执行状态；调用 execute 前应先查看。",
            {}, [],
        ),
        tool(
            "describe_schema",
            (
                f"获取「{name}」场景的完整表结构：字段名/类型/业务语义 + 表间关联(ER)"
                + (" + 知识表分派结构。" if has_knowledge else "。")
                + "构造 query_data 的 SQL 前应先调用一次。"
            ),
            {}, [],
        ),
    ]
    if has_knowledge:
        defs.extend([
        tool(
            "list_knowledge",
            f"浏览「{name}」场景知识表的条目（分页），了解可执行的知识条目范围。",
            {
                "limit": {"type": "integer", "description": "返回条数上限（默认 50）", "default": 50},
                "data_dir": data_dir_prop,
            },
            [],
        ),
        tool(
            "search_knowledge",
            f"在「{name}」场景知识表中按关键词搜索条目。",
            {
                "keyword": {"type": "string", "description": "搜索关键词（空=返回前 N 条）"},
                "limit": {"type": "integer", "description": "返回条数上限（默认 20）", "default": 20},
                "data_dir": data_dir_prop,
            },
            [],
        ),
        ])
    defs.extend([
        tool(
            "execute",
            (
                f"执行「{name}」场景的数据处理并产出结果文件。"
                + (
                    "知识驱动模式下只返回命中的知识行原文，需据此再用 query_data 逐条落地查询。"
                    if has_knowledge else
                    "本场景无知识表，按流程管线直接处理业务数据。"
                )
            ),
            {
                "output_id": {
                    "type": "string",
                    "description": f"产出ID，可选值：{output_ids}",
                    **({"enum": output_ids} if output_ids else {}),
                },
                "params": {
                    "type": ["string", "object", "null"],
                    "description": (
                        "知识条目过滤：空=全量；字符串=关键词；对象={列名:值}=精确匹配"
                        if has_knowledge else
                        "本场景无知识表，通常无需传入。"
                    ),
                },
                "max_rows": {"type": "integer", "description": "最大产出行数（默认 20000）", "default": 20000},
                "data_dir": data_dir_prop,
                "out_dir": {
                    "type": "string",
                    "description": "可选：结果输出目录；不传时使用 BFE_OUT_DIR 或包同级 outputs/。",
                },
            },
            ["output_id"],
        ),
        tool(
            "query_data",
            f"对「{name}」场景已上传的业务数据执行 DuckDB SQL（任意表/字段/多表 JOIN/聚合）。"
            "表名用双引号，例如：SELECT * FROM \"表名\" LIMIT 10。",
            {
                "sql": {"type": "string", "description": "要执行的 DuckDB SQL"},
                "data_dir": data_dir_prop,
                "save_result": {
                    "type": "boolean",
                    "description": "是否把查询结果保存为 CSV；默认 false，避免临时查询污染输出目录。",
                    "default": False,
                },
                "out_dir": {
                    "type": "string",
                    "description": "可选：save_result=true 时的结果输出目录；不传时使用 BFE_OUT_DIR 或包同级 outputs/。",
                },
            },
            ["sql"],
        ),
    ])
    return defs


def _write_mcp_descriptor(
    skill_dir: Path, scenario: Scenario,
    domain: DomainKnowledge, dispatch: dict, mode: str,
) -> None:
    """生成 MCP 描述符和 Skill-only 使用的 system_prompt.md。"""
    card = _synthesize_capability_card(scenario, domain, dispatch, mode)
    ns = card["namespace"]
    skill_name = card["skill_name"]
    pkg_abs = str(skill_dir.resolve())

    tools = _mcp_tool_defs(ns, card, scenario)

    # 对外基址：已配置固定域名则用之，否则给占位符（真正的安装链接在配置面板按实际
    # 访问地址动态生成，见 playground_service.build_install_config）。
    base = settings.mcp_base_url or "http://<你的服务地址>:8000"
    sse_url = f"{base}/api/mcp/{scenario.id}/sse"

    # ---- mcp.json：能力卡片 + 命名空间化业务动作定义（兼容工具宿主）----
    mcp_doc = {
        "protocol": "mcp",
        "spec_version": "2024-11-05",
        "scenario_id": scenario.id,
        **card,
        # 兼容：远程 HTTP(SSE) 交付，仅用于平台托管验证或支持 MCP 的宿主桥接
        "server": {
            "transport": "sse",
            "url": sse_url,
            "note": "开发/测试用本服务实际访问地址；正式环境在 .env 配 MCP_PUBLIC_BASE_URL",
        },
        # 兼容：离线场景可用 stdio 本地起服务（需能访问本包目录）
        "server_stdio_fallback": {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "app.runtime.mcp_server", "--pkg", pkg_abs],
        },
        "tools": tools,
    }
    (skill_dir / "mcp.json").write_text(
        json.dumps(mcp_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- mcp_config.example.json：兼容 MCP 宿主的配置片段（远程 URL 化）----
    config_example = {
        "mcpServers": {
            f"bfe-{ns}": {
                "command": "npx",
                "args": ["-y", "mcp-remote", sse_url],
            }
        }
    }
    (skill_dir / "mcp_config.example.json").write_text(
        json.dumps(config_example, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _write_subagent_system_prompt(skill_dir, scenario, skill_name, card, tools)


def materialize_evolved_skill(scenario: Scenario, skill: Skill) -> Skill:
    skill_dir = store.skills_dir(scenario.id) / skill.skill_id
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    runner = f'''"""{skill.name}（进化技能）。

{skill.description}
"""

import pandas as pd


def run(tables: dict, **params) -> pd.DataFrame:
    """对输入的 {{表名: DataFrame}} 执行「{skill.name}」。"""
    # TODO: 依据业务口径实现
    return next(iter(tables.values())) if tables else pd.DataFrame()
'''
    (scripts_dir / "run.py").write_text(runner, encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        f"# {skill.name}\n\n{skill.description}\n\n## 接口\n`run(tables, **params) -> DataFrame`\n",
        encoding="utf-8",
    )
    return skill
