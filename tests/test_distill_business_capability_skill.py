from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "system_skills" / "distill-business-capability"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DISTILL = load_module("distill_capabilities_skill", SKILL_ROOT / "scripts" / "distill_capabilities.py")
TABULAR = load_module(
    "portable_tabular_reader",
    SKILL_ROOT / "assets" / "portable-tabular-reader" / "scripts" / "query_tabular.py",
)
DOCUMENT = load_module(
    "portable_document_reader",
    SKILL_ROOT / "assets" / "portable-document-reader" / "scripts" / "extract_documents.py",
)
STAGE_RUNTIME = load_module(
    "portable_stage_runtime",
    SKILL_ROOT / "assets" / "portable-stage-runtime" / "scripts" / "run_stage.py",
)
VECTOR_SCRIPTS = PROJECT_ROOT / "system_skills" / "vector-kb" / "scripts"
if str(VECTOR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(VECTOR_SCRIPTS))
KNOWLEDGE = load_module(
    "portable_knowledge_wrapper",
    SKILL_ROOT / "assets" / "portable-knowledge-wrapper" / "scripts" / "scenario_kb.py",
)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upstream_fixture(root: Path, *, mixed_formats: bool) -> tuple[Path, Path]:
    relation_root = root / "data-relations"
    relation_root.mkdir(parents=True)
    files = [
        ("claims.xlsx", ".xlsx", "E-CLAIM"),
        ("results.csv", ".csv", "E-RESULT"),
    ]
    if mixed_formats:
        files.extend(
            [
                ("policy.pdf", ".pdf", "E-POLICY"),
                ("attachment.png", ".png", "E-IMAGE"),
                ("guidance.docx", ".docx", "E-GUIDE"),
            ]
        )
    request_evidence = ["E-CLAIM"] + (["E-IMAGE"] if mixed_formats else [])
    policy_evidence = ["E-POLICY", "E-GUIDE"] if mixed_formats else ["E-CLAIM"]
    relations = {
        "schema_version": 1,
        "status": "complete",
        "scenario": {"name": "费用审核", "purpose": "形成受规则约束且可追溯的费用审核结果"},
        "nodes": [
            {
                "id": "n_request",
                "name": "费用申请与支撑材料",
                "type": "input",
                "description": "申请事项及其支撑材料",
                "evidence_ids": request_evidence,
            },
            {
                "id": "n_policy",
                "name": "费用审核政策",
                "type": "rule",
                "description": "约束费用审核判断",
                "evidence_ids": policy_evidence,
            },
            {
                "id": "n_decision",
                "name": "费用审核判定",
                "type": "decision",
                "description": "综合申请和政策形成判断",
                "evidence_ids": ["E-CLAIM"],
            },
            {
                "id": "n_result",
                "name": "费用审核结果",
                "type": "output",
                "description": "可供下游使用的审核结果",
                "evidence_ids": ["E-RESULT"],
            },
        ],
        "edges": [
            {
                "id": "e_feed",
                "source": "n_request",
                "target": "n_decision",
                "type": "feeds",
                "label": "提供审核信息",
                "confidence": 0.94,
                "evidence_ids": ["E-FEED"],
            },
            {
                "id": "e_govern",
                "source": "n_policy",
                "target": "n_decision",
                "type": "governs",
                "label": "约束审核",
                "confidence": 0.95,
                "evidence_ids": ["E-GOV"],
            },
            {
                "id": "e_result",
                "source": "n_decision",
                "target": "n_result",
                "type": "derives",
                "label": "形成结果",
                "confidence": 0.93,
                "evidence_ids": ["E-DERIVE"],
            },
        ],
        "main_chain": ["n_request", "n_decision", "n_result"],
        "primary_data_path": ["n_request", "n_decision", "n_result"],
        "branches": [],
        "coverage": {"included_files": [item[0] for item in files], "excluded_files": []},
    }
    relation_path = relation_root / "scenario-relationship.json"
    write_json(relation_path, relations)
    (relation_root / "relations.mmd").write_text("flowchart LR\n", encoding="utf-8")
    (relation_root / "relation-report.md").write_text("# relation report\n", encoding="utf-8")
    write_json(
        relation_root / "evidence-cards.json",
        {
            "schema_version": 1,
            "cards": [
                {
                    "id": evidence_id,
                    "kind": "file_structure",
                    "strength": "contextual",
                    "statement": f"{name} inventory",
                    "sources": [{"file": name, "locator": "file"}],
                    "facts": {"extension": extension, "table_count": 1},
                    "snippet": "",
                }
                for name, extension, evidence_id in files
            ],
        },
    )
    role_map = {
        "claims.xlsx": [
            {"node_id": "n_request", "node_name": "费用申请与支撑材料", "node_type": "input"},
            {"node_id": "n_decision", "node_name": "费用审核判定", "node_type": "decision"},
        ],
        "results.csv": [{"node_id": "n_result", "node_name": "费用审核结果", "node_type": "output"}],
        "attachment.png": [{"node_id": "n_request", "node_name": "费用申请与支撑材料", "node_type": "input"}],
        "policy.pdf": [{"node_id": "n_policy", "node_name": "费用审核政策", "node_type": "rule"}],
        "guidance.docx": [{"node_id": "n_policy", "node_name": "费用审核政策", "node_type": "rule"}],
    }
    if not mixed_formats:
        role_map["claims.xlsx"].append(
            {"node_id": "n_policy", "node_name": "费用审核政策", "node_type": "rule"}
        )
    sources = []
    for index, (name, extension, _) in enumerate(files, 1):
        tabular = extension in {".xlsx", ".csv"}
        sources.append({
            "source_id": f"src-{index}",
            "view_name": f"source_{index}",
            "path": name,
            "extension": extension,
            "kind": "tabular" if tabular else "document",
            "size_bytes": 100,
            "is_large": False,
            "roles": role_map[name],
            "tables": [{
                "table_id": f"{name}:Sheet1",
                "sheet_or_table": "Sheet1",
                "row_count": 10,
                "column_count": 2,
                "columns": [
                    {"name": "申请ID", "query_name": "申请ID", "kind": "id"},
                    {"name": "金额", "query_name": "金额", "kind": "other"},
                ],
                "header": {"header_row": 0, "header_confidence": 1.0, "header_detection": "fixture"},
                "schema_usable": True,
            }] if tabular else [],
            "access_policy": "bounded_sql_only" if tabular else "bounded_extract_or_ocr",
            "content_retrieval": {
                "mode": "schema_bound_read_only_sql" if tabular else (
                    "ocr_then_chunk_index" if extension == ".png" else "parse_then_chunk_index"
                ),
                "required_output_provenance": ["source_id", "locator", "content digest"],
            },
            "agent_must_not_open_directly": True,
        })
    link = {
        "link_id": "link-claims-results",
        "kind": "result_trace",
        "source_id": "src-1",
        "target_id": "src-2",
        "source_file": "claims.xlsx",
        "target_file": "results.csv",
        "recommended_candidate": {
            "source_field": "申请ID", "target_field": "申请ID", "score": 0.9,
        },
        "candidates": [],
        "runtime_validation": ["check join fanout"],
    }
    semantic_routes = []
    if mixed_formats:
        semantic_routes = [{
            "route_id": "route-policy-claims",
            "mode": "provenance_preserving_semantic_retrieval",
            "source_id": "src-3",
            "target_id": "src-1",
            "macro_edge_id": "e_govern",
            "relation_type": "governs",
            "evidence_ids": ["E-GOV"],
            "evidence_locators": [{"evidence_id": "E-GOV", "file": "policy.pdf", "locator": "page:1"}],
            "runtime_validation": ["require explicit business key before structured join"],
        }]
    operational = {
        "schema_version": 1,
        "status": "ready",
        "scenario": relations["scenario"],
        "source": {"field_evidence": "fixture", "field_evidence_fingerprint": "fixture"},
        "sources": sources,
        "links": [link],
        "semantic_routes": semantic_routes,
        "rule_source_ids": ["src-3", "src-5"] if mixed_formats else ["src-1"],
        "result_source_ids": ["src-2"],
        "query_policy": {
            "rule_record_mode": "return_complete_selected_rule_row",
            "large_table_threshold_rows": 50000,
            "large_sources_must_use_sql": True,
            "agent_must_not_open_source_files": True,
            "required_sequence": ["locate complete rule record", "validate joins", "query"],
        },
        "quality_gates": {"status": "passed", "blockers": [], "warnings": []},
    }
    operational_path = relation_root / "operational-data-contract.json"
    write_json(operational_path, operational)
    relations["operational_contract"] = {
        "artifact": str(operational_path.resolve()),
        "fingerprint": digest(operational_path),
        "status": "ready",
        "quality_gates": operational["quality_gates"],
    }
    write_json(relation_path, relations)

    flow_root = root / "business-flow"
    flow_root.mkdir(parents=True)
    stages = [
        {
            "id": "s_prepare",
            "name": "准备费用审核上下文",
            "stage_type": "preparation",
            "objective": "汇聚费用申请及支撑材料",
            "outcome": "形成可供审核使用的完整业务上下文",
            "owner_role": "",
            "input_node_ids": ["n_request"],
            "output_node_ids": ["n_request"],
            "support": {},
            "inference": {"basis": "structural", "confidence": 0.86, "rationale": "上游输入汇入判定。"},
        },
        {
            "id": "s_decide",
            "name": "执行费用审核判定",
            "stage_type": "decision",
            "objective": "依据申请上下文和审核政策形成判断",
            "outcome": "形成可供结果落实使用的审核结论",
            "owner_role": "",
            "input_node_ids": ["n_request", "n_policy"],
            "output_node_ids": ["n_decision"],
            "support": {},
            "inference": {"basis": "structural", "confidence": 0.92, "rationale": "数据和规则共同支撑判定。"},
        },
        {
            "id": "s_result",
            "name": "形成费用审核结果",
            "stage_type": "closure",
            "objective": "将审核结论落实为场景级结果",
            "outcome": "形成可追溯的费用审核结果",
            "owner_role": "",
            "input_node_ids": ["n_decision"],
            "output_node_ids": ["n_result"],
            "support": {},
            "inference": {"basis": "structural", "confidence": 0.91, "rationale": "判定形成最终结果。"},
        },
    ]
    transitions = [
        {"id": "t_prepare_decide", "source": "s_prepare", "target": "s_decide", "type": "normal"},
        {"id": "t_decide_result", "source": "s_decide", "target": "s_result", "type": "normal"},
    ]
    flow = {
        "schema_version": 1,
        "status": "complete",
        "source": {
            "capability": "discover-data-relations",
            "artifact": str(relation_path.resolve()),
            "fingerprint": digest(relation_path),
            "operational_data_contract": {
                "artifact": str(operational_path.resolve()),
                "fingerprint": digest(operational_path),
            },
        },
        "scenario": {
            "name": "费用审核",
            "purpose": "形成受规则约束且可追溯的费用审核结果",
            "business_outcome": "形成受规则约束且可追溯的费用审核结果",
            "grain": "macro_business_scenario",
        },
        "history_policy": {"role": "validation_only", "statement": "历史数据只验证，不定义流程。"},
        "execution_policy": {
            "rule_resolution": "complete_rule_record_before_bulk_query",
            "bulk_data_access": "bounded_read_only_sql",
            "join_policy": "evidence_backed_keys_with_runtime_fanout_validation",
            "agent_direct_file_read": False,
            "unstructured_access": "parse_or_ocr_then_provenance_chunk_search",
        },
        "stages": stages,
        "transitions": transitions,
        "main_flow": ["s_prepare", "s_decide", "s_result"],
        "states": [
            {
                "id": "st_complete",
                "name": "费用审核结果已形成",
                "state_type": "terminal",
                "reached_after": "s_result",
            }
        ],
        "controls": [
            {
                "id": "c_policy",
                "name": "费用审核政策",
                "applies_to": ["s_decide"],
                "policy": "约束审核结论形成",
            }
        ],
        "validation_checks": [],
        "open_questions": [
            {
                "id": "q_owner",
                "question": "费用审核由哪个岗位或系统执行？",
                "impact": "影响责任归属",
                "related_stage_ids": ["s_decide"],
            }
        ],
        "coverage": {},
    }
    flow_path = flow_root / "business-flow.json"
    write_json(flow_path, flow)
    (flow_root / "business-flow.mmd").write_text("flowchart LR\n", encoding="utf-8")
    (flow_root / "business-flow-report.md").write_text("# flow report\n", encoding="utf-8")
    write_json(flow_root / "flow-claims.json", flow)
    return relation_path, flow_path


def fill_plan(template: dict) -> dict:
    claims = json.loads(json.dumps(template, ensure_ascii=False))
    claims["bundle"]["name"] = "expense-review-capabilities"
    claims["bundle"]["description"] = "面向第三方 Agent 的费用审核场景能力源码，覆盖文件读取、阶段责任、流程路由和已验收业务约束。"
    for foundation in claims["foundation_skills"]:
        kind = foundation["kind"]
        foundation["skill_name"] = f"expense-review-{kind}-reader"
        foundation["display_name"] = f"费用审核{kind}基础读取"
        foundation["description"] = (
            f"在费用审核业务场景收到 {', '.join(foundation['formats'])} 文件，且准备或判定阶段需要读取其业务内容时使用；"
            "仅提供有界解析和可追溯内容，不执行费用审核判断。"
        )
        foundation["when_to_use"] = [
            f"当前费用审核输入包含 {', '.join(foundation['formats'])} 文件",
            "阶段输入契约要求读取对应业务材料",
        ]
        foundation["scenario_instructions"] = ["按 file_roles 中声明的费用申请、政策或结果角色选择输入文件。"]
        foundation["non_goals"] = ["不决定费用申请是否通过，不修改原始业务文件。"]
    for stage in claims["stage_skills"]:
        stage_slug = stage["stage_id"].replace("_", "-")
        stage["skill_name"] = f"expense-review-{stage_slug}"
        stage["description"] = (
            f"在费用审核业务场景进入“{stage['display_name']}”或用户明确请求完成该阶段责任时使用；"
            f"依据已验收输入和控制形成“{stage['outcome']}”，不代替相邻流程阶段。"
        )
        stage["invocation_triggers"] = [
            f"用户要求完成{stage['display_name']}",
            "已收到前置阶段交付并满足本阶段输入契约",
        ]
        input_ids = [node for item in stage["input_contract"] for node in item["relation_node_ids"]]
        stage["procedure"] = [
            {
                "action": "核对并读取输入契约中的业务对象，只保留完成本阶段所需内容。",
                "basis": "input_contract" if input_ids else "flow_stage",
                "source_ids": input_ids[:2] if input_ids else [stage["stage_id"]],
            },
            {
                "action": "完成已验收阶段目标，并按输出契约形成可追溯的阶段结果。",
                "basis": "flow_stage",
                "source_ids": [stage["stage_id"]],
            },
        ]
        if stage["control_ids"]:
            stage["procedure"].insert(
                1,
                {
                    "action": "应用已验收业务控制约束阶段判断，不从历史样本扩展规则。",
                    "basis": "control",
                    "source_ids": stage["control_ids"],
                },
            )
        stage["non_goals"] = ["不执行前置或后续阶段，不补充未获上游支撑的审批和退回逻辑。"]
    orchestrator = claims["orchestrator"]
    orchestrator["skill_name"] = "expense-review-orchestrator"
    orchestrator["description"] = (
        "在第三方 Agent 收到完整费用审核请求、需要判断应调用哪个阶段，或需要按已验收主流程串联文件读取与阶段能力时使用；"
        "只负责任务路由、交接和边界控制。"
    )
    orchestrator["invocation_triggers"] = ["用户请求完成端到端费用审核", "用户输入跨越两个或更多费用审核阶段"]
    orchestrator["failure_policy"] = [
        "缺少输入、政策或基础读取能力时停止对应阶段并报告缺口。",
        "遇到待确认分支或责任归属时保留问题，不根据历史频次自动选择。",
    ]
    orchestrator["non_goals"] = ["不替代阶段 Skill 执行业务判断，不发布或安装能力包。"]
    for item in claims["unsupported_formats"]:
        item["reason"] = "当前没有随包提供该格式解析器，第三方应先转换为已支持格式。"
    return claims


class PortableFoundationTests(unittest.TestCase):
    def test_duckdb_reader_queries_csv_and_rejects_mutation(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "data.csv"
            source.write_text("kind,amount\nA,10\nB,20\nA,5\n", encoding="utf-8")
            payload = TABULAR.run(
                ["query", "--input", str(source), "--sql", "SELECT kind, sum(amount) total FROM source GROUP BY kind", "--max-rows", "10"]
            )
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["row_count_returned"], 2)
            with self.assertRaises(TABULAR.ReaderError):
                TABULAR.run(["query", "--input", str(source), "--sql", "DELETE FROM source"])
            with self.assertRaises(TABULAR.ReaderError):
                TABULAR.run([
                    "query", "--input", str(source),
                    "--sql", "SELECT * FROM read_csv_auto('/uncontracted/file.csv')",
                ])

    def test_contract_reader_accepts_new_content_when_runtime_schema_matches(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "claims.csv"
            source.write_text("business_id,amount\nA1,10\n", encoding="utf-8")
            contract = {
                "status": "ready",
                "sources": [{
                    "source_id": "src-claims", "view_name": "source_1", "path": "claims.csv",
                    "extension": ".csv", "kind": "tabular", "size_bytes": source.stat().st_size,
                    "content_sha256": digest(source), "is_large": False,
                    "tables": [{
                        "sheet_or_table": "claims.csv", "row_count": 1, "column_count": 2,
                        "columns": [
                            {"name": "business_id", "query_name": "business_id"},
                            {"name": "amount", "query_name": "amount"},
                        ],
                        "header": {"header_row": 0, "header_confidence": 1.0},
                    }],
                }],
                "links": [],
            }
            queried = TABULAR.query_contract(contract, root, "SELECT * FROM source_1", 10)
            self.assertEqual(queried["columns"], ["business_id", "amount"])
            self.assertEqual(queried["rows"][0], ["A1", 10])
            self.assertEqual(
                queried["registrations"][0]["design_time_content_sha256"], digest(source)
            )

            source.write_text("business_id,amount\nA1,11\n", encoding="utf-8")
            queried = TABULAR.query_contract(contract, root, "SELECT * FROM source_1", 10)
            self.assertEqual(queried["rows"][0], ["A1", 11])

            source.write_text("business_id,other\nA1,11\n", encoding="utf-8")
            with self.assertRaisesRegex(TABULAR.ReaderError, "schema is incompatible"):
                TABULAR.query_contract(contract, root, "SELECT * FROM source_1", 10)

    def test_contract_reader_ignores_missing_templates_and_unreferenced_sources(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "claims.csv").write_text("business_id,amount\nA1,10\n", encoding="utf-8")

            def source(identifier: str, view: str, path: str, lifecycle: str) -> dict:
                return {
                    "source_id": identifier,
                    "view_name": view,
                    "path": path,
                    "extension": ".csv",
                    "kind": "tabular",
                    "lifecycle": lifecycle,
                    "runtime_required": lifecycle == "runtime_input",
                    "tables": [{
                        "sheet_or_table": path,
                        "row_count": 1,
                        "column_count": 2,
                        "columns": [
                            {"name": "business_id", "query_name": "business_id"},
                            {"name": "amount", "query_name": "amount"},
                        ],
                        "header": {"header_row": 0, "header_confidence": 1.0},
                    }],
                }

            contract = {
                "status": "ready",
                "sources": [
                    source("src-claims", "source_1", "claims.csv", "runtime_input"),
                    source("src-unused", "source_2", "missing-runtime.csv", "runtime_input"),
                    source("src-result", "source_3", "missing-result-template.csv", "design_time_template"),
                ],
                "links": [],
            }
            queried = TABULAR.query_contract(contract, root, "SELECT * FROM source_1", 10)
            self.assertEqual(queried["row_count_returned"], 1)
            preflight = TABULAR.preflight_contract(contract, root, {"src-claims"})
            self.assertEqual(preflight["status"], "success")
            self.assertEqual(preflight["ignored_design_time_source_ids"], ["src-result"])
            with self.assertRaisesRegex(TABULAR.ReaderError, "Scoped preflight"):
                TABULAR.preflight_contract(contract, root)

    def test_contract_reader_supports_explicit_runtime_source_binding(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "current-batch.csv").write_text(
                "business_id,amount,extra\nB9,99,new\n", encoding="utf-8"
            )
            contract = {
                "status": "ready",
                "sources": [{
                    "source_id": "src-claims",
                    "view_name": "source_1",
                    "path": "design-time-name.csv",
                    "extension": ".csv",
                    "kind": "tabular",
                    "lifecycle": "runtime_input",
                    "runtime_required": True,
                    "tables": [{
                        "sheet_or_table": "design-time-name.csv",
                        "columns": [
                            {"name": "business_id", "query_name": "business_id"},
                            {"name": "amount", "query_name": "amount"},
                        ],
                        "header": {"header_row": 0},
                    }],
                }],
                "links": [],
            }
            queried = TABULAR.query_contract(
                contract,
                root,
                "SELECT business_id, amount FROM source_1",
                10,
                bindings={"src-claims": "current-batch.csv"},
            )
            self.assertEqual(queried["columns"], ["business_id", "amount"])
            self.assertEqual(queried["rows"], [["B9", 99]])

    def test_required_external_knowledge_failure_requires_manual_intervention(self) -> None:
        with patch.object(
            KNOWLEDGE,
            "search_kb",
            return_value={"status": "no_results", "message": "provider returned no evidence"},
        ):
            code, payload = KNOWLEDGE.run(["search", "--query", "drug specification", "--required"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "manual_intervention_required")
        self.assertNotIn("data", payload)

    def test_document_reader_extracts_bounded_text(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "policy.md"
            source.write_text("# Policy\nOnly supported claims are allowed.\n", encoding="utf-8")
            payload, _ = DOCUMENT.run(["extract", "--input", str(source), "--max-chars", "12", "--format", "json"])
            self.assertEqual(payload["status"], "success")
            self.assertTrue(payload["truncated"])
            self.assertEqual(len(payload["text"]), 12)

    def test_document_reader_indexes_and_returns_provenance_hits(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "policy.md"
            source.write_text("# 医保规则\n重复收费属于违规。\n", encoding="utf-8")
            index = root / "policy.db"
            indexed, _ = DOCUMENT.run(["index", "--input", str(source), "--output", str(index)])
            self.assertEqual(indexed["status"], "success")
            found, _ = DOCUMENT.run(["search", "--index", str(index), "--term", "重复收费"])
            self.assertEqual(found["hit_count_returned"], 1)
            hit = found["hits"][0]
            self.assertTrue(hit["source_digest"])
            self.assertTrue(hit["locator"])
            self.assertTrue(hit["text_digest"])

    def test_document_reader_streams_gb18030_and_returns_bounded_context(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "policy.txt"
            text = "第一节 总则\n" + ("医保审核范围。\n" * 30) + "第二节 重复收费\n不得重复收费。\n"
            source.write_bytes(text.encode("gb18030"))
            index = root / "policy.db"
            indexed, _ = DOCUMENT.run([
                "index", "--input", str(source), "--output", str(index), "--chunk-chars", "12",
            ])
            self.assertGreater(indexed["chunk_count"], 1)
            found, _ = DOCUMENT.run(["search", "--index", str(index), "--term", "重复收费"])
            hit = found["hits"][0]
            context, _ = DOCUMENT.run([
                "context", "--index", str(index), "--chunk-id", str(hit["chunk_id"]),
                "--before", "1", "--after", "1",
            ])
            self.assertLessEqual(len(context["evidence"]), 3)
            self.assertTrue(all(item["source_digest"] == digest(source) for item in context["evidence"]))

    def test_contract_reader_returns_complete_rule_and_validates_multisource_join(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            long_example = "示" * 3000
            (root / "rules.csv").write_text(
                f"规则ID,问题清单,违规类型,参考示例,用途\nR1,重复收费,虚构项目,{long_example},审计\n",
                encoding="utf-8",
            )
            (root / "claims.csv").write_text("业务ID,金额\nA1,10\nA2,20\n", encoding="utf-8")
            (root / "results.csv").write_text("业务ID,结论\nA1,违规\n", encoding="utf-8")

            def source(identifier: str, view: str, path: str, columns: list[str]) -> dict:
                return {
                    "source_id": identifier, "view_name": view, "path": path,
                    "extension": ".csv", "kind": "tabular", "is_large": False,
                    "tables": [{
                        "sheet_or_table": path, "row_count": 2, "column_count": len(columns),
                        "columns": [{"name": item, "query_name": item} for item in columns],
                        "header": {"header_row": 0, "header_confidence": 1.0},
                    }],
                }

            contract = {
                "status": "ready",
                "sources": [
                    source("src-rule", "source_1", "rules.csv", ["规则ID", "问题清单", "违规类型", "参考示例", "用途"]),
                    source("src-claims", "source_2", "claims.csv", ["业务ID", "金额"]),
                    source("src-results", "source_3", "results.csv", ["业务ID", "结论"]),
                ],
                "rule_source_ids": ["src-rule"],
                "links": [{
                    "link_id": "link-1", "source_id": "src-claims", "target_id": "src-results",
                    "recommended_candidate": {"source_field": "业务ID", "target_field": "业务ID"},
                }],
            }
            selected = TABULAR.search_contract(contract, root, "src-rule", ["重复收费"], 10)
            self.assertEqual(selected["row_count_returned"], 1)
            self.assertEqual(selected["columns"], ["规则ID", "问题清单", "违规类型", "参考示例", "用途"])
            self.assertEqual(selected["rows"][0], ["R1", "重复收费", "虚构项目", long_example, "审计"])
            self.assertFalse(selected["cell_values_truncated"])
            validation = TABULAR.validate_contract_link(contract, root, "link-1")
            self.assertEqual(validation["joined_rows"], 1)
            queried = TABULAR.query_contract(
                contract, root,
                'SELECT c."业务ID", c."金额", r."结论" FROM source_2 c JOIN source_3 r ON c."业务ID"=r."业务ID"',
                10, ["link-1"],
            )
            self.assertEqual(queried["rows"], [["A1", 10, "违规"]])
            with self.assertRaisesRegex(TABULAR.ReaderError, "does not use validated key pair"):
                TABULAR.query_contract(
                    contract, root,
                    'SELECT c."业务ID" FROM source_2 c JOIN source_3 r ON c."业务ID"=r."结论"',
                    10, ["link-1"],
                )
            exported = TABULAR.export_contract(
                contract, root,
                'SELECT c."业务ID", c."金额", r."结论" FROM source_2 c JOIN source_3 r ON c."业务ID"=r."业务ID"',
                str(root / "all-results.parquet"), ["link-1"],
            )
            self.assertEqual(exported["mode"], "complete_result_export")
            self.assertEqual(exported["row_count"], 1)
            self.assertTrue((root / "all-results.parquet").is_file())

    def test_composite_contract_key_resolves_single_key_many_to_many(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "left.csv").write_text(
                "机构,业务ID,金额\nA,1,10\nB,1,20\n", encoding="utf-8"
            )
            (root / "right.csv").write_text(
                "机构,业务ID,结论\nA,1,违规A\nB,1,违规B\n", encoding="utf-8"
            )

            def source(identifier: str, view: str, path: str, columns: list[str]) -> dict:
                return {
                    "source_id": identifier, "view_name": view, "path": path,
                    "extension": ".csv", "kind": "tabular", "is_large": False,
                    "tables": [{
                        "sheet_or_table": path, "row_count": 2, "column_count": len(columns),
                        "columns": [{"name": item, "query_name": item} for item in columns],
                        "header": {"header_row": 0, "header_confidence": 1.0},
                    }],
                }

            contract = {
                "status": "ready",
                "sources": [
                    source("src-left", "source_1", "left.csv", ["机构", "业务ID", "金额"]),
                    source("src-right", "source_2", "right.csv", ["机构", "业务ID", "结论"]),
                ],
                "links": [{
                    "link_id": "link-composite", "source_id": "src-left", "target_id": "src-right",
                    "recommended_candidate": {"source_field": "业务ID", "target_field": "业务ID"},
                    "candidate_key_sets": [
                        {
                            "key_set_index": 0, "mode": "single_key",
                            "key_pairs": [{"source_field": "业务ID", "target_field": "业务ID"}],
                        },
                        {
                            "key_set_index": 1, "mode": "composite_key_runtime_candidate",
                            "key_pairs": [
                                {"source_field": "机构", "target_field": "机构"},
                                {"source_field": "业务ID", "target_field": "业务ID"},
                            ],
                        },
                    ],
                }],
            }
            single = TABULAR.validate_contract_link(contract, root, "link-composite", 0)
            self.assertTrue(single["unexplained_many_to_many"])
            self.assertTrue(single["requires_agent_review"])
            composite = TABULAR.validate_contract_link(contract, root, "link-composite", 1)
            self.assertEqual(composite["joined_rows"], 2)
            self.assertFalse(composite["unexplained_many_to_many"])
            self.assertFalse(composite["requires_agent_review"])
            queried = TABULAR.query_contract(
                contract, root,
                'SELECT l."机构", l."业务ID", r."结论" FROM source_1 l JOIN source_2 r '
                'ON l."机构"=r."机构" AND l."业务ID"=r."业务ID" ORDER BY l."机构"',
                10, ["link-composite@1"],
            )
            self.assertEqual(queried["rows"], [["A", 1, "违规A"], ["B", 1, "违规B"]])


class DistillBusinessCapabilitySkillTests(unittest.TestCase):
    def test_system_skill_credentials_are_materialized_without_redaction(self) -> None:
        with TemporaryDirectory() as temporary:
            target = Path(temporary)
            write_json(target / "config" / "defaults.json", {"OCR_BASE_URL": "https://ocr.example", "OCR_API_KEY": ""})
            with patch.dict(DISTILL.os.environ, {"OCR_API_KEY": "test-preserved-key"}, clear=False):
                status = DISTILL.materialize_system_skill_credentials("ocr-parser", target)
            defaults = json.loads((target / "config" / "defaults.json").read_text(encoding="utf-8"))
            self.assertEqual(defaults["OCR_API_KEY"], "test-preserved-key")
            self.assertTrue(status["all_required_credentials_configured"])
            self.assertEqual(status["fields"][0]["origin"], "environment")
            self.assertNotIn("test-preserved-key", json.dumps(status))

    def test_stage_runtime_blocks_raw_files_and_validates_handoff(self) -> None:
        contract = {
            "stage_id": "s_decide",
            "objective": "形成判断",
            "outcome": "形成结论",
            "input_contract": [{"name": "规则", "accepted_formats": [".xlsx"], "required": True}],
            "output_contract": [{"name": "审核结论", "formats": ["运行时对象"], "required": True}],
            "foundation_skills": ["scenario-tabular-reader"],
            "procedure": [],
            "control_ids": [],
            "execution_contract": {},
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "large.csv"
            raw.write_text("id,value\n1,2\n", encoding="utf-8")
            with self.assertRaisesRegex(STAGE_RUNTIME.StageRuntimeError, "raw business file"):
                STAGE_RUNTIME.start_work_order(contract, "执行审核", {"path": str(raw)})
            work_order = STAGE_RUNTIME.start_work_order(contract, "执行审核", {"rule_id": "R-1"})
            handoff = STAGE_RUNTIME.finish_work_order(
                contract,
                work_order,
                {
                    "status": "complete",
                    "outputs": [{
                        "name": "审核结论",
                        "value": {
                            "kind": "exported_query_result",
                            "path": str(root / "result.parquet"),
                            "sha256": "a" * 64,
                        },
                    }],
                    "evidence": [{"rule_id": "R-1"}],
                },
            )
            self.assertEqual(handoff["status"], "complete")
            self.assertEqual(handoff["stage_id"], "s_decide")
            self.assertEqual(len(handoff["handoff_digest"]), 64)

    def test_prepare_blocks_without_accepted_upstream(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "capability-distillation"
            code, payload = DISTILL.prepare(
                Namespace(
                    relations=str(root / "missing-relations.json"),
                    flow=str(root / "missing-flow.json"),
                    output=str(output),
                    summary_limit=30,
                )
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "blocked_missing_or_invalid_upstream")
            self.assertFalse((output / "capability-plan.template.json").exists())

    def test_tabular_only_scenario_generates_no_ocr_or_document_skill(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations, flow = upstream_fixture(root, mixed_formats=False)
            output = root / "capability-distillation"
            prepare_code, prepare_payload = DISTILL.prepare(
                Namespace(relations=str(relations), flow=str(flow), output=str(output), summary_limit=30)
            )
            self.assertEqual(prepare_code, 0, prepare_payload)
            template = json.loads((output / "capability-plan.template.json").read_text(encoding="utf-8"))
            self.assertEqual(template["generator_contract_version"], 2)
            self.assertEqual([item["kind"] for item in template["foundation_skills"]], ["tabular"])
            candidate = output / "capability-plan.candidate.json"
            write_json(candidate, fill_plan(template))
            arguments = Namespace(
                relations=str(relations), flow=str(flow), output=str(output), claims=str(candidate), summary_limit=30
            )
            preflight_code, preflight_payload = DISTILL.preflight(arguments)
            self.assertEqual(preflight_code, 0, preflight_payload)
            finalize_code, finalize_payload = DISTILL.finalize(arguments)
            self.assertEqual(finalize_code, 0, finalize_payload)
            manifest = json.loads((output / "capability-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["generator_contract_version"], 2)
            self.assertEqual(manifest["foundation_skill_count"], 1)
            self.assertEqual(manifest["stage_skill_count"], 3)
            self.assertEqual(manifest["skill_count"], 5)
            self.assertFalse(any(item.get("foundation_kind") == "ocr" for item in manifest["skills"]))

    def test_mixed_scenario_generates_custom_portable_foundations_and_stage_skills(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations, flow = upstream_fixture(root, mixed_formats=True)
            output = root / "capability-distillation"
            DISTILL.prepare(Namespace(relations=str(relations), flow=str(flow), output=str(output), summary_limit=30))
            template = json.loads((output / "capability-plan.template.json").read_text(encoding="utf-8"))
            self.assertEqual({item["kind"] for item in template["foundation_skills"]}, {"tabular", "document", "ocr"})
            document_foundation = next(item for item in template["foundation_skills"] if item["kind"] == "document")
            self.assertIn(".png", document_foundation["formats"])
            candidate = output / "capability-plan.candidate.json"
            write_json(candidate, fill_plan(template))
            arguments = Namespace(
                relations=str(relations), flow=str(flow), output=str(output), claims=str(candidate), summary_limit=30
            )
            finalize_code, finalize_payload = DISTILL.finalize(arguments)
            self.assertEqual(finalize_code, 0, finalize_payload)
            manifest = json.loads((output / "capability-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["foundation_skill_count"], 3)
            self.assertEqual(manifest["stage_skill_count"], 3)
            self.assertEqual(manifest["skill_count"], 7)
            ocr_skill = output / "skills" / "expense-review-ocr-reader"
            ocr_text = (ocr_skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("费用审核", ocr_text)
            self.assertIn(".pdf", ocr_text)
            self.assertNotIn("/workspace", ocr_text)
            self.assertNotIn("Studio", ocr_text)
            defaults = json.loads((ocr_skill / "config" / "defaults.json").read_text(encoding="utf-8"))
            source_defaults = json.loads(
                (PROJECT_ROOT / "system_skills" / "ocr-parser" / "config" / "defaults.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(defaults, source_defaults)
            self.assertEqual(defaults["OCR_API_KEY"], "")
            dependencies = manifest["runtime_requirements"]["python_dependencies"]
            self.assertTrue(any(item.startswith("duckdb") for item in dependencies))
            self.assertTrue(any(item.startswith("fastexcel") for item in dependencies))
            self.assertTrue(any(item.startswith("pypdf") for item in dependencies))
            self.assertTrue(any(item.startswith("httpx") for item in dependencies))
            prompt = (output / "agent_prompts.md").read_text(encoding="utf-8")
            self.assertIn("完整一行", prompt)
            self.assertIn("validate-join", prompt)
            self.assertIn("index-ocr", prompt)
            self.assertEqual(manifest["artifacts"]["agent_prompts"], "agent_prompts.md")
            self.assertEqual(manifest["artifact_digests"]["agent_prompts"], digest(output / "agent_prompts.md"))
            for foundation in ("tabular", "document", "ocr"):
                contract_copy = (
                    output / "skills" / f"expense-review-{foundation}-reader" /
                    "references" / "operational-data-contract.json"
                )
                self.assertTrue(contract_copy.is_file())
                copied = json.loads(contract_copy.read_text(encoding="utf-8"))
                self.assertNotIn("field_evidence", copied["source"])
            self.assertEqual(len(manifest["runtime_requirements"]["external_services"]), 1)
            ocr_service = manifest["runtime_requirements"]["external_services"][0]
            self.assertEqual(ocr_service["skill"], "expense-review-ocr-reader")
            self.assertEqual(ocr_service["kind"], "ocr_http_api")
            self.assertEqual(ocr_service["credential_fields"], ["OCR_API_KEY"])
            self.assertEqual(ocr_service["configuration_policy"], "preserved_from_system_skill_without_redaction")
            for skill in manifest["skills"]:
                skill_root = output / skill["path"]
                scripts = sorted(path.relative_to(skill_root).as_posix() for path in (skill_root / "scripts").glob("*.py"))
                self.assertTrue(scripts, skill["name"])
                self.assertEqual(skill["executables"], scripts)
            for stage_id in ("s_prepare", "s_decide", "s_result"):
                self.assertTrue(any(item.get("stage_id") == stage_id for item in manifest["skills"]))
            decision_contract = json.loads(
                (output / "skills" / "expense-review-s-decide" / "references" / "contract.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(all(item["required"] is True for item in decision_contract["output_contract"]))
            summary_code, summary_payload = DISTILL.summary(
                Namespace(
                    result=str(output / "capability-manifest.json"),
                    relations=str(relations),
                    flow=str(flow),
                    output=str(output),
                    offset=0,
                    limit=30,
                )
            )
            self.assertEqual(summary_code, 0, summary_payload)
            self.assertEqual(summary_payload["runtime_requirements"], manifest["runtime_requirements"])

            stale_manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
            stale_manifest.pop("generator_contract_version")
            write_json(output / "capability-manifest.json", stale_manifest)
            with self.assertRaisesRegex(DISTILL.ContractError, "生成器契约版本已过期"):
                DISTILL.summary(
                    Namespace(
                        result=str(output / "capability-manifest.json"),
                        relations=str(relations),
                        flow=str(flow),
                        output=str(output),
                        offset=0,
                        limit=30,
                    )
                )
            write_json(output / "capability-manifest.json", manifest)

            manifest["runtime_requirements"]["python_dependencies"] = []
            write_json(output / "capability-manifest.json", manifest)
            with self.assertRaises(DISTILL.ContractError):
                DISTILL.summary(
                    Namespace(
                        result=str(output / "capability-manifest.json"),
                        relations=str(relations),
                        flow=str(flow),
                        output=str(output),
                        offset=0,
                        limit=30,
                    )
                )

    def test_external_knowledge_node_generates_complete_customized_vector_skill(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations, flow = upstream_fixture(root, mixed_formats=False)
            relation_payload = json.loads(relations.read_text(encoding="utf-8"))
            relation_payload["nodes"].append({
                "id": "n_external_knowledge",
                "name": "外部药品知识库",
                "type": "system",
                "description": "按规则需要从外部知识库取得药品规格辅助判断",
                "evidence_ids": [],
            })
            write_json(relations, relation_payload)
            flow_payload = json.loads(flow.read_text(encoding="utf-8"))
            flow_payload["source"]["fingerprint"] = digest(relations)
            decision = next(item for item in flow_payload["stages"] if item["id"] == "s_decide")
            decision["input_node_ids"].append("n_external_knowledge")
            write_json(flow, flow_payload)

            output = root / "capability-distillation"
            prepare_code, prepare_payload = DISTILL.prepare(
                Namespace(relations=str(relations), flow=str(flow), output=str(output), summary_limit=30)
            )
            self.assertEqual(prepare_code, 0, prepare_payload)
            template = json.loads((output / "capability-plan.template.json").read_text(encoding="utf-8"))
            self.assertEqual({item["kind"] for item in template["foundation_skills"]}, {"tabular", "knowledge"})
            knowledge = next(item for item in template["foundation_skills"] if item["kind"] == "knowledge")
            self.assertEqual(knowledge["system_roles"][0]["node_id"], "n_external_knowledge")
            decision_plan = next(item for item in template["stage_skills"] if item["stage_id"] == "s_decide")
            self.assertIn("foundation-knowledge", decision_plan["foundation_ids"])

            candidate = output / "capability-plan.candidate.json"
            write_json(candidate, fill_plan(template))
            arguments = Namespace(
                relations=str(relations), flow=str(flow), output=str(output), claims=str(candidate), summary_limit=30
            )
            code, payload = DISTILL.finalize(arguments)
            self.assertEqual(code, 0, payload)
            manifest = json.loads((output / "capability-manifest.json").read_text(encoding="utf-8"))
            generated = output / "skills" / "expense-review-knowledge-reader"
            source = PROJECT_ROOT / "system_skills" / "vector-kb"
            self.assertEqual(
                json.loads((generated / "config" / "defaults.json").read_text(encoding="utf-8")),
                json.loads((source / "config" / "defaults.json").read_text(encoding="utf-8")),
            )
            self.assertTrue((generated / "scripts" / "kb_client.py").is_file())
            self.assertTrue((generated / "scripts" / "scenario_kb.py").is_file())
            self.assertIn("费用审核", (generated / "SKILL.md").read_text(encoding="utf-8"))
            knowledge_manifest = next(item for item in manifest["skills"] if item.get("foundation_kind") == "knowledge")
            self.assertEqual(knowledge_manifest["source_skill"], "vector-kb")
            self.assertIn("scripts/kb_client.py", knowledge_manifest["inherited_resources"])
            self.assertEqual(
                knowledge_manifest["credential_status"]["fields"][0]["configured"],
                bool(json.loads((source / "config" / "defaults.json").read_text(encoding="utf-8"))["api_key"]),
            )
            service = next(
                item for item in manifest["runtime_requirements"]["external_services"]
                if item["kind"] == "vector_kb_http_api"
            )
            self.assertEqual(service["skill"], "expense-review-knowledge-reader")

    def test_legacy_result_sample_is_not_reclassified_as_external_runtime_input(self) -> None:
        relations = {
            "nodes": [
                {
                    "id": "n_external",
                    "name": "External drug specification reference",
                    "type": "object",
                    "description": "Knowledge base or crawler enrichment selected from the complete rule",
                },
                {"id": "n_result", "name": "Audit result", "type": "output", "description": "Output"},
            ]
        }
        operational = {
            "schema_version": 1,
            "status": "ready",
            "sources": [{
                "source_id": "src-result",
                "path": "historical-result.xlsx",
                "kind": "tabular",
                "extension": ".xlsx",
                "roles": [
                    {"node_id": "n_external", "node_type": "object", "node_name": "External reference"},
                    {"node_id": "n_result", "node_type": "output", "node_name": "Audit result"},
                ],
                "tables": [{"columns": [{"name": "finding_id", "query_name": "finding_id"}]}],
            }],
            "links": [],
            "semantic_routes": [],
            "result_source_ids": ["src-result"],
        }
        normalized = DISTILL.normalize_operational_runtime_contract(relations, operational)
        source = normalized["sources"][0]
        self.assertEqual(source["roles"], [
            {"node_id": "n_result", "node_type": "output", "node_name": "Audit result"}
        ])
        self.assertEqual(source["lifecycle"], "design_time_template")
        self.assertFalse(source["runtime_required"])
        self.assertEqual(normalized["runtime_source_ids"], [])
        self.assertEqual(normalized["template_source_ids"], ["src-result"])
        self.assertEqual(normalized["external_capabilities"][0]["lifecycle"], "optional_enrichment")
        self.assertEqual(
            normalized["external_capabilities"][0]["failure_policy"],
            "manual_intervention_required_when_mandatory_and_unavailable",
        )

    def test_preflight_rejects_optional_stage_output(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations, flow = upstream_fixture(root, mixed_formats=False)
            output = root / "capability-distillation"
            DISTILL.prepare(Namespace(relations=str(relations), flow=str(flow), output=str(output), summary_limit=30))
            template = json.loads((output / "capability-plan.template.json").read_text(encoding="utf-8"))
            claims = fill_plan(template)
            claims["stage_skills"][0]["output_contract"][0]["required"] = False
            candidate = output / "capability-plan.candidate.json"
            write_json(candidate, claims)
            code, payload = DISTILL.preflight(
                Namespace(relations=str(relations), flow=str(flow), output=str(output), claims=str(candidate), summary_limit=30)
            )
            self.assertEqual(code, 2)
            self.assertTrue(any("降级为可选" in item for item in payload["errors"]))

    def test_preflight_rejects_missing_stage_and_platform_coupling_is_scanned(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations, flow = upstream_fixture(root, mixed_formats=False)
            output = root / "capability-distillation"
            DISTILL.prepare(Namespace(relations=str(relations), flow=str(flow), output=str(output), summary_limit=30))
            template = json.loads((output / "capability-plan.template.json").read_text(encoding="utf-8"))
            claims = fill_plan(template)
            claims["stage_skills"] = claims["stage_skills"][:-1]
            candidate = output / "capability-plan.candidate.json"
            write_json(candidate, claims)
            code, payload = DISTILL.preflight(
                Namespace(relations=str(relations), flow=str(flow), output=str(output), claims=str(candidate), summary_limit=30)
            )
            self.assertEqual(code, 2)
            self.assertTrue(any("每个流程阶段" in item for item in payload["errors"]))


if __name__ == "__main__":
    unittest.main()
