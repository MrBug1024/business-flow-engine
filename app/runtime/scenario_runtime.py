"""场景能力包运行时（平台无关执行核心）。

本模块是标准 Skill 发布包、兼容 MCP Server 与 `playground_agent.py`
（通用 Agent 平台）的**公共执行源**。它只接收「能力包目录」（即某场景的
`skills/` 目录）作为输入，**不依赖平台的 `store` / 数据库 / 任何全局状态**——
这就在代码层面证明了「蒸馏出的业务能力可以脱离平台独立执行」。

一个能力包目录形如：
    <pkg>/
      mcp.json                    能力卡片 + 命名空间化工具定义
      manifest.json
      SKILL.md
      main_skill/
        domain_knowledge.json     表结构 + ER + 知识表结构
        output_specs.json
        dispatch_config.json
        scripts/skill_executor.py
      tools/knowledge/{search,list}_knowledge.py

附件上传、文件预览和结果文件下载由宿主 Agent 平台处理。本运行时只在本地脚本/Agent 平台
场景下支持 `data_dir`，用于读取宿主已经整理好的业务数据目录。
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_DATA_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xls", ".json")

# 与 skill_builder._MCP_ACTIONS 对应：一个能力包对外暴露的固定动作集
ACTIONS = (
    "describe_capability",
    "describe_schema",
    "list_outputs",
    "list_knowledge",
    "search_knowledge",
    "execute",
    "query_data",
)


# ===========================================================================
# 能力包封装
# ===========================================================================
@dataclass
class ScenarioPackage:
    """一个已生成的场景能力包（skills/ 目录）的只读视图。"""

    pkg_dir: Path
    card: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)

    @classmethod
    def load(cls, pkg_dir: str | Path) -> "ScenarioPackage":
        pkg = Path(pkg_dir).resolve()
        card = _jload(pkg / "mcp.json") or {}
        manifest = _jload(pkg / "manifest.json") or {}
        return cls(pkg_dir=pkg, card=card, manifest=manifest)

    # -- 元信息 --
    @property
    def namespace(self) -> str:
        return self.card.get("namespace") or self.manifest.get("namespace") or "scn"

    @property
    def display_name(self) -> str:
        return (self.card.get("display_name")
                or self.manifest.get("scenario_name") or self.namespace)

    @property
    def summary(self) -> str:
        return self.card.get("summary", "")

    @property
    def when_to_use(self) -> list[str]:
        return list(self.card.get("when_to_use", []))

    @property
    def not_for(self) -> list[str]:
        return list(self.card.get("not_for", []))

    @property
    def tools(self) -> list[dict]:
        return list(self.card.get("tools", []))

    @property
    def execution_mode(self) -> str:
        return self.card.get("execution_mode") or self.manifest.get("execution_mode", "")

    def is_ready(self) -> bool:
        return (self.pkg_dir / "main_skill").exists() and bool(self.card or self.manifest)

    # -- 内部路径 --
    def _main_skill(self) -> Path:
        return self.pkg_dir / "main_skill"

    def domain(self) -> dict:
        return _jload(self._main_skill() / "domain_knowledge.json") or {}

    def dispatch(self) -> dict:
        return _jload(self._main_skill() / "dispatch_config.json") or {}

    def output_specs(self) -> list[dict]:
        return (_jload(self._main_skill() / "output_specs.json") or {}).get("outputs", [])

    def default_data_dir(self) -> Optional[Path]:
        """未显式给 data_dir 时的回退。

        Agent 平台从 release 包加载时，验证数据仍在场景目录；本地脚本场景可通过
        BFE_DATA_DIR 或显式 data_dir 指定。这里保留 /data 作为通用容器/CI 兼容路径，
        但业务 Skill 不要求第三方平台提供 MCP/Docker 文件写入能力。
        """
        candidates: list[Path] = []
        env_dir = os.getenv("BFE_DATA_DIR", "").strip()
        if env_dir:
            candidates.append(Path(env_dir))

        candidates.extend([
            self.pkg_dir / "data",
            self.pkg_dir.parent / "data",
            self.pkg_dir.parent / "verify_uploads",
            self.pkg_dir.parent / "uploads",
            self.pkg_dir.parent.parent / "verify_uploads",
            self.pkg_dir.parent.parent / "uploads",
            Path("/data"),
        ])
        for d in candidates:
            if _has_data_files(d):
                return d
        return None

    def default_out_dir(self) -> Path:
        env_dir = os.getenv("BFE_OUT_DIR", "").strip()
        if env_dir:
            return Path(env_dir)
        return self.pkg_dir.parent / "outputs"


# ===========================================================================
# 工具函数
# ===========================================================================
def _jload(path: Path) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _has_data_files(d: Path) -> bool:
    return d.exists() and any(f.suffix.lower() in _DATA_SUFFIXES for f in d.iterdir())


def _load_module(script: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve_data_dir(pkg: ScenarioPackage, data_dir: Optional[str]) -> Optional[Path]:
    if data_dir and str(data_dir).strip():
        return Path(str(data_dir).strip())
    return pkg.default_data_dir()


# ===========================================================================
# 五个对外动作（返回给 LLM 的字符串结果，与验证 Agent 行为对齐）
# ===========================================================================
def describe_capability(pkg: ScenarioPackage) -> str:
    """Return a compact onboarding card for third-party agents."""
    domain = pkg.domain()
    tables = domain.get("tables", []) if isinstance(domain, dict) else []
    outputs = pkg.output_specs()
    required = list(pkg.card.get("required_tables") or [])
    knowledge_table = pkg.card.get("knowledge_table", "")
    tools = [
        {
            "name": t.get("name"),
            "action": t.get("action"),
            "description": t.get("description", ""),
        }
        for t in pkg.tools
    ]
    table_brief = []
    for table in tables:
        table_name = table.get("table_name", "")
        columns = table.get("columns", []) or []
        important_columns = [
            c.get("name") for c in columns
            if c.get("semantic_role") in {"PK", "FK", "TIME", "METRIC", "NL_TEXT", "CATEGORY"}
        ][:12]
        table_brief.append({
            "name": table_name,
            "role": table.get("role", "input"),
            "required": table_name in required,
            "columns_count": len(columns),
            "important_columns": important_columns,
        })

    payload = {
        "scope": (
            "This is a standalone published business scenario package. It can describe and execute only "
            "the scenario(s) mounted in this MCP server; it cannot enumerate the distillation platform "
            "database or unrelated platform scenarios."
        ),
        "scenario_name": pkg.display_name,
        "namespace": pkg.namespace,
        "skill_name": pkg.card.get("skill_name", ""),
        "summary": pkg.summary,
        "when_to_use": pkg.when_to_use,
        "not_for": pkg.not_for,
        "required_business_data": {
            "required_tables": required,
            "knowledge_table": knowledge_table,
            "mount_or_pass_data_dir": (
                "When running local scripts, place business files in the data directory or set BFE_DATA_DIR. "
                "On third-party Agent platforms, file upload/download is expected to be handled by the host platform."
            ),
            "tables": table_brief,
        },
        "outputs": [
            {
                "output_id": o.get("output_id"),
                "name": o.get("name"),
                "format": o.get("fmt", "csv"),
                "status": o.get("status", ""),
                "capability": o.get("capability", ""),
            }
            for o in outputs
        ],
        "tools": tools,
        "recommended_workflow": [
            f"Call {pkg.namespace}__describe_capability first when the host is unsure what this package does.",
            f"Call {pkg.namespace}__describe_schema before writing SQL or checking field names.",
            f"Call {pkg.namespace}__list_outputs to see executable outputs.",
            f"Use {pkg.namespace}__list_knowledge or {pkg.namespace}__search_knowledge when the scenario is knowledge/rule driven.",
            f"Use {pkg.namespace}__query_data for ad hoc data checks and {pkg.namespace}__execute for scenario outputs.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def describe_schema(pkg: ScenarioPackage) -> str:
    """完整表结构：字段名/类型/业务语义 + 表间关联(ER) + 知识表分派结构。"""
    domain = pkg.domain()
    if not domain:
        return "❌ domain_knowledge.json 不存在或为空，能力包可能不完整。"

    lines = [f"# 场景「{domain.get('scenario', pkg.display_name)}」完整表结构\n"]
    for t in domain.get("tables", []):
        role = t.get("role", "input")
        lines.append(f"## 表「{t.get('table_name')}」（角色:{role}，约 {t.get('row_count', '?')} 行）")
        for c in t.get("columns", []):
            sem = f" — {c.get('semantic')}" if c.get("semantic") else ""
            rtag = c.get("semantic_role", "")
            tag = "|" + rtag if rtag and rtag != "UNKNOWN" else ""
            lines.append(f"  - `{c.get('name')}` ({c.get('dtype', '?')}{tag}){sem}")
        lines.append("")

    relations = domain.get("relations", [])
    lines.append("## 表间关联（JOIN 键）")
    if relations:
        for r in relations:
            lines.append(
                f"- `{r.get('from_table')}`.`{r.get('from_column')}` "
                f"↔ `{r.get('to_table')}`.`{r.get('to_column')}`"
                f"（{r.get('relation_type', '?')}，置信度 {r.get('confidence', 0):.0%}）"
            )
    else:
        lines.append("（本场景未推导出表间关联，多表联查前请先用少量数据自行核对关联键）")

    ks = domain.get("knowledge_schema") or domain.get("rule_schema")
    if ks:
        lines.append("\n## 知识表结构（若要按知识条目驱动查询）")
        kt = ks.get("knowledge_table") or ks.get("rule_table")
        lines.append(f"- 知识表：`{kt}`")
        dk = ks.get("dispatch_key_column") or ks.get("discriminator_column")
        if dk:
            lines.append(f"- 分派键列：`{dk}`")
        dmap = ks.get("dispatch_map") or ks.get("discriminator_to_template") or {}
        if dmap:
            lines.append(f"- 分派值一览（仅供理解知识表全貌，不代表固定处理逻辑）：{dmap}")
        frm = ks.get("field_role_map") or {}
        if frm:
            lines.append(f"- 知识字段 → 业务表字段对应关系：{frm}")
        lines.append(
            "> 每条知识/规则行的具体判断逻辑各不相同，本场景不预置任何执行 SQL——"
            "请先用 search_knowledge/list_knowledge 读到规则原文，自行理解判断逻辑后"
            "再用 query_data 构造针对该规则的查询。"
        )

    lines.append(
        "\n> 提示：query_data 的表名与上面列出的表名完全一致（用双引号引用）。"
        "本场景支持对任意上述表、任意字段、任意多表 JOIN/聚合发起查询。"
    )
    return "\n".join(lines)


def list_outputs(pkg: ScenarioPackage) -> str:
    specs = pkg.output_specs()
    if not specs:
        return "❌ 能力包中无可用产出规格（output_specs.json 为空）。"
    lines = [f"本场景共 {len(specs)} 个可执行产出：\n"]
    for s in specs:
        lines.append(
            f"- **{s.get('output_id')}**（{s.get('name', '?')}）"
            f" 格式:{s.get('fmt','csv')} 状态:{s.get('status','?')}"
        )
    if pkg.execution_mode:
        lines.append(f"\n执行模式：{pkg.execution_mode}")
    return "\n".join(lines)


def list_knowledge(pkg: ScenarioPackage, limit: int = 50, data_dir: Optional[str] = None) -> str:
    script = pkg.pkg_dir / "tools" / "knowledge" / "list_knowledge.py"
    if not script.exists():
        return "❌ list_knowledge 脚本不存在，能力包可能不完整。"
    dd = _resolve_data_dir(pkg, data_dir)
    if dd is None:
        return "❌ 未指定业务数据目录，且包内无默认数据。请通过 data_dir 提供包含知识表文件的目录。"
    try:
        mod = _load_module(script, "_lk")
        rows = mod.list_all(limit=int(limit), data_dir=str(dd))
        cols = mod.get_columns(data_dir=str(dd))
    except Exception as exc:
        return f"❌ 读取知识表失败：{type(exc).__name__}: {exc}"
    if not rows:
        return "知识表为空或未找到对应数据文件。"
    preview = _df_preview(rows)
    dispatch_col = pkg.dispatch().get("dispatch_key_column", "")
    hint = f"（分派键列：`{dispatch_col}`）" if dispatch_col else ""
    return (f"知识表字段（{len(cols)} 列）{hint}：{cols}\n\n"
            f"前 {len(rows)} 条（共限制 {limit} 条）：\n{preview}")


def search_knowledge(pkg: ScenarioPackage, keyword: str = "", limit: int = 20,
                     data_dir: Optional[str] = None) -> str:
    script = pkg.pkg_dir / "tools" / "knowledge" / "search_knowledge.py"
    if not script.exists():
        return "❌ search_knowledge 脚本不存在，能力包可能不完整。"
    dd = _resolve_data_dir(pkg, data_dir)
    if dd is None:
        return "❌ 未指定业务数据目录，且包内无默认数据。请通过 data_dir 提供包含知识表文件的目录。"
    try:
        mod = _load_module(script, "_sk")
        rows = mod.search(keyword=(keyword or "").strip(), limit=int(limit), data_dir=str(dd))
    except Exception as exc:
        return f"❌ 搜索知识表失败：{type(exc).__name__}: {exc}"
    if not rows:
        return f"未找到匹配「{keyword}」的知识条目。"
    return f"找到 {len(rows)} 条匹配「{keyword}」的知识条目：\n{_df_preview(rows)}"


def execute(pkg: ScenarioPackage, output_id: str, params: Any = "",
            data_dir: Optional[str] = None, out_dir: Optional[str] = None,
            max_rows: int = 20000) -> str:
    """执行主技能产出。行为与验证 Agent 的 execute_skill 对齐。"""
    executor = pkg._main_skill() / "scripts" / "skill_executor.py"
    if not executor.exists():
        return "❌ 主技能执行脚本不存在，能力包不完整。"
    dd = _resolve_data_dir(pkg, data_dir)
    if dd is None:
        return "❌ 未指定业务数据目录，且包内无默认数据。请通过 data_dir 提供包含业务数据文件的目录。"
    od = Path(out_dir.strip()) if isinstance(out_dir, str) and out_dir.strip() else pkg.default_out_dir()

    rf = params
    if isinstance(params, str):
        s = params.strip()
        if not s:
            rf = None
        else:
            try:
                rf = json.loads(s)
            except Exception:
                rf = s

    t0 = time.perf_counter()
    try:
        mod = _load_module(executor, "_skill_exec")
        result = mod.produce(output_id, str(dd), out_dir=str(od), params=rf, max_rows=int(max_rows))
    except Exception as exc:
        return (f"❌ 技能执行失败：{type(exc).__name__}: {exc}\n"
                f"（output_id={output_id!r}，params={rf!r}，数据目录={dd}）")
    elapsed = time.perf_counter() - t0
    mode = result.get("mode", "")

    if mode == "knowledge_rows":
        matched = result.get("matched_rows", [])
        matched_count = result.get("matched_count", len(matched))
        guidance = result.get("guidance", "")
        preview = _df_preview(matched[:20], width=3000)
        batch_hint = ""
        if matched_count > 10:
            batch_hint = (
                f"\n\n⚠️ 命中 {matched_count} 条规则，逐条落地查询耗时会很长。"
                "请先告知用户总量，本轮只处理前 10 条（或让用户缩小范围），再询问是否继续。"
            )
        return (
            f"📋 命中知识表「{result.get('knowledge_table', '?')}」{matched_count} 条规则/知识行"
            f"（筛选耗时 {elapsed:.1f}s），尚未据此计算最终业务结果。\n\n"
            f"{guidance}{batch_hint}\n\n命中的知识行预览（≤20 条）：\n{preview}"
        )

    rows = result.get("rows", 0)
    artifact = result.get("artifact", "")
    status = "✅ 有产出" if rows > 0 else "⚠️ 0 行输出（可能过滤条件不匹配）"
    summary = f"{status}｜产出 {rows} 行｜耗时 {elapsed:.1f}s｜数据目录：{dd}"
    if artifact:
        summary += f"\n产出文件：{Path(artifact).name}"
        try:
            import pandas as pd
            ap = Path(artifact)
            if ap.suffix.lower() in (".csv", ".tsv") and rows > 0:
                sep = "\t" if ap.suffix.lower() == ".tsv" else ","
                head_df = pd.read_csv(ap, sep=sep, nrows=10, encoding="utf-8-sig")
                summary += f"\n产出预览（≤10 行）：\n{head_df.to_markdown(index=False)[:1500]}"
        except Exception:
            pass
    if rows == 0:
        summary += "\n\n0 行 ≠ 执行失败：请向用户说明是过滤条件未命中还是数据本身无记录。"
    return summary


def query_data(
    pkg: ScenarioPackage,
    sql: str,
    data_dir: Optional[str] = None,
    save_result: bool = False,
    out_dir: Optional[str] = None,
) -> str:
    """对能力包引用的业务数据执行 DuckDB SQL（按需加载 SQL 实际引用到的表）。"""
    sql = (sql or "").strip()
    if not sql:
        return "❌ sql 参数不能为空。"
    domain = pkg.domain()
    tables_meta = domain.get("tables", [])
    if not tables_meta:
        return "❌ domain_knowledge.json 不存在或无表信息，能力包可能不完整。"
    dd = _resolve_data_dir(pkg, data_dir)
    if dd is None:
        return "❌ 未指定业务数据目录，且包内无默认数据。请通过 data_dir 提供包含业务数据文件的目录。"

    all_names = [t.get("table_name", "") for t in tables_meta if t.get("table_name")]
    referenced = {n for n in all_names if n and n in sql}
    if not referenced:
        return ("❌ SQL 中未识别到本场景的任何表名，未执行查询。\n"
                f"可用表：{'、'.join(all_names) or '（无）'}\n"
                "表名必须与 describe_schema 列出的完全一致（建议用双引号引用）。")

    # 平台内使用 app.data，交付包内使用同目录 bfe_runtime.table_io。
    try:
        from .table_io import load_full_frame_cached
    except Exception:  # noqa: BLE001
        from app.data.table_io import load_full_frame_cached

    loaded: list[str] = []
    load_notes: list[str] = []
    t0 = time.perf_counter()
    try:
        import duckdb
        con = duckdb.connect()
        try:
            for t in tables_meta:
                name = t.get("table_name", "")
                if name not in referenced:
                    continue
                candidates = [p for p in dd.glob(f"{name}.*")
                              if p.suffix.lower() in _DATA_SUFFIXES]
                fpath = t.get("file_path", "")
                if not candidates and fpath and Path(fpath).exists():
                    candidates = [Path(fpath)]
                if not candidates:
                    return (f"❌ 未执行查询：找不到表「{name}」对应的数据文件（查找目录：{dd}）。\n"
                            "请确认数据文件名（不含后缀）与表名一致。")
                tl = time.perf_counter()
                df = load_full_frame_cached(str(candidates[0]))
                con.register(name, df)
                loaded.append(name)
                load_notes.append(f"{name}（{len(df)} 行，加载 {time.perf_counter() - tl:.1f}s）")
            result = con.execute(sql).fetchdf()
        finally:
            con.close()
    except Exception as exc:
        return (f"❌ SQL 执行失败：{type(exc).__name__}: {exc}\n"
                f"已加载表：{loaded or '（无）'}\n表名/字段名请以 describe_schema 的输出为准。")

    elapsed = time.perf_counter() - t0
    n_rows, n_cols = len(result), len(result.columns)
    artifact_name = ""
    if save_result:
        try:
            target_out_dir = Path(out_dir.strip()) if isinstance(out_dir, str) and out_dir.strip() else pkg.default_out_dir()
            target_out_dir.mkdir(parents=True, exist_ok=True)
            artifact = target_out_dir / f"query_{int(time.time())}.csv"
            result.to_csv(artifact, index=False, encoding="utf-8-sig")
            artifact_name = artifact.name
        except Exception:
            pass

    preview = _df_preview(result, width=2000, head=20)
    head = (f"✅ 查询完成：{n_rows} 行 × {n_cols} 列，耗时 {elapsed:.1f}s"
            + (f"，落盘：{artifact_name}" if artifact_name else ""))
    note = "" if n_rows else (
        "\n⚠️ 查询成功但结果为 0 行——这不是执行失败，而是没有数据满足当前条件。"
        "请核对 WHERE 条件的字段名/取值是否与真实数据一致。")
    return (f"{head}\n数据来源：{dd}\n已加载表：{'；'.join(load_notes)}{note}\n预览（≤20行）：\n{preview}")


# ===========================================================================
# 统一分发（供 mcp_server / playground_agent 调用）
# ===========================================================================
def call_action(pkg: ScenarioPackage, action: str, args: dict) -> str:
    """按 action 名分发到对应函数。args 为工具入参 dict。"""
    args = args or {}
    if action == "describe_capability":
        return describe_capability(pkg)
    if action == "describe_schema":
        return describe_schema(pkg)
    if action == "list_outputs":
        return list_outputs(pkg)
    if action == "list_knowledge":
        return list_knowledge(pkg, limit=args.get("limit", 50), data_dir=args.get("data_dir"))
    if action == "search_knowledge":
        return search_knowledge(pkg, keyword=args.get("keyword", ""),
                                limit=args.get("limit", 20), data_dir=args.get("data_dir"))
    if action == "execute":
        return execute(pkg, output_id=args.get("output_id", ""),
                       params=args.get("params", ""), data_dir=args.get("data_dir"),
                       out_dir=args.get("out_dir"), max_rows=args.get("max_rows", 20000))
    if action == "query_data":
        return query_data(
            pkg,
            sql=args.get("sql", ""),
            data_dir=args.get("data_dir"),
            save_result=bool(args.get("save_result", False)),
            out_dir=args.get("out_dir"),
        )
    return f"❌ 未知动作：{action}（可用：{'、'.join(ACTIONS)}）"


# ===========================================================================
# 能力包发现
# ===========================================================================
def discover_packages(root: str | Path) -> list[ScenarioPackage]:
    """扫描 data/scenarios 根目录下所有已生成 mcp.json 的能力包。"""
    root = Path(root)
    pkgs: list[ScenarioPackage] = []
    if not root.exists():
        return pkgs
    for mcp_file in sorted(root.glob("*/skills/mcp.json")):
        try:
            pkgs.append(ScenarioPackage.load(mcp_file.parent))
        except Exception:
            continue
    return pkgs


def capability_catalog(pkgs: list[ScenarioPackage]) -> list[dict]:
    """把一组能力包压成「能力目录」——发现 + 触发判据。"""
    return [
        {
            "namespace": p.namespace,
            "display_name": p.display_name,
            "summary": p.summary,
            "when_to_use": p.when_to_use,
            "not_for": p.not_for,
            "required_tables": list(p.card.get("required_tables") or []),
            "knowledge_table": p.card.get("knowledge_table", ""),
            "outputs": [
                {"output_id": o.get("output_id"), "name": o.get("name")}
                for o in p.output_specs()
            ],
            "first_tool_to_call": f"{p.namespace}__describe_capability",
            "tools": [t.get("name") for t in p.tools],
        }
        for p in pkgs
    ]


def _df_preview(rows: Any, width: int = 3000, head: int = 20) -> str:
    try:
        import pandas as pd
        df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        return df.head(head).to_markdown(index=False)[:width]
    except Exception:
        try:
            return json.dumps(list(rows)[:10], ensure_ascii=False)[:width]
        except Exception:
            return str(rows)[:width]
