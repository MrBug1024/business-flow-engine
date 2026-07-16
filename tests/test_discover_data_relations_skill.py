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
            sys.executable, str(SCRIPT), "analyze", "--input", str(data), "--output", str(output),
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
