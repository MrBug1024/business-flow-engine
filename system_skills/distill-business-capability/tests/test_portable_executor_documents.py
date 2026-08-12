"""Regression checks for document and hybrid main-executor routing."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_PATH = (
    ROOT / "assets" / "portable-scenario-executor" / "scripts" / "execute_scenario.py"
)
SPEC = importlib.util.spec_from_file_location("portable_executor_documents", EXECUTOR_PATH)
assert SPEC and SPEC.loader
EXECUTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXECUTOR)


def flow_contract() -> dict:
    return {
        "execution_mode": "evidence_pipeline",
        "main_flow": [],
        "stages": [],
        "controls": [],
        "output_contract": {},
    }


def document_source(source_id: str = "policy") -> dict:
    return {
        "source_id": source_id,
        "kind": "document",
        "path": "policy.md",
        "lifecycle": "runtime_input",
        "runtime_required": True,
        "roles": [{"node_type": "rule"}],
    }


class PortableExecutorDocumentTests(unittest.TestCase):
    def test_document_only_returns_provenance_hits_in_agent_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data_root = Path(raw)
            (data_root / "policy.md").write_text(
                "# 差旅报销制度\n住宿费用应提供合规发票，并按出差天数审核。\n",
                encoding="utf-8",
            )
            contract = {
                "sources": [document_source()],
                "runtime_source_ids": ["policy"],
                "rule_source_ids": ["policy"],
            }
            payload = EXECUTOR.execute(
                "请依据差旅报销制度审核住宿费用", data_root, contract, flow_contract(), {}, 10, True,
            )

            self.assertEqual("ready_for_agent_judgment", payload["status"])
            source = payload["document_evidence"]["sources"][0]
            self.assertGreater(source["hit_count_returned"], 0)
            hit = source["hits"][0]
            self.assertTrue(all(hit.get(key) for key in ("source_digest", "locator", "chunk_id", "text_digest")))
            handoff = EXECUTOR.agent_handoff_payload(payload)
            handoff_hit = handoff["document_evidence"]["sources"][0]["hits"][0]
            self.assertTrue(all(handoff_hit.get(key) for key in ("source_digest", "locator", "chunk_id", "text_digest")))
            compact = EXECUTOR.compact_stdout_payload({**payload, "artifact": {"path": "result.json"}})
            self.assertGreater(compact["document_evidence"]["hit_count_returned"], 0)
            contract_path = data_root / "contract.json"
            flow_path = data_root / "flow.json"
            contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
            flow_path.write_text(json.dumps(flow_contract(), ensure_ascii=False), encoding="utf-8")
            bundle_scripts = data_root / "standalone-bundle" / "scripts"
            bundle_scripts.mkdir(parents=True)
            for name in ("execute_scenario.py", "recipe_runtime.py"):
                shutil.copy2(EXECUTOR_PATH.parent / name, bundle_scripts / name)
            shutil.copy2(
                ROOT / "assets" / "portable-document-reader" / "scripts" / "extract_documents.py",
                bundle_scripts / "extract_documents.py",
            )
            completed = subprocess.run(
                [
                    sys.executable, str(bundle_scripts / "execute_scenario.py"),
                    "--contract", str(contract_path),
                    "--flow-contract", str(flow_path),
                    "--recipes", str(data_root / "missing-recipes.json"),
                    "execute", "--request", "请依据差旅报销制度审核住宿费用",
                    "--data-root", str(data_root),
                ],
                capture_output=True,
                check=False,
                encoding="utf-8",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("ready_for_agent_judgment", json.loads(completed.stdout)["status"])

    def test_sparse_pdf_is_a_non_retryable_ocr_blocker(self) -> None:
        try:
            from pypdf import PdfWriter
        except ImportError as exc:  # pragma: no cover - package dependency contract
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as raw:
            data_root = Path(raw)
            pdf_path = data_root / "policy.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as handle:
                writer.write(handle)
            source = document_source()
            source["path"] = "policy.pdf"
            contract = {
                "sources": [source],
                "runtime_source_ids": ["policy"],
                "rule_source_ids": ["policy"],
            }
            payload = EXECUTOR.execute("审核差旅报销制度", data_root, contract, flow_contract(), {}, 10, True)

            self.assertEqual("blocked_ocr_required", payload["status"])
            document = payload["document_evidence"]["sources"][0]
            self.assertEqual("blocked_ocr_required", document["status"])
            self.assertTrue(document["ocr"]["required"])

    def test_hybrid_tabular_path_keeps_document_evidence_visible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data_root = Path(raw)
            (data_root / "rules.csv").write_text(
                "rule_id,title,detail\nR-1,差旅住宿报销,住宿费用需要审核\n",
                encoding="utf-8",
            )
            (data_root / "policy.md").write_text(
                "差旅住宿报销审核需核验出差天数和合规发票。\n",
                encoding="utf-8",
            )
            rules = {
                "source_id": "rules",
                "kind": "tabular",
                "path": "rules.csv",
                "view_name": "rules_view",
                "lifecycle": "runtime_input",
                "runtime_required": True,
                "roles": [{"node_type": "rule"}],
                "tables": [{
                    "columns": [
                        {"name": "rule_id", "query_name": "rule_id", "kind": "id"},
                        {"name": "title", "query_name": "title", "kind": "other"},
                        {"name": "detail", "query_name": "detail", "kind": "other"},
                    ],
                }],
            }
            document = document_source()
            document["roles"] = [{"node_type": "input"}]
            contract = {
                "sources": [rules, document],
                "runtime_source_ids": ["rules", "policy"],
                "rule_source_ids": ["rules"],
            }
            payload = EXECUTOR.execute(
                "审核差旅住宿报销", data_root, contract, flow_contract(), {}, 10, True,
            )

            self.assertIn(payload["status"], {"blocked_uncompiled_rule_family", "ready_for_agent_judgment"})
            source = payload["document_evidence"]["sources"][0]
            self.assertGreater(source["hit_count_returned"], 0)
            handoff = EXECUTOR.agent_handoff_payload(payload)
            self.assertGreater(handoff["document_evidence"]["sources"][0]["hit_count_returned"], 0)


if __name__ == "__main__":
    unittest.main()
