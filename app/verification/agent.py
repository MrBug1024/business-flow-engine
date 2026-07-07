"""验证 Agent（v1.0.5）。

与蒸馏平台完全隔离：只能调用目标业务场景的 Skill 包，
不能访问任何平台内部工具（deduce_*/extract_metadata/generate_skills 等）。

工具集来自 build_verify_tools()，这些工具只读取：
  - 技能包文件（manifest.json / output_specs.json / dispatch_config.json）
  - 场景上传目录（uploads/）下的业务数据文件
  - 技能执行脚本（skill_executor.py / search_knowledge.py / list_knowledge.py）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from deepagents import create_deep_agent
from langchain_core.tools import StructuredTool

from app.core.agent_guard import ExcludeBuiltinToolsMiddleware
from app.core.llm import get_llm
from app.domain.models import Scenario
from app.release.builder import ensure_release_package
from app.domain.storage import store
from app.data.table_io import load_full_frame_cached

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _load_verify_prompt() -> str:
    p = _PROMPTS_DIR / "verification" / "system.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def build_verify_tools(scenario_id: str) -> list[StructuredTool]:
    """构建验证 Agent 工具集（仅 Skill 包访问权限）。"""

    _DATA_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xls", ".json")

    def _skills_dir() -> Path:
        return ensure_release_package(scenario_id).package_dir

    def _has_data_files(d: Path) -> bool:
        return d.exists() and any(f.suffix.lower() in _DATA_SUFFIXES for f in d.iterdir())

    def _uploads_dir() -> Path:
        """验证通道的业务数据目录：优先用户在验证通道里新上传的测试数据
        （verify_uploads/），没有的话才退回蒸馏阶段的 uploads/。

        这两个目录物理隔离：验证通道要证明的是"技能包能在新数据上跑通"，
        而不是反复读同一批蒸馏时用过的文件。
        """
        verify_dir = Path(store.verify_uploads_dir(scenario_id))
        if _has_data_files(verify_dir):
            return verify_dir
        return Path(store.uploads_dir(scenario_id))

    def _outputs_dir() -> Path:
        return Path(store.outputs_dir(scenario_id))

    def _load_manifest() -> dict:
        p = _skills_dir() / "manifest.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {}

    def _load_dispatch() -> dict:
        p = _skills_dir() / "main_skill" / "dispatch_config.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {}

    def _load_output_specs() -> list[dict]:
        p = _skills_dir() / "main_skill" / "output_specs.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")).get("outputs", [])
        return []

    def _require_skills() -> str | None:
        """若 Skill 包不存在则返回错误信息，否则返回 None。"""
        sd = _skills_dir()
        if not (sd / "manifest.json").exists() and not (sd / "main_skill").exists():
            return "❌ 当前场景尚未生成技能包，请先在蒸馏通道完成「生成技能」步骤。"
        return None

    # ---------------------------------------------------------------- list_outputs
    def list_outputs() -> str:
        """列出本场景 Skill 包中所有可以产出的输出项。"""
        err = _require_skills()
        if err:
            return err
        specs = _load_output_specs()
        if not specs:
            return "❌ 技能包中无可用产出规格（output_specs.json 为空）。"
        lines = [f"本场景共 {len(specs)} 个可执行产出：\n"]
        for s in specs:
            lines.append(
                f"- **{s.get('output_id')}**（{s.get('name', '?')}）"
                f" 格式:{s.get('fmt','csv')} 状态:{s.get('status','?')}"
            )
        manifest = _load_manifest()
        if manifest.get("execution_mode"):
            lines.append(f"\n执行模式：{manifest['execution_mode']}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- describe_schema
    def describe_schema() -> str:
        """获取本场景全部表结构：每张表的字段名/类型/业务语义 + 表间关联(ER) + 知识表分派结构。

        这是构造 `query_data` SQL 前**必须**先调用一次的工具（每个新对话调一次即可，
        无需每轮重复调用）。它给出场景内**任意**表、**任意**字段、表间 JOIN 键的完整清单，
        让你可以针对用户当前这句话现场拼 SQL，而不是凭记忆/猜测字段名，
        也不必受限于之前对话中出现过的表或过滤条件。
        """
        err = _require_skills()
        if err:
            return err
        domain_file = _skills_dir() / "main_skill" / "domain_knowledge.json"
        if not domain_file.exists():
            return "❌ domain_knowledge.json 不存在，技能包可能不完整。"
        domain = json.loads(domain_file.read_text(encoding="utf-8"))

        lines = [f"# 场景「{domain.get('scenario', '')}」完整表结构\n"]
        for t in domain.get("tables", []):
            role = t.get("role", "input")
            cols = t.get("columns", [])
            lines.append(f"## 表「{t.get('table_name')}」（角色:{role}，约 {t.get('row_count', '?')} 行）")
            for c in cols:
                sem = f" — {c.get('semantic')}" if c.get("semantic") else ""
                role_tag = c.get("semantic_role", "")
                lines.append(f"  - `{c.get('name')}` ({c.get('dtype', '?')}"
                              f"{'|' + role_tag if role_tag and role_tag != 'UNKNOWN' else ''}){sem}")
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
                "请先用 `search_knowledge`/`list_knowledge` 读到规则原文，自行理解判断"
                "逻辑后再用 `query_data` 构造针对该规则的查询。"
            )

        lines.append(
            "\n> 提示：`query_data` 的表名与上面列出的表名完全一致（用双引号引用）。"
            "本场景支持对**任意**上述表、**任意**字段、**任意**多表 JOIN/聚合发起查询，"
            "不局限于历史上问过的表或字段。请根据用户本次的具体诉求现场构造 SQL，"
            "**不要**照搬之前对话里用过的过滤值（如某个具体编号/名称），除非用户这次问的就是同一个对象。"
        )
        return "\n".join(lines)

    # ---------------------------------------------------------------- execute_skill
    def execute_skill(
        output_id: str,
        params: str = "",
        data_dir: str = "",
        out_dir: str = "",
    ) -> str:
        """执行业务场景 Skill。

        两种产出规格对应两种结果：
        ① **纯结构性 pipeline**（join/aggregate/filter 等，与业务判断无关）：
           直接产出结果文件，返回行数 + 产出文件路径，可直接当作最终结果。
        ② **知识表驱动**（该产出依赖知识/规则表逐条判断）：本工具**不会**替你算出
           最终结果——真实业务规则可能有成百上千条、每条判断逻辑都不同，不可能
           提前写死。它只按 params 筛出命中的知识行原文，连同 field_role_map
           （知识字段 → 业务表字段对应关系）一起返回。**收到这种结果后，你必须
           自己逐条阅读规则原文，调用 `query_data` 针对每条规则构造并执行查询**，
           不能因为 execute_skill 没有直接给出行数据就当作产出了 0 行或执行失败。

        参数：
            output_id  产出 ID（先调 list_outputs 查询可用 ID）
            params     过滤条件：空串=全量；关键词字符串；或 JSON {"列名":"值"}
            data_dir   业务数据目录（默认=场景上传目录）
            out_dir    输出目录（默认=场景输出目录）
        """
        err = _require_skills()
        if err:
            return err

        executor_path = _skills_dir() / "main_skill" / "scripts" / "skill_executor.py"
        if not executor_path.exists():
            return "❌ 主技能执行脚本不存在，请重新在蒸馏通道执行「生成技能」。"

        actual_data_dir = data_dir.strip() or str(_uploads_dir())
        actual_out_dir = out_dir.strip() or str(_outputs_dir())

        rf = None
        if params.strip():
            try:
                rf = json.loads(params.strip())
            except Exception:
                rf = params.strip()

        t0 = time.perf_counter()
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_skill_exec", str(executor_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            result = mod.produce(
                output_id, actual_data_dir,
                out_dir=actual_out_dir, params=rf, max_rows=20000,
            )
        except Exception as exc:
            return (
                f"❌ 技能执行失败：{type(exc).__name__}: {exc}\n"
                f"（output_id={output_id!r}，params={rf!r}，数据目录={actual_data_dir}）\n"
                "请把这个失败原因如实告知用户；若是 params 不匹配，可先用 "
                "`list_knowledge` 核对知识条目后重试。"
            )
        elapsed = time.perf_counter() - t0

        mode = result.get("mode", "")

        if mode == "knowledge_rows":
            matched = result.get("matched_rows", [])
            matched_count = result.get("matched_count", len(matched))
            guidance = result.get("guidance", "")
            try:
                import pandas as pd
                preview = pd.DataFrame(matched).head(20).to_markdown(index=False)[:3000]
            except Exception:
                preview = json.dumps(matched[:10], ensure_ascii=False)[:2000]
            batch_hint = ""
            if matched_count > 10:
                batch_hint = (
                    f"\n\n⚠️ 命中 {matched_count} 条规则，逐条落地查询耗时会很长。"
                    "请**不要**在本轮静默逐条执行全部规则：先告知用户总量，"
                    "本轮只处理前 10 条（或让用户指定范围/关键词缩小），"
                    "给出这批结果后再询问是否继续下一批。"
                )
            return (
                f"📋 命中知识表「{result.get('knowledge_table', '?')}」"
                f"{matched_count} 条规则/知识行（筛选耗时 {elapsed:.1f}s），"
                "尚未据此计算最终业务结果。\n\n"
                f"{guidance}{batch_hint}\n\n"
                f"命中的知识行预览（≤20 条）：\n{preview}"
            )

        rows = result.get("rows", 0)
        artifact = result.get("artifact", "")

        # 注：这里只是"有没有产出"的执行状态，不是最终验证结论——是否真正验证通过，
        # 需要按系统提示词里的「验证判断」标准（同一条规则时看是否覆盖历史结果，而不是
        # 行数/列结构比对）由你自己判断，不能仅凭这里 rows > 0 就下结论。
        status = "✅ 有产出" if rows > 0 else "⚠️ 0 行输出（可能过滤条件不匹配）"
        summary = f"{status}｜产出 {rows} 行｜耗时 {elapsed:.1f}s｜数据目录：{actual_data_dir}"
        if artifact:
            summary += f"\n产出文件：{Path(artifact).name}"
            # 给出产出内容预览，让调用方（LLM）能核对结果内容，而不是只看到一个行数
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
            summary += ("\n\n0 行 ≠ 执行失败：请向用户说明是过滤条件未命中还是数据本身无记录。"
                        "建议先调 `list_knowledge` 查看知识条目，确认 params 与知识表数据匹配。")
        return summary

    # ---------------------------------------------------------------- list_knowledge
    def list_knowledge(limit: int = 50, data_dir: str = "") -> str:
        """浏览 Skill 包中知识表的所有条目。了解可执行的知识条目范围。"""
        err = _require_skills()
        if err:
            return err

        search_script = _skills_dir() / "skill_knowledge_search" / "scripts" / "list_knowledge.py"
        if not search_script.exists():
            return "❌ list_knowledge 脚本不存在，技能包可能不完整。"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_lk", str(search_script))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            actual_data_dir = data_dir.strip() or str(_uploads_dir())
            rows = mod.list_all(limit=int(limit), data_dir=actual_data_dir)
            cols = mod.get_columns(data_dir=actual_data_dir)
        except Exception as exc:
            return f"❌ 读取知识表失败：{type(exc).__name__}: {exc}"

        if not rows:
            return "知识表为空或未找到对应数据文件。"
        try:
            import pandas as pd
            import io
            df = pd.DataFrame(rows)
            preview = df.to_markdown(index=False)[:3000]
        except Exception:
            preview = json.dumps(rows[:10], ensure_ascii=False)[:3000]

        dispatch = _load_dispatch()
        dispatch_col = dispatch.get("dispatch_key_column", "")
        hint = f"（分派键列：`{dispatch_col}`）" if dispatch_col else ""
        return (f"知识表字段（{len(cols)} 列）{hint}：{cols}\n\n"
                f"前 {len(rows)} 条（共限制 {limit} 条）：\n{preview}")

    # ---------------------------------------------------------------- search_knowledge
    def search_knowledge(keyword: str = "", limit: int = 20, data_dir: str = "") -> str:
        """在 Skill 包的知识表中按关键词搜索条目。

        参数：
            keyword   搜索关键词（空=返回全量前 N 条）
            limit     返回条数上限（默认 20）
            data_dir  业务数据目录（默认=场景上传目录）
        """
        err = _require_skills()
        if err:
            return err

        search_script = _skills_dir() / "skill_knowledge_search" / "scripts" / "search_knowledge.py"
        if not search_script.exists():
            return "❌ search_knowledge 脚本不存在，技能包可能不完整。"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_sk", str(search_script))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            actual_data_dir = data_dir.strip() or str(_uploads_dir())
            rows = mod.search(
                keyword=keyword.strip(), limit=int(limit),
                data_dir=actual_data_dir,
            )
        except Exception as exc:
            return f"❌ 搜索知识表失败：{type(exc).__name__}: {exc}"

        if not rows:
            return f"未找到匹配「{keyword}」的知识条目。"
        try:
            import pandas as pd
            df = pd.DataFrame(rows)
            preview = df.to_markdown(index=False)[:3000]
        except Exception:
            preview = json.dumps(rows[:10], ensure_ascii=False)[:3000]
        return f"找到 {len(rows)} 条匹配「{keyword}」的知识条目：\n{preview}"

    # ---------------------------------------------------------------- query_data
    def query_data(sql: str) -> str:
        """对场景中已上传的业务数据执行 DuckDB SQL 即席查询——支持任意表、任意字段、
        任意多表 JOIN/聚合/分组，不是只能查某一张表或某一种过滤条件。

        表名用双引号，例如：SELECT * FROM "业务明细表" LIMIT 10
        多表关联示例：SELECT a.*, b.某字段 FROM "表A" a JOIN "表B" b ON a.关联键 = b.关联键

        **强制要求**：调用本工具前，若本次对话还没调用过 `describe_schema`，先调用它拿到
        完整表结构与关联键，再据此现场构造本次查询——不要凭猜测拼字段名，也不要把
        之前某一轮查询里用过的具体过滤值原样套到新的问题上；用户换了问题，WHERE 条件
        必须跟着换。
        """
        err = _require_skills()
        if err:
            return err

        sql = (sql or "").strip()
        if not sql:
            return "❌ sql 参数不能为空。"

        # 加载 domain_knowledge 获取表结构
        domain_file = _skills_dir() / "main_skill" / "domain_knowledge.json"
        if not domain_file.exists():
            return "❌ domain_knowledge.json 不存在，技能包可能不完整。"

        domain = json.loads(domain_file.read_text(encoding="utf-8"))
        tables_meta = domain.get("tables", [])
        uploads = _uploads_dir()

        # 只加载 SQL 里实际引用到的表。历史版本在这里把场景内**所有**表每次都全量
        # 重读一遍（且用的是无缓存的 openpyxl 慢路径）——一张几十万行的 Excel 单次
        # 就要读几分钟，Agent 每条规则又要查好几次，正是验证通道"慢/卡/无反馈"的
        # 头号根因。现在改为：SQL 引用哪张表才加载哪张，且走 load_full_frame_cached
        # （calamine 快速引擎 + 表头自动识别 + 进程内 mtime 缓存，重复查询秒回）。
        all_names = [t.get("table_name", "") for t in tables_meta if t.get("table_name")]
        referenced = {n for n in all_names if n and n in sql}
        if not referenced:
            return (
                "❌ SQL 中未识别到本场景的任何表名，未执行查询。\n"
                f"可用表：{ '、'.join(all_names) or '（无）' }\n"
                "表名必须与 describe_schema 列出的完全一致（建议用双引号引用）。"
            )

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
                    candidates = [
                        p for p in uploads.glob(f"{name}.*")
                        if p.suffix.lower() in _DATA_SUFFIXES
                    ]
                    fpath = t.get("file_path", "")
                    if not candidates and fpath and Path(fpath).exists():
                        candidates = [Path(fpath)]
                    if not candidates:
                        return (
                            f"❌ 未执行查询：找不到表「{name}」对应的数据文件"
                            f"（查找目录：{uploads}）。\n"
                            "请确认验证数据文件名（不含后缀）与表名一致，或先上传该表。"
                        )
                    p = candidates[0]
                    t_load = time.perf_counter()
                    df = load_full_frame_cached(str(p))
                    con.register(name, df)
                    loaded.append(name)
                    load_notes.append(
                        f"{name}（{len(df)} 行，加载 {time.perf_counter() - t_load:.1f}s）"
                    )
                result = con.execute(sql).fetchdf()
            finally:
                con.close()
        except Exception as exc:
            return (
                f"❌ SQL 执行失败：{type(exc).__name__}: {exc}\n"
                f"已加载表：{loaded or '（无）'}\n"
                "表名/字段名请以 describe_schema 的输出为准。"
            )

        elapsed = time.perf_counter() - t0
        n_rows, n_cols = len(result), len(result.columns)
        # 落盘
        try:
            out_dir = _outputs_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = f"query_{int(time.time())}"
            artifact = out_dir / f"{stem}.csv"
            result.to_csv(artifact, index=False, encoding="utf-8-sig")
            artifact_name = artifact.name
        except Exception:
            artifact_name = ""

        try:
            preview = "\n" + result.head(20).to_markdown(index=False)[:2000]
        except Exception:
            preview = "\n" + result.head(20).to_string(index=False)[:2000]

        head = (f"✅ 查询完成：{n_rows} 行 × {n_cols} 列，耗时 {elapsed:.1f}s"
                + (f"，落盘：{artifact_name}" if artifact_name else ""))
        note = "" if n_rows else (
            "\n⚠️ 查询成功但结果为 0 行——这不是执行失败，而是没有数据满足当前条件。"
            "请核对 WHERE 条件的字段名/取值是否与真实数据一致（可先 SELECT DISTINCT 该字段确认取值）。"
        )
        return (f"{head}\n数据来源：{uploads}\n"
                f"已加载表：{'；'.join(load_notes)}{note}\n预览（≤20行）：{preview}")

    return [
        StructuredTool.from_function(list_outputs),
        StructuredTool.from_function(describe_schema),
        StructuredTool.from_function(execute_skill),
        StructuredTool.from_function(list_knowledge),
        StructuredTool.from_function(search_knowledge),
        StructuredTool.from_function(query_data),
    ]


def build_verification_agent(scenario: Scenario):
    """构建验证 Agent（与蒸馏平台完全隔离）。"""
    llm = get_llm()
    if llm is None:
        raise RuntimeError("LLM 未配置，无法构建验证 Agent。")

    tools = build_verify_tools(scenario.id)
    manifest = {}
    try:
        mp = ensure_release_package(scenario.id).package_dir / "manifest.json"
        if mp.exists():
            manifest = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        pass

    has_skills = bool(scenario.skills)
    skill_names = "、".join(s.name for s in scenario.skills[:5]) if scenario.skills else "（未生成）"
    exec_mode = manifest.get("execution_mode", "未知")

    context = (
        f"\n\n# 当前挂载场景（验证视图）\n"
        f"场景名：{scenario.name}\n"
        f"描述：{scenario.description or '（无）'}\n"
        f"技能包状态：{'✅ 已生成' if has_skills else '❌ 未生成'}\n"
        f"技能列表：{skill_names}\n"
        f"执行模式：{exec_mode}\n\n"
        "⚠️ 本通道为**验证专属通道**，只能调用上述 Skill 包工具。\n"
        "如需修改场景/重新推导，请切换到蒸馏平台。"
    )

    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=_load_verify_prompt() + context,
        middleware=[ExcludeBuiltinToolsMiddleware()],
    )
