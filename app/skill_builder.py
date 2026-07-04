"""技能落盘（v1.0.5：生成 manifest.json + 验证通道就绪）。

# 核心原则
平台代码（此文件）**零业务术语**。不出现"违规"、"规则"、"就诊ID"、"医保"等任何
业务场景下才有的概念。所有业务语义通过 scenario 元数据传入，生成的文件里可以有。

# 生成产物（场景可移植的完整制品集）
    {scenario_id}/skills/
        SCENARIO_CONTEXT.md        ── 给任意 AI Agent 的完整提示词（可脱离平台使用）
        main_skill/
            SKILL.md               ── 人类可读的技能说明
            domain_knowledge.json  ── 数据字典 + ER + 字段语义
            output_specs.json      ── 产出规格（含 pipeline SQL）
            dispatch_config.json   ── 知识表驱动配置（若无知识表则为空 {}）
            schema.json            ── OpenAI function-calling 格式的工具定义
            scripts/
                skill_executor.py  ── 完全独立的执行脚本（无平台依赖）
        step_N_{name}/
            SKILL.md               ── 节点能力说明
            node.json              ── 节点元数据
            scripts/
                run.py             ── 节点单步执行器

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

from . import transform_builder
from .models import DomainKnowledge, FlowStep, KnowledgeSchemaMapping, Scenario, Skill, TableRole
from .storage import store

_MAIN_SKILL_ID = "main_skill"

_RESERVED_DIRS = {"uploads", "outputs", "skills"}
_SKILL_PREFIXES = ("main_", "step_")


def _slug(name: str) -> str:
    s = re.sub(r"[\s/\\]+", "_", str(name).strip())
    s = re.sub(r"[^0-9A-Za-z_一-鿿]", "", s)
    return s[:60] or "node"


# ===========================================================================
# 独立执行脚本模板（完全域无关；域特定值通过 JSON sidecar 注入）
# ===========================================================================
_EXECUTOR_TEMPLATE = r'''"""业务技能执行器（由「业务流逆向平台」自动生成）。

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
    cache = Path(str(path) + ".cache.pkl")
    try:
        if cache.exists() and cache.stat().st_mtime >= Path(path).stat().st_mtime:
            return pd.read_pickle(cache)
    except Exception:
        pass
    df = _read_raw(path, header_row)
    try:
        df.to_pickle(cache)
    except Exception:
        pass
    return df


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
_NODE = json.loads((_ROOT / "node.json").read_text(encoding="utf-8"))


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
    rows = search(keyword=kw, limit=lim)
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
    rows = list_all(limit=lim)
    cols = get_columns()
    print(f"知识表字段（{len(cols)} 列）：{cols}")
    print(f"前 {len(rows)} 条：")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''


# ===========================================================================
# 独立运行环境清单（脱离平台后，目标机器只需按此安装依赖，无需数据库服务）
# ===========================================================================
_REQUIREMENTS_TXT = """# 本技能包的运行依赖（脱离「业务流逆向平台」后独立安装）
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

_ROOT = Path(__file__).resolve().parent.parent
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
当 dispatch_map 中的模板不够细化时，此技能可进一步识别规则的语义模式，
辅助 skill_executor.py 选择更合适的 SQL 模板。

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
    """识别文本最可能的处理模式，返回 (pattern, signals, confidence)。"""
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
            "pattern": str,           # 识别到的处理模式
            "confidence": float,      # 0-1 置信度
            "signals": list[str],     # 匹配到的信号词
            "threshold": dict|None,   # 数值阈值（若有）
            "entities": list[str],    # 关键实体列表
            "override_template": str|None,  # 若与 dispatch_type 不同，给出建议覆盖值
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
    """生成 SCENARIO_CONTEXT.md：脱离平台也能让任意 Agent 理解并执行本场景的完整文档。"""
    tables_section = ""
    for t in domain.tables:
        col_list = ", ".join(c.name for c in t.columns[:20])
        if len(t.columns) > 20:
            col_list += "…"
        tables_section += (
            f"\n### `{t.table_name}`（{t.role}）\n"
            f"- 行数：{t.row_count or '未知'}\n"
            f"- 字段：{col_list}\n"
        )
        if t.columns:
            sem_lines = [f"  - `{c.name}`：{c.semantic}" for c in t.columns if c.semantic]
            if sem_lines:
                tables_section += "- 字段语义：\n" + "\n".join(sem_lines) + "\n"

    relations_section = ""
    if scenario.relations and scenario.relations.relations:
        for r in scenario.relations.relations:
            relations_section += (
                f"- `{r.from_table}.{r.from_column}` ←→ "
                f"`{r.to_table}.{r.to_column}`（置信度 {r.confidence:.0%}）\n"
            )
    if not relations_section:
        relations_section = "（暂未推导）"

    flow_section = ""
    if scenario.flow and scenario.flow.flow_steps:
        for s in scenario.flow.flow_steps:
            flow_section += (
                f"\n### 步骤 {s.step_id}：{s.step_name}\n"
                f"**目标**：{s.purpose or '—'}\n\n"
                f"**能力**：{s.capability or '—'}\n"
            )
            if s.sql:
                flow_section += f"\n```sql\n{s.sql.strip()}\n```\n"
            elif s.external_data_needed:
                flow_section += (
                    f"\n> ⚠️ 缺少外部数据：{s.external_data_needed}，当前步骤无法执行。\n"
                )

    outputs_section = ""
    for o in (scenario.outputs or []):
        outputs_section += f"\n- **{o.name}**（`{o.output_id}`，格式 `{o.fmt}`，状态 `{o.status}`）\n"

    dispatch_section = ""
    if dispatch and dispatch.get("knowledge_table"):
        dispatch_section = (
            f"\n## 知识表驱动配置\n"
            f"- 知识表：`{dispatch['knowledge_table']}`\n"
            f"- 分派键列：`{dispatch.get('dispatch_key_column', '—')}`\n"
        )
        if dispatch.get("dispatch_map"):
            dispatch_section += "- 分派值一览（仅供理解知识表全貌，不代表固定处理逻辑）：\n"
            for k, v in dispatch["dispatch_map"].items():
                dispatch_section += f"  - `{k}` → `{v}`\n"
        if dispatch.get("field_role_map"):
            dispatch_section += "- 知识字段 → 业务表字段对应关系（field_role_map）：\n"
            for k, v in dispatch["field_role_map"].items():
                dispatch_section += f"  - `{k}` → `{v}`\n"
        dispatch_section += (
            "\n> ⚠️ 知识表里每条规则的判断逻辑各不相同，本文档**不**预先给出每条规则的"
            "执行 SQL——请在读取知识行原文后，结合上面的字段对应关系与真实业务表 schema，"
            "自行推理并现场构造查询（真实业务规则可能有成百上千条，不可能逐条预置）。\n"
        )

    outputs_list = scenario.outputs or []
    first_output_id = outputs_list[0].output_id if outputs_list else "OUTPUT_ID"
    out_fmt = outputs_list[0].fmt if outputs_list else "xlsx"

    md = f"""# 业务场景：{scenario.name}

> 此文档由「业务流逆向平台」自动生成。将此文档提供给任意 AI Agent，Agent 即可理解本场景并处理新业务数据，无需依赖平台。

## 场景描述

{scenario.description or "（无描述）"}

---

## 一、数据表结构
{tables_section}
---

## 二、表间关系（ER）

{relations_section}

---

## 三、业务流程
{flow_section}
---

## 四、产出规格
{outputs_section}
{dispatch_section}
---

## 五、完整技能工具集

本场景 Skill 包含覆盖完整业务生命周期的技能：**新数据上传 → 校验 → 执行 → 产出结果**。

| 技能 | 脚本 | 功能 | 阶段 |
|------|------|------|------|
| 数据读取 | `skill_data_reader/scripts/skill_data_reader.py` | 读取任意格式业务数据并校验字段 | 上传 |
| 主技能（执行） | `main_skill/scripts/skill_executor.py` | 应用知识条目到业务数据产出结果 | 执行 |
| NL 规则解析 | `skill_nl_rule_parser/scripts/skill_nl_rule_parser.py` | 识别自然语言规则语义模式 | 执行辅助 |
| 搜索知识条目 | `utils/scripts/search_knowledge.py` | 在知识表中按关键词搜索 | 查询 |
| 浏览知识条目 | `utils/scripts/list_knowledge.py` | 列出知识表所有条目 | 查询 |

> ⚠️ **重要**：历史结果表仅作为输出列结构的格式模板（列名、列顺序）。
> 逆向推导完成后，可对知识表中**任意条目**执行，不限于历史结果中出现过的那几条。

---

## 六、完整业务处理流程（新数据 → 结果）

### 第一步：校验新业务数据

```bash
# 读取并校验数据结构
python skills/skill_data_reader/scripts/skill_data_reader.py /path/to/new_data
```

```python
import importlib.util
spec = importlib.util.spec_from_file_location("dr", "skills/skill_data_reader/scripts/skill_data_reader.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
frames = mod.read("/path/to/new_data")
check = mod.validate_schema(frames)
if not check["ok"]:
    print("数据问题：", check["warnings"])
```

### 第二步：了解可执行的知识条目

```bash
# 浏览所有知识条目
python skills/utils/scripts/list_knowledge.py

# 搜索特定条目
python skills/utils/scripts/search_knowledge.py "关键词"
```

### 第三步：执行产出

```bash
# 查看可用产出列表
python skills/main_skill/scripts/skill_executor.py

# 执行（将新数据文件放入 data_dir，文件名=表名）
python skills/main_skill/scripts/skill_executor.py \\
    {first_output_id} \\
    /path/to/new_data \\
    /path/to/output

# 带知识条目过滤（字符串关键词）
python skills/main_skill/scripts/skill_executor.py \\
    {first_output_id} /path/to/new_data /path/to/output '"关键词"'

# 带知识条目过滤（精确列匹配）
python skills/main_skill/scripts/skill_executor.py \\
    {first_output_id} /path/to/new_data /path/to/output '{{"col":"value"}}'

# 执行全部知识条目（无过滤）
python skills/main_skill/scripts/skill_executor.py \\
    {first_output_id} /path/to/new_data /path/to/output null
```

### 第四步（可选）：解析复杂 NL 规则

若知识表含自然语言描述规则，可用 NL 规则解析技能做运行时增强识别：

```python
import importlib.util
spec = importlib.util.spec_from_file_location("nlp", "skills/skill_nl_rule_parser/scripts/skill_nl_rule_parser.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
result = mod.parse_rule("不得同时申报A和B")
# result["pattern"] = "co_occurrence"
# result["entities"] = ["A", "B"]
```

### Agent 工具调用

参见 `main_skill/schema.json`，按 OpenAI function-calling 格式调用工具：
- `execute_{_slug(scenario.name)}` — 执行产出
- `search_knowledge_{_slug(scenario.name)}` — 搜索知识条目
- `list_knowledge_{_slug(scenario.name)}` — 浏览知识条目

```python
# 伪代码示例
import importlib.util
spec = importlib.util.spec_from_file_location("se", "skills/main_skill/scripts/skill_executor.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
result = mod.produce("{first_output_id}", "/path/to/new_data", "/path/to/output")
print(f"产出 {{result['rows']}} 行 → {{result['artifact']}}")
```

---

## 七、注意事项

- **环境准备**：`pip install -r requirements.txt`（同目录下）——只有 pandas/duckdb/openpyxl
  三个 pip 包，**不需要部署或连接任何数据库服务**。`duckdb` 是嵌入式 SQL 引擎（原理类似
  sqlite），`skill_executor.py` 里的 `duckdb.connect()` 打开的是进程内临时库，数据来自你
  指定目录下的业务文件（读成 DataFrame 后 `register()` 成可查询的表），跑完即释放，
  不产生需要另外维护的数据库文件。
- **新数据怎么放**：新建一个目录，把业务数据文件放进去，文件名（不含后缀）必须与
  下面「数据表结构」里列出的表名完全一致；然后 `produce(output_id, 这个目录, out_dir)`
  即可，无需回到本平台、无需网络、无需 AI。
- 支持 `.xlsx` / `.xls` / `.csv` / `.tsv` / `.json` 格式
- 执行结果文件保存在 `out_dir`，格式为 `{out_fmt}`
- 本场景运行无需 AI，所有决策在场景分析阶段已固化
- 历史结果表 = 输出列结构格式模板，不约束可执行的知识条目范围
- NL 规则解析（`skill_nl_rule_parser`）辅助提升复杂规则的识别精度，可选使用

---

## 八、作为 MCP Server 挂载到第三方（零改动）

除了上面的脚本/函数调用方式，本能力包同时符合 **MCP 标准**，可让任意支持 MCP 的
Agent 宿主（Claude Desktop / Cursor / Cline 等）**零改动代码**挂载：

1. 打开同目录 `mcp_config.example.json`，把里面的片段粘进你宿主的 MCP 配置；
2. 宿主启动后会自动发现本场景的命名空间化工具（形如 `<namespace>__execute` /
   `__query_data` / `__describe_schema` / `__search_knowledge` / `__list_knowledge`）；
3. 能力卡片（用途 / 何时使用 when_to_use / 何时不用 not_for / 工具清单）见同目录
   `mcp.json`，宿主的模型据此自主判断何时调用本能力，多个能力同时挂载也因命名空间
   前缀而互不冲突。

> 也可用 `python -m app.mcp_server --pkg <本目录>` 直接以 stdio 方式起一个 MCP Server。
> 若把 Anthropic Agent Skills 作为接入形态，本目录根部的 `SKILL.md` 已带触发描述，
> 直接丢进宿主的 skills 目录即可渐进披露。

---

*由「业务流逆向平台」生成 · 场景：{scenario.name}*
"""
    (skill_dir / "SCENARIO_CONTEXT.md").write_text(md, encoding="utf-8")


# ===========================================================================
# schema.json 生成（OpenAI function-calling 格式）
# ===========================================================================
def _generate_tool_schema(skill_dir: Path, scenario: Scenario) -> None:
    """生成 schema.json：任意 LLM Agent 可按此定义调用本场景的全套工具。

    包含三个工具：
    - execute（主技能：应用知识条目到业务数据产出结果）
    - search_knowledge（搜索知识表条目）
    - list_knowledge（浏览知识表条目）
    """
    outputs = scenario.outputs or []
    output_ids = [o.output_id for o in outputs]
    output_desc = "; ".join(f"{o.output_id}={o.name}" for o in outputs[:5])
    slug = _slug(scenario.name)

    execute_tool = {
        "name": f"execute_{slug}",
        "description": (
            f"执行业务场景「{scenario.name}」的数据处理，产出结果文件。\n"
            f"可用产出：{output_desc}\n"
            "params 过滤知识表条目：空=全量；字符串=关键词；对象={列名:值}=精确匹配。"
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
                "params": {
                    "type": ["string", "object", "null"],
                    "description": "过滤参数：字符串=关键词；对象={列名:列值}；null=全部条目",
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

    schema = {"tools": [execute_tool, search_tool, list_tool]}
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

    md = f"""---
name: {_MAIN_SKILL_ID}
scenario: {scenario.name}
mode: {mode_desc}
engine: duckdb
standalone: true
---

# 主技能

**执行模式**：{mode_desc}

## 接口

```python
produce(output_id, data_path, out_dir=None, params=None, max_rows=20000) -> dict
```

- `params`：`None`（全部）| `"关键词"` | `{{"col": "val"}}` | `{{"keyword": "xxx"}}`

## 数据表
{tables_hint}

## 可执行产出
{outputs_hint}

## 命令行
```bash
python scripts/skill_executor.py
python scripts/skill_executor.py {first_id} /data ./out
python scripts/skill_executor.py {first_id} /data ./out '"关键词"'
```

## 完整使用指南
参见同目录下的 `SCENARIO_CONTEXT.md`（含 ER、流程、独立运行方式）。
"""
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")


def _write_node_skill_md(skill_dir: Path, scenario: Scenario, step: FlowStep) -> None:
    in_lines = "\n".join("- " + s for s in step.data_in) if step.data_in else "- （未填写）"
    out_lines = "\n".join("- " + s for s in step.data_out) if step.data_out else "- （未填写）"
    md = f"""---
name: step_{step.step_id}_{_slug(step.step_name)}
scenario: {scenario.name}
step_id: {step.step_id}
template_kind: {step.template_kind or "—"}
status: {step.status}
---

# {step.step_name}

## 目标（purpose）
{step.purpose or "（未填写）"}

## 能力（capability）
{step.capability or "（未填写）"}

## 数据输入
{in_lines}

## 数据输出
{out_lines}

## 模板 & 参数
- 模板：`{step.template_kind or step.strategy or "—"}`
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
    """生成节点子技能 + 主技能 + 场景上下文文档。保留用户进化技能。"""
    base = store.skills_dir(scenario.id)
    evolved = [s for s in scenario.skills if s.is_evolved]
    evolved_ids = {s.skill_id for s in evolved}

    for child in base.iterdir():
        if not child.is_dir():
            continue
        if child.name in _RESERVED_DIRS:
            continue
        # main_/step_ prefixed dirs and utils dir are regenerated each time
        is_generated = (any(child.name.startswith(p) for p in _SKILL_PREFIXES)
                        or child.name == "utils")
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
    (main_dir / "dispatch_config.json").write_text(
        json.dumps(dispatch, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (main_scripts / "skill_executor.py").write_text(_EXECUTOR_TEMPLATE, encoding="utf-8")

    _write_main_skill_md(main_dir, scenario, domain, dispatch)
    _generate_tool_schema(main_dir, scenario)

    # ---- 场景上下文（给任意 Agent 的完整提示词）----
    _generate_scenario_context(base, scenario, domain, dispatch)

    # ---- 工具脚本（独立可执行的知识表查询工具）----
    utils_dir = base / "utils" / "scripts"
    utils_dir.mkdir(parents=True, exist_ok=True)
    (utils_dir / "search_knowledge.py").write_text(_SEARCH_KNOWLEDGE_TEMPLATE, encoding="utf-8")
    (utils_dir / "list_knowledge.py").write_text(_LIST_KNOWLEDGE_TEMPLATE, encoding="utf-8")

    # ---- 基础技能：数据读取器 ----
    reader_dir = base / "skill_data_reader"
    reader_scripts = reader_dir / "scripts"
    reader_scripts.mkdir(parents=True, exist_ok=True)
    (reader_scripts / "skill_data_reader.py").write_text(_DATA_READER_TEMPLATE, encoding="utf-8")
    (reader_dir / "SKILL.md").write_text(
        f"""---
name: skill_data_reader
scenario: {scenario.name}
standalone: true
---

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

    # ---- 基础技能：NL 规则解析器 ----
    nl_parser_dir = base / "skill_nl_rule_parser"
    nl_parser_scripts = nl_parser_dir / "scripts"
    nl_parser_scripts.mkdir(parents=True, exist_ok=True)
    (nl_parser_scripts / "skill_nl_rule_parser.py").write_text(
        _NL_RULE_PARSER_TEMPLATE, encoding="utf-8"
    )
    (nl_parser_dir / "SKILL.md").write_text(
        f"""---
name: skill_nl_rule_parser
scenario: {scenario.name}
standalone: true
---

# 自然语言规则解析

**功能**：对知识表中的自然语言规则文本做运行时语义识别，
辅助 `skill_executor.py` 选择正确的 SQL 模板处理每条规则。

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
- `_override`：若与 dispatch_map 不一致，给出建议覆盖模板

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
        (sdir / "node.json").write_text(s.model_dump_json(indent=2), encoding="utf-8")
        (scripts / "run.py").write_text(_NODE_RUNNER_TEMPLATE, encoding="utf-8")
        _write_node_skill_md(sdir, scenario, s)
        node_skills.append(Skill(
            skill_id=sid,
            name=s.step_name,
            operation=s.operation,
            description=f"{s.purpose or s.step_name}（模板：{s.template_kind or s.strategy or '—'}）",
            step_id=s.step_id,
            is_main=False,
            path=str(sdir),
            capability=s.capability,
            status="generated" if s.status != "blocked" else "blocked",
        ))

    # ---- 工具技能（知识表查询工具，独立可执行）----
    utils_skill = Skill(
        skill_id="utils",
        name="知识表工具（search/list）",
        operation="QUERY",
        description=(
            "独立可执行的知识表查询工具：search_knowledge.py（关键词搜索）+ "
            "list_knowledge.py（全览条目）。脱离平台直接运行。"
        ),
        is_main=False,
        path=str(base / "utils"),
        capability="search_knowledge(keyword, limit) / list_all(limit) → list[dict]",
    )

    # ---- 基础技能（完整业务生命周期所需）----
    reader_skill = Skill(
        skill_id="skill_data_reader",
        name="数据读取与校验",
        operation="READ",
        description="读取任意格式业务数据（xlsx/csv/tsv/json），自动探测编码，校验字段完整性。",
        is_main=False,
        path=str(reader_dir),
        capability="read(data_path) → {表名: DataFrame}；validate_schema(frames) → {ok, warnings}",
    )
    nl_parser_skill = Skill(
        skill_id="skill_nl_rule_parser",
        name="NL 规则解析",
        operation="PARSE",
        description=(
            "对知识表 NL 规则文本做运行时语义识别，识别模式（共存/频次/阈值/互斥/关键词），"
            "辅助 skill_executor 选择正确 SQL 模板。"
        ),
        is_main=False,
        path=str(nl_parser_dir),
        capability="parse_rule(text) → {pattern, confidence, threshold, entities}",
    )

    all_skills = [main_skill, utils_skill, reader_skill, nl_parser_skill, *node_skills, *evolved]

    # ---- manifest.json（验证通道发现入口）----
    _write_manifest(base, scenario, all_skills, dispatch, mode)

    # ---- MCP 能力包（第三方零改动挂载：mcp.json + 根 SKILL.md + 粘贴即用配置）----
    _write_mcp_descriptor(base, scenario, domain, dispatch, mode)

    return all_skills


# ===========================================================================
# manifest.json 生成（验证通道发现与调用的统一入口）
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
        elif s.skill_id == "utils":
            entry["entry_points"] = {
                "search": "utils/scripts/search_knowledge.py",
                "list": "utils/scripts/list_knowledge.py",
            }
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

    manifest = {
        "version": "1.0.5",
        "generated_at": now,
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "namespace": _ascii_namespace(scenario.name, scenario.id),
        "description": scenario.description or "",
        "execution_mode": mode,
        "has_knowledge_table": bool(dispatch.get("knowledge_table")),
        "knowledge_table": dispatch.get("knowledge_table", ""),
        # MCP 能力包指针（详见同目录 mcp.json / SKILL.md / mcp_config.example.json）
        "mcp": {
            "descriptor": "mcp.json",
            "config_example": "mcp_config.example.json",
            "skill_md": "SKILL.md",
        },
        "skills": skills_index,
        "outputs": outputs_summary,
        "tools": tools_def,
        "entry_points": {
            "executor": "main_skill/scripts/skill_executor.py",
            "search_knowledge": "utils/scripts/search_knowledge.py",
            "list_knowledge": "utils/scripts/list_knowledge.py",
            "data_reader": "skill_data_reader/scripts/skill_data_reader.py",
            "nl_rule_parser": "skill_nl_rule_parser/scripts/skill_nl_rule_parser.py",
            "context_doc": "SCENARIO_CONTEXT.md",
            "requirements": "requirements.txt",
        },
        "verify_instructions": (
            "将本场景 skills/ 目录挂载到验证 Agent，Agent 即可调用上述工具执行业务场景。"
            "执行无需依赖平台任何代码：`pip install -r requirements.txt`（pandas/duckdb/openpyxl，"
            "duckdb 是嵌入式引擎，不需要数据库服务）即可独立运行。"
        ),
    }

    (skill_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ===========================================================================
# MCP 能力包生成（第三方零改动挂载的统一标准入口）
# ===========================================================================
# 命名空间化工具的固定动作集：任意场景包都暴露这 5 个 action，
# 工具名 = f"{namespace}__{action}"，用命名空间前缀保证多场景同时挂载不撞名。
_MCP_ACTIONS = ("describe_schema", "list_knowledge", "search_knowledge", "execute", "query_data")


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
        "display_name": name,
        "summary": summary,
        "when_to_use": when_to_use,
        "not_for": not_for,
        "keywords": keywords,
        "required_tables": required_tables,
        "knowledge_table": knowledge_table,
        "execution_mode": mode,
    }


def _mcp_tool_defs(ns: str, card: dict, scenario: Scenario) -> list[dict]:
    """构建命名空间化 MCP 工具定义（name / action / description / inputSchema）。

    对应验证 Agent 已有的能力，但去掉 data_dir/out_dir 这类第三方不该关心的内部路径参数
    ——运行时由 scenario_runtime 用包内默认目录兜底。
    """
    name = scenario.name
    output_ids = [o.output_id for o in (scenario.outputs or [])]
    trigger = f"（本工具属于「{name}」能力包；仅当用户诉求匹配该能力时才调用）"

    def tool(action: str, desc: str, props: dict, required: list[str]) -> dict:
        return {
            "name": f"{ns}__{action}",
            "action": action,
            "description": desc + trigger,
            "inputSchema": {"type": "object", "properties": props, "required": required},
        }

    defs = [
        tool(
            "describe_schema",
            f"获取「{name}」场景的完整表结构：字段名/类型/业务语义 + 表间关联(ER) + 知识表分派结构。"
            "构造 query_data 的 SQL 前应先调用一次。",
            {}, [],
        ),
        tool(
            "list_knowledge",
            f"浏览「{name}」场景知识表的条目（分页），了解可执行的知识条目范围。",
            {"limit": {"type": "integer", "description": "返回条数上限（默认 50）", "default": 50}},
            [],
        ),
        tool(
            "search_knowledge",
            f"在「{name}」场景知识表中按关键词搜索条目。",
            {
                "keyword": {"type": "string", "description": "搜索关键词（空=返回前 N 条）"},
                "limit": {"type": "integer", "description": "返回条数上限（默认 20）", "default": 20},
            },
            [],
        ),
        tool(
            "execute",
            f"执行「{name}」场景的数据处理并产出结果文件。知识驱动模式下只返回命中的知识行原文，"
            "需据此再用 query_data 逐条落地查询。",
            {
                "output_id": {
                    "type": "string",
                    "description": f"产出ID，可选值：{output_ids}",
                    **({"enum": output_ids} if output_ids else {}),
                },
                "params": {
                    "type": ["string", "object", "null"],
                    "description": "知识条目过滤：空=全量；字符串=关键词；对象={列名:值}=精确匹配",
                },
                "max_rows": {"type": "integer", "description": "最大产出行数（默认 20000）", "default": 20000},
            },
            ["output_id"],
        ),
        tool(
            "query_data",
            f"对「{name}」场景已上传的业务数据执行 DuckDB SQL（任意表/字段/多表 JOIN/聚合）。"
            "表名用双引号，例如：SELECT * FROM \"表名\" LIMIT 10。",
            {"sql": {"type": "string", "description": "要执行的 DuckDB SQL"}},
            ["sql"],
        ),
    ]
    return defs


def _write_mcp_descriptor(
    skill_dir: Path, scenario: Scenario,
    domain: DomainKnowledge, dispatch: dict, mode: str,
) -> None:
    """生成 MCP 标准能力包三件套：mcp.json + 根 SKILL.md + mcp_config.example.json。

    这三样让第三方 Agent 宿主（Claude Desktop / Cursor / Cline 等）能像配置一个普通
    MCP Server 一样，粘贴一段配置就挂载本场景能力，全程零改动第三方代码。
    """
    card = _synthesize_capability_card(scenario, domain, dispatch, mode)
    ns = card["namespace"]
    pkg_abs = str(skill_dir.resolve())

    tools = _mcp_tool_defs(ns, card, scenario)

    # ---- mcp.json：能力卡片 + 命名空间化工具定义 ----
    mcp_doc = {
        "protocol": "mcp",
        "spec_version": "2024-11-05",
        "scenario_id": scenario.id,
        **card,
        "server": {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "app.mcp_server", "--pkg", pkg_abs],
        },
        "tools": tools,
    }
    (skill_dir / "mcp.json").write_text(
        json.dumps(mcp_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- mcp_config.example.json：粘贴即用的第三方宿主配置片段 ----
    config_example = {
        "mcpServers": {
            f"bfe-{ns}": {
                "command": "python",
                "args": ["-m", "app.mcp_server", "--pkg", pkg_abs],
            }
        }
    }
    (skill_dir / "mcp_config.example.json").write_text(
        json.dumps(config_example, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 根 SKILL.md：Anthropic Agent Skills 兼容（frontmatter 触发语做渐进披露）----
    when_summary = "；".join(card["when_to_use"])
    not_summary = "；".join(card["not_for"])
    desc_line = f"{card['summary']} Use this skill when: {when_summary}. Do NOT use for: {not_summary}."
    # frontmatter 单行 description，避免换行破坏 YAML
    desc_line = desc_line.replace("\n", " ").replace('"', "'")
    tool_lines = "\n".join(f"- `{t['name']}` — {t['description']}" for t in tools)
    skill_md = f"""---
name: {ns}
description: "{desc_line}"
---

# {scenario.name}

{card['summary']}

## 何时使用本能力
{chr(10).join('- ' + w for w in card['when_to_use'])}

## 何时不要使用
{chr(10).join('- ' + w for w in card['not_for'])}

## 必需数据表
{('、'.join(card['required_tables'])) or '（无特定要求）'}

## 提供的工具（命名空间：`{ns}`）
{tool_lines}

## 两种挂载方式
1. **作为 MCP Server（推荐，零改动）**：把 `mcp_config.example.json` 的内容粘进你的
   Agent 宿主 MCP 配置即可，宿主会自动发现上面这些 `{ns}__*` 工具。
2. **作为独立脚本 / Agent Skill**：详见同目录 `SCENARIO_CONTEXT.md`（完整表结构、ER、
   业务流程、独立运行方式）与 `manifest.json`。

> 本能力包完全独立于生成它的平台：`pip install -r requirements.txt`
> （pandas / duckdb / openpyxl）即可运行，无需回连平台、无需数据库服务。
"""
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")


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
