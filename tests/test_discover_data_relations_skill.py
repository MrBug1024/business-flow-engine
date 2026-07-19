from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "system_skills" / "discover-data-relations"
SCRIPT_DIR = SKILL_ROOT / "scripts"
SCRIPT = SCRIPT_DIR / "analyze_relations.py"
ENGINE = SCRIPT_DIR / "progressive_engine.py"
SCENARIO_ENGINE = SCRIPT_DIR / "scenario_engine.py"


def load_engine():
    spec = importlib.util.spec_from_file_location("progressive_engine_test", ENGINE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def arguments(**overrides):
    defaults = {
        "goal_file": "",
        "ocr_mode": "never",
        "deadline_seconds": 0,
        "resume": True,
        "seed_cell_budget": 100,
        "seed_file_bytes": 200,
        "seed_values_per_column": 32,
        "max_seed_values": 256,
        "max_text_seeds": 64,
        "profile_size": 16,
        "frontier_values_per_column": 16,
        "min_expansion_distinct_ratio": 0.01,
        "max_matches_per_seed": 10,
        "bootstrap_rows": 20,
        "checkpoint_rows": 100,
        "document_character_budget": 100_000,
        "summary_limit": 20,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def write_generic_chain(data: Path, noise_rows: int = 2_000) -> None:
    (data / "policy.csv").write_text(
        "product_code,rule_id\nSKU-ALPHA-0001,RULE-900001\n",
        encoding="utf-8",
    )
    event_lines = ["product_code,transaction_id,event_type"]
    event_lines.extend(f"SKU-NOISE-{index:06d},TX-NOISE-{index:06d},view" for index in range(noise_rows))
    event_lines.append("SKU-ALPHA-0001,TX-LINK-880001,purchase")
    (data / "events.csv").write_text("\n".join(event_lines) + "\n", encoding="utf-8")
    order_lines = ["transaction_id,customer_id,status"]
    order_lines.extend(f"TX-OTHER-{index:06d},CUSTOMER-{index:06d},closed" for index in range(noise_rows // 2))
    order_lines.append("TX-LINK-880001,CUSTOMER-LINK-01,open")
    (data / "orders.csv").write_text("\n".join(order_lines) + "\n", encoding="utf-8")


def test_progressive_seed_scan_builds_a_generic_multihop_chain(tmp_path):
    module = load_engine()
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    write_generic_chain(data)

    result = module.ProgressiveAnalyzer(data, output, arguments()).run()

    assert result["status"] == "complete"
    linked_pairs = {(item["source"], item["target"]) for item in result["relations"]}
    assert ("events.csv", "policy.csv") in linked_pairs
    assert ("events.csv", "orders.csv") in linked_pairs
    assert any(len(chain["files"]) == 3 for chain in result["chains"])
    assert result["coverage"]["seed_value_count"] <= 256
    assert result["coverage"]["profile_value_count"] <= 16 * 6


def test_large_noise_does_not_create_a_full_value_database(tmp_path):
    module = load_engine()
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    write_generic_chain(data, noise_rows=30_000)

    result = module.ProgressiveAnalyzer(data, output, arguments(checkpoint_rows=5_000)).run()

    assert result["status"] == "complete"
    assert (output / "evidence.sqlite3").stat().st_size < 2 * 1024 * 1024
    assert (output / "progress.json").stat().st_size < 2 * 1024 * 1024
    connection = sqlite3.connect(output / "evidence.sqlite3")
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert tables == {"relations", "evidence"}
    assert "SKU-ALPHA-0001" not in (output / "relations.json").read_text(encoding="utf-8")


def test_partial_run_resumes_from_a_row_checkpoint(tmp_path, monkeypatch):
    module = load_engine()
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    write_generic_chain(data, noise_rows=500)
    first = module.ProgressiveAnalyzer(data, output, arguments(checkpoint_rows=10))
    calls = {"count": 0}

    def stop_once():
        calls["count"] += 1
        if calls["count"] == 3:
            raise module.DeadlineReached

    monkeypatch.setattr(first, "check_deadline", stop_once)
    partial = first.run()
    assert partial["status"] == "partial"
    assert partial["coverage"]["partial_table"], partial["scan_stats"]

    second = module.ProgressiveAnalyzer(data, output, arguments())
    resumed = second.run()
    assert resumed["status"] == "complete"
    assert second.resumed is True
    assert second.partial_table is None
    assert any(item["type"] == "seeded_value_link" for item in second.relations.values())


def test_xlsx_uses_generic_column_semantics_and_fast_engine(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    pytest.importorskip("duckdb")
    module = load_engine()
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    (data / "mapping.csv").write_text("asset_code\nASSET-LINK-001\n", encoding="utf-8")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Assets"
    sheet.append(["asset_code", "owner_id", "description"])
    for index in range(200):
        sheet.append([f"ASSET-NOISE-{index:05d}", f"OWNER-{index:05d}", "noise"])
    sheet.append(["ASSET-LINK-001", "OWNER-LINK-9", "matched"])
    workbook.save(data / "assets.xlsx")

    result = module.ProgressiveAnalyzer(data, output, arguments(seed_file_bytes=50)).run()

    table = next(item for item in result["files"] if item["path"] == "assets.xlsx")["tables"][0]
    assert table["engine"] in {"fastexcel", "duckdb", "openpyxl"}
    assert any({item["source"], item["target"]} == {"mapping.csv", "assets.xlsx"} for item in result["relations"])
    stat = next(value for key, value in result["scan_stats"].items() if key.endswith(":Assets"))
    assert stat["candidate_columns"] == ["asset_code", "owner_id"]


def test_ocr_cooperation_keeps_raw_text_out_of_artifacts(tmp_path, monkeypatch):
    module = load_engine()
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    secret = "DOC-LINK-778899"
    (data / "index.csv").write_text(f"document_id\n{secret}\n", encoding="utf-8")
    (data / "scan.png").write_bytes(b"fake-image")
    fake = tmp_path / "fake_ocr.py"
    fake.write_text(f"print('document_id: {secret}')\n", encoding="utf-8")
    monkeypatch.setattr(module, "find_ocr_script", lambda: fake)

    result = module.ProgressiveAnalyzer(data, output, arguments(ocr_mode="auto", seed_file_bytes=1000)).run()

    assert any({item["source"], item["target"]} == {"index.csv", "scan.png"} for item in result["relations"])
    assert secret not in (output / "relations.json").read_text(encoding="utf-8")
    assert secret.encode("utf-8") not in (output / "evidence.sqlite3").read_bytes()


def test_cli_summary_is_bounded(tmp_path):
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    write_generic_chain(data, noise_rows=200)
    completed = subprocess.run(
        [
            sys.executable, str(ENGINE), "analyze", "--input", str(data), "--output", str(output),
            "--goal-file", "", "--ocr-mode", "never", "--deadline-seconds", "0",
            "--seed-cell-budget", "100", "--seed-file-bytes", "200", "--summary-limit", "1",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)
    assert payload["relation_count"] >= 2
    assert len(payload["relation_page"]["items"]) == 1
    assert "SKU-ALPHA-0001" not in completed.stdout


def test_low_cardinality_keys_do_not_expand_the_frontier(tmp_path):
    module = load_engine()
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    (data / "seed.csv").write_text("group_code\nGROUP-LINK-001\n", encoding="utf-8")
    bridge = ["group_code,operator_id,record_id"]
    bridge.extend(
        f"GROUP-LINK-001,OPERATOR-COMMON,RECORD-{index:06d}"
        for index in range(1_000)
    )
    (data / "bridge.csv").write_text("\n".join(bridge) + "\n", encoding="utf-8")
    target = ["operator_id,target_id"]
    target.extend(f"OPERATOR-COMMON,TARGET-{index:06d}" for index in range(500))
    (data / "target.csv").write_text("\n".join(target) + "\n", encoding="utf-8")

    analyzer = module.ProgressiveAnalyzer(
        data,
        output,
        arguments(max_matches_per_seed=5, checkpoint_rows=100),
    )
    result = analyzer.run()

    seeded_pairs = {
        frozenset((item["source"], item["target"]))
        for item in result["relations"]
        if item["type"] == "seeded_value_link"
    }
    assert frozenset(("seed.csv", "bridge.csv")) in seeded_pairs
    assert frozenset(("bridge.csv", "target.csv")) not in seeded_pairs
    bridge_table = next(table for table in analyzer.tables.values() if table.file_path == "bridge.csv")
    operator_stats = analyzer.column_stats[f"{bridge_table.key}\0operator_id"]
    assert operator_stats.ratio() < 0.01
    assert result["scan_stats"][bridge_table.key]["matches"] <= 5


def load_scenario_engine():
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        spec = importlib.util.spec_from_file_location("scenario_engine_test", SCENARIO_ENGINE)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPT_DIR))


def fake_scenario_field_result() -> dict:
    return {
        "schema_version": 3,
        "status": "complete",
        "coverage": {"files_discovered": 3, "tables_discovered": 2, "tables_completed": 2},
        "files": [
            {
                "path": "requests.csv", "kind": "tabular", "extension": ".csv", "size": 200,
                "tables": [{
                    "table_name": "requests.csv", "row_count": 4, "column_count": 5,
                    "columns": [
                        {"name": "申请材料", "kind": "other"}, {"name": "提交时间", "kind": "other"},
                        {"name": "申请人", "kind": "other"}, {"name": "审核状态", "kind": "other"},
                        {"name": "结果通知", "kind": "other"},
                    ],
                }],
            },
            {
                "path": "rules.csv", "kind": "tabular", "extension": ".csv", "size": 160,
                "tables": [{
                    "table_name": "rules.csv", "row_count": 2, "column_count": 3,
                    "columns": [
                        {"name": "适用条件", "kind": "other"}, {"name": "审核规则", "kind": "other"},
                        {"name": "拒绝原因", "kind": "other"},
                    ],
                }],
            },
            {"path": "guide.md", "kind": "document", "extension": ".md", "size": 240, "tables": []},
        ],
        "relations": [{
            "id": "R-low-level", "source": "requests.csv", "target": "rules.csv",
            "source_column": "审核状态", "target_column": "审核状态", "type": "seeded_value_link",
            "verdict": "confirmed", "confidence": 0.96, "evidence_count": 3,
            "evidence": [{
                "source_locator": "table:requests.csv;row:2;column:审核状态",
                "target_locator": "table:rules.csv;row:2;column:审核状态",
            }],
        }],
    }


def scenario_arguments(data: Path, output: Path, field_result: Path) -> argparse.Namespace:
    return argparse.Namespace(
        input=str(data), output=str(output), goal_file="", field_result=str(field_result),
        ocr_mode="never", deadline_seconds=0, resume=True, seed_cell_budget=100,
        seed_file_bytes=1_000, seed_values_per_column=16, max_seed_values=100,
        max_text_seeds=20, profile_size=16, frontier_values_per_column=16,
        min_expansion_distinct_ratio=0.01, max_matches_per_seed=10, bootstrap_rows=20,
        checkpoint_rows=100, document_character_budget=10_000, document_cards_per_file=8,
        semantic_table_cell_budget=1_000, table_character_budget=10_000, table_cards_per_file=8,
        max_evidence_cards=100, summary_limit=10,
    )


def build_scenario_evidence(tmp_path: Path):
    module = load_scenario_engine()
    data, output = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    (data / "requests.csv").write_text("申请材料,提交时间,申请人,审核状态,结果通知\n", encoding="utf-8")
    (data / "rules.csv").write_text(
        "适用条件,审核规则,拒绝原因\n材料完整,审核必须依据规则，未通过则拒绝,缺少材料\n",
        encoding="utf-8",
    )
    (data / "guide.md").write_text(
        "# 办理说明\n申请人提交材料后触发审核。审核依据规则表进行校验；如果通过则生成结果通知，否则进入拒绝处理。\n",
        encoding="utf-8",
    )
    field_result = tmp_path / "field-result.json"
    field_result.write_text(json.dumps(fake_scenario_field_result(), ensure_ascii=False), encoding="utf-8")
    prepared = module.prepare_evidence(scenario_arguments(data, output, field_result))
    payload = json.loads((output / "evidence-cards.json").read_text(encoding="utf-8"))
    return module, data, output, prepared, payload


def scenario_card_ids(payload: dict) -> dict[str, str]:
    by_kind: dict[str, list[dict]] = {}
    for card in payload["cards"]:
        by_kind.setdefault(card["kind"], []).append(card)
    document = by_kind["document_relation_statement"][0]["id"]
    request_schema = next(card["id"] for card in by_kind["table_schema"] if card["sources"][0]["file"] == "requests.csv")
    request_signal = next(card["id"] for card in by_kind["table_process_signal"] if card["sources"][0]["file"] == "requests.csv")
    rule_schema = next(card["id"] for card in by_kind["table_schema"] if card["sources"][0]["file"] == "rules.csv")
    field_link = by_kind["field_relationship"][0]["id"]
    file_cards = {card["sources"][0]["file"]: card["id"] for card in by_kind["file_structure"]}
    return {
        "document": document,
        "request_schema": request_schema,
        "request_signal": request_signal,
        "rule_schema": rule_schema,
        "field_link": field_link,
        **{f"file:{key}": value for key, value in file_cards.items()},
    }


def valid_scenario_claims(payload: dict) -> dict:
    ids = scenario_card_ids(payload)
    document = ids["document"]
    return {
        "schema_version": 1,
        "scenario": {"name": "申请审核", "purpose": "从材料提交到结果通知的整体业务关系"},
        "nodes": [
            {"id": "n_submit", "name": "材料提交", "type": "input", "description": "业务输入", "evidence_ids": [document, ids["request_schema"]]},
            {"id": "n_review", "name": "规则审核", "type": "decision", "description": "按规则校验", "evidence_ids": [document, ids["rule_schema"]]},
            {"id": "n_notice", "name": "结果通知", "type": "output", "description": "主链输出", "evidence_ids": [document, ids["request_schema"]]},
            {"id": "n_reject", "name": "拒绝处理", "type": "state", "description": "未通过分支", "evidence_ids": [document, ids["rule_schema"]]},
        ],
        "edges": [
            {"id": "e_trigger", "source": "n_submit", "target": "n_review", "type": "triggers", "label": "提交后触发审核", "confidence": 0.96, "evidence_ids": [document]},
            {"id": "e_output", "source": "n_review", "target": "n_notice", "type": "produces", "label": "审核通过生成通知", "confidence": 0.93, "evidence_ids": [document]},
            {"id": "e_reject", "source": "n_review", "target": "n_reject", "type": "branches_to", "label": "未通过进入拒绝处理", "confidence": 0.92, "evidence_ids": [document]},
            {"id": "e_rule", "source": "n_review", "target": "n_notice", "type": "depends_on", "label": "材料与规则可追溯", "confidence": 0.88, "evidence_ids": [ids["field_link"]]},
        ],
        "main_chain": ["n_submit", "n_review", "n_notice"],
        "branches": [{"id": "b_reject", "from": "n_review", "condition": "审核未通过", "path": ["n_reject"], "evidence_ids": [document]}],
        "coverage": {"included_files": ["requests.csv", "rules.csv", "guide.md"], "excluded_files": []},
    }


def test_scenario_prepare_distills_cross_format_evidence_without_raw_rows(tmp_path):
    module, _data, output, prepared, payload = build_scenario_evidence(tmp_path)

    assert prepared["status"] == "ready_for_synthesis"
    kinds = {card["kind"] for card in payload["cards"]}
    assert {
        "table_schema", "table_process_signal", "field_relationship",
        "table_relation_statement", "document_relation_statement",
    } <= kinds
    assert payload["card_count"] <= 100
    serialized = (output / "evidence-cards.json").read_text(encoding="utf-8")
    assert "触发审核" in serialized
    assert "R-low-level" in serialized
    assert "row:2" in serialized
    assert "申请材料,提交时间" not in serialized
    assert max(len(card.get("snippet", "")) for card in payload["cards"]) <= 323
    brief = prepared["synthesis_brief"]
    assert brief["selected_card_count"] <= 40
    assert brief["coverage"]["file_count"] == 3
    brief_text = json.dumps(brief, ensure_ascii=False)
    assert "field_relation_ids" not in brief_text
    assert "document_relation_statement" in {card["kind"] for card in brief["cards"]}
    assert (output / "synthesis-brief.json").exists()

    oversized_page = module.evidence_page(
        {"cards": payload["cards"] * 10},
        offset=0,
        limit=50,
    )
    assert oversized_page["limit"] == 20
    assert len(oversized_page["items"]) == 20


def test_scenario_finalize_writes_one_main_chain_and_attached_branch(tmp_path):
    module, _data, output, _prepared, payload = build_scenario_evidence(tmp_path)
    canonical_claims_path = output / "scenario-claims.json"
    canonical_claims_path.write_text("{}", encoding="utf-8")
    claims_path = output / "scenario-claims.candidate.json"
    claims_path.write_text(json.dumps(valid_scenario_claims(payload), ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(output=str(output), cards="", claims=str(claims_path), summary_limit=20)

    code, summary = module.finalize(args)

    assert code == 0
    assert summary["main_chain"] == ["n_submit", "n_review", "n_notice"]
    assert summary["branch_count"] == 1
    assert not claims_path.exists()
    promoted_claims = json.loads(canonical_claims_path.read_text(encoding="utf-8"))
    assert promoted_claims["main_chain"] == summary["main_chain"]
    result = json.loads((output / "scenario-relationship.json").read_text(encoding="utf-8"))
    assert result["strategy"] == "bounded_evidence_semantic_synthesis"
    assert "材料提交 → 规则审核 → 结果通知" in (output / "relation-report.md").read_text(encoding="utf-8")
    connection = sqlite3.connect(output / "evidence.sqlite3")
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert tables == {"nodes", "edges", "evidence_cards", "claim_evidence"}


def test_claim_mutation_cli_builds_a_candidate_without_large_file_writes(tmp_path):
    module, _data, output, _prepared, payload = build_scenario_evidence(tmp_path)
    candidate = output / "scenario-claims.candidate.json"
    cards = output / "evidence-cards.json"
    evidence_id = payload["cards"][0]["id"]

    assert module.main([
        "claims-init", "--claims", str(candidate), "--name", "Demo scenario",
        "--purpose", "Bounded mutation workflow",
    ]) == 0
    assert module.main([
        "claims-node", "--claims", str(candidate), "--id", "n_input",
        "--name", "Request", "--node-type", "input", "--description", "Stable input",
        "--evidence-ids", evidence_id,
    ]) == 0
    assert module.main([
        "claims-node", "--claims", str(candidate), "--id", "n_input",
        "--node-type", "trigger",
    ]) == 0
    assert module.main([
        "claims-edge", "--claims", str(candidate), "--id", "e_start",
        "--source", "n_input", "--target", "n_result", "--edge-type", "triggers",
        "--label", "Request starts processing", "--confidence", "0.9",
        "--evidence-ids", evidence_id,
    ]) == 0
    assert module.main([
        "claims-chain", "--claims", str(candidate), "--node-ids", "n_input,n_result",
    ]) == 0
    assert module.main([
        "claims-branch", "--claims", str(candidate), "--id", "b_review",
        "--from-node", "n_input", "--condition", "Needs review", "--path-ids", "n_review",
        "--evidence-ids", evidence_id,
    ]) == 0
    assert module.main([
        "claims-coverage", "--claims", str(candidate), "--cards", str(cards), "--include-all",
    ]) == 0
    assert module.main([
        "claims-exclusion", "--claims", str(candidate), "--file", "duplicate.csv",
        "--reason", "Exact duplicate", "--evidence-ids", evidence_id,
    ]) == 0
    assert module.main([
        "claims-remove", "--claims", str(candidate), "--kind", "branch", "--id", "b_review",
    ]) == 0

    claims = json.loads(candidate.read_text(encoding="utf-8"))
    assert claims["nodes"] == [{
        "id": "n_input", "name": "Request", "type": "trigger",
        "description": "Stable input", "evidence_ids": [evidence_id],
    }]
    assert claims["main_chain"] == ["n_input", "n_result"]
    assert claims["branches"] == []
    assert claims["coverage"]["included_files"]
    assert claims["coverage"]["excluded_files"][0]["file"] == "duplicate.csv"

    copied = output / "scenario-claims.candidate-2.json"
    assert module.main([
        "claims-copy", "--source", str(candidate), "--claims", str(copied),
    ]) == 0
    assert json.loads(copied.read_text(encoding="utf-8")) == claims
    assert module.main([
        "claims-copy", "--source", str(candidate), "--claims", str(copied),
    ]) == 1
    assert module.main([
        "claims-init", "--claims", str(candidate), "--name", "Overwrite",
        "--purpose", "Must be rejected",
    ]) == 1
    assert json.loads(candidate.read_text(encoding="utf-8")) == claims


def test_scenario_validation_rejects_record_values_bad_directions_open_branches_and_weak_order_claims(tmp_path):
    module, _data, _output, _prepared, payload = build_scenario_evidence(tmp_path)
    claims = valid_scenario_claims(payload)
    ids = scenario_card_ids(payload)
    claims["nodes"].append({
        "id": "n_record", "name": "ORDER-20260716-99881", "type": "object",
        "description": "single row", "evidence_ids": [ids["field_link"]],
    })
    claims["edges"][0]["evidence_ids"] = [ids["field_link"]]
    claims["nodes"].extend([
        {
            "id": "n_rule", "name": "审核规则", "type": "rule",
            "description": "业务规则", "evidence_ids": [ids["rule_schema"]],
        },
        {
            "id": "n_lookup", "name": "辅助资料查询", "type": "input",
            "description": "分支中的中间输入", "evidence_ids": [ids["document"]],
        },
    ])
    claims["edges"].extend([
        {
            "id": "e_bad_rule_direction", "source": "n_rule", "target": "n_review",
            "type": "governed_by", "label": "方向写反", "confidence": 0.9,
            "evidence_ids": [ids["document"]],
        },
        {
            "id": "e_open_lookup", "source": "n_review", "target": "n_lookup",
            "type": "branches_to", "label": "查询辅助资料", "confidence": 0.9,
            "evidence_ids": [ids["document"]],
        },
    ])
    claims["branches"].append({
        "id": "b_open_lookup", "from": "n_review", "condition": "需要辅助资料",
        "path": ["n_lookup"], "evidence_ids": [ids["document"]],
    })

    errors = module.validate_claims(claims, payload)

    assert any("record value or code" in error for error in errors)
    assert any("disconnected" in error for error in errors)
    assert any("lacks evidence strong enough" in error for error in errors)
    assert any("invalid governed_by direction" in error for error in errors)
    assert any("must end at output/state/object or return" in error for error in errors)


def test_scenario_validation_rejects_dense_non_macro_graph(tmp_path):
    module, _data, _output, _prepared, payload = build_scenario_evidence(tmp_path)
    claims = valid_scenario_claims(payload)
    evidence_id = scenario_card_ids(payload)["document"]
    for index in range(7):
        claims["nodes"].append({
            "id": f"n_field_{index}",
            "name": f"字段概念{index}",
            "type": "object",
            "description": "不应进入宏观图的低层概念",
            "evidence_ids": [evidence_id],
        })

    errors = module.validate_claims(claims, payload)

    assert any("at most 10 nodes" in error for error in errors)
    assert any("merge field/table-level concepts" in error for error in errors)


def test_scenario_cli_pages_evidence_and_rejects_invalid_claims(tmp_path):
    _module, _data, output, _prepared, payload = build_scenario_evidence(tmp_path)
    page = subprocess.run(
        [sys.executable, str(SCRIPT), "evidence", "--cards", str(output / "evidence-cards.json"), "--offset", "0", "--limit", "2"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    page_payload = json.loads(page.stdout)
    assert len(page_payload["evidence_page"]["items"]) == 2
    assert page_payload["evidence_page"]["has_more"] is True

    brief = subprocess.run(
        [sys.executable, str(SCRIPT), "brief", "--brief", str(output / "synthesis-brief.json")],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    brief_payload = json.loads(brief.stdout)
    assert brief_payload["status"] == "ready_for_synthesis"
    assert brief_payload["selected_card_count"] <= 40
    assert "\n  " not in brief.stdout

    claims = valid_scenario_claims(payload)
    claims["main_chain"] = ["n_submit", "n_notice"]
    claims_path = output / "invalid-claims.json"
    claims_path.write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "finalize", "--claims", str(claims_path), "--output", str(output)],
        check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["status"] == "validation_failed"
