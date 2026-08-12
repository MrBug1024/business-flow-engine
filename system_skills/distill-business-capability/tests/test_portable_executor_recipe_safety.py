"""Regression checks for catalog-bound deterministic recipe execution."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_PATH = (
    ROOT / "assets" / "portable-scenario-executor" / "scripts" / "execute_scenario.py"
)
RUNTIME_PATH = (
    ROOT / "assets" / "portable-scenario-executor" / "scripts" / "recipe_runtime.py"
)
EXECUTOR_SPEC = importlib.util.spec_from_file_location("portable_executor_recipe_safety", EXECUTOR_PATH)
assert EXECUTOR_SPEC and EXECUTOR_SPEC.loader
EXECUTOR = importlib.util.module_from_spec(EXECUTOR_SPEC)
EXECUTOR_SPEC.loader.exec_module(EXECUTOR)
RUNTIME_SPEC = importlib.util.spec_from_file_location("portable_recipe_runtime_safety", RUNTIME_PATH)
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
sys.modules[RUNTIME_SPEC.name] = RUNTIME
RUNTIME_SPEC.loader.exec_module(RUNTIME)


def flow_contract() -> dict:
    return {
        "execution_mode": "evidence_pipeline",
        "main_flow": [],
        "stages": [],
        "controls": [],
        "output_contract": {},
    }


def generated_tabular_source(source_id: str, path: str, view_name: str, columns: list[str]) -> dict:
    return {
        "source_id": source_id,
        "kind": "tabular",
        "path": path,
        "view_name": view_name,
        "lifecycle": "runtime_input",
        "runtime_required": True,
        "tables": [{
            "columns": [
                {"name": column, "query_name": column, "kind": "other"}
                for column in columns
            ],
        }],
    }


def recipe_catalog() -> dict:
    return {
        "schema_version": 1,
        "recipes": [{
            "id": "recipe-duplicate-alpha-beta",
            "kind": "grouped_cooccurrence",
            "rule_selector": {
                "source_id": "rules",
                "equals": {"rule_id": "R-1"},
            },
            "source_id": "charges",
            "group_by": ["visit_id"],
            "all_of": [{"field": "item", "operator": "contains", "value": "alpha"}],
            "any_of": [{"field": "item", "operator": "contains", "value": "beta"}],
            "summary_measure": "amount",
            "result_fields": ["visit_id", "item", "amount"],
            "result_annotations": {},
        }],
    }


class PortableExecutorRecipeSafetyTests(unittest.TestCase):
    def test_oversized_stdout_keeps_compact_handoff_without_an_artifact(self) -> None:
        payload = {
            "status": "ready_for_agent_judgment",
            "request": "review duplicate alpha beta",
            "rule_selection": "unique",
            "selected_rule": {"source_id": "rules", "row": {"rule_id": "R-1"}},
            "candidate_evidence": {
                "status": "ready",
                "sources": [{
                    "source_id": "charges",
                    "columns": ["item"],
                    "rows": [{"item": "x" * (EXECUTOR.MAX_OUTPUT_CHARS + 1)}],
                }],
                "records": [],
                "validated_paths": [],
            },
            "recipe_execution": {
                "status": "unverified_recipe_evidence_only",
                "verified": False,
                "reason": "Missing real replay verification.",
            },
            "next_action": "apply_complete_rule_to_bounded_evidence",
            "next_step": {"action": "apply_rule_to_candidate_evidence_once"},
        }
        stdout = io.StringIO()
        with mock.patch.object(EXECUTOR, "run", return_value=payload), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, EXECUTOR.main([]))
        compact = json.loads(stdout.getvalue())
        self.assertEqual("ready_for_agent_judgment", compact["status"])
        self.assertIn("summarized", compact["message"])
        self.assertEqual("unverified_recipe_evidence_only", compact["recipe_execution"]["status"])
        self.assertEqual(1, compact["candidate_evidence"]["source_count"])

    def test_missing_or_stale_certificate_never_opens_deterministic_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "compiled-recipes.json"
            catalog_path.write_text(json.dumps(recipe_catalog()), encoding="utf-8")
            recipes = RUNTIME.load_recipes(catalog_path)

            missing = RUNTIME.inspect_recipe_verification(root / "missing.json", catalog_path, recipes)
            self.assertEqual("missing_recipe_verification", missing["status"])
            self.assertFalse(RUNTIME.recipe_execution_gate(recipes[0], missing)["verified"])

            stale_path = root / "recipe-verification.json"
            stale_path.write_text(json.dumps({
                "schema_version": 1,
                "recipe_catalog_fingerprint": "not-the-current-catalog",
                "status": "verified",
                "verifiable": True,
                "publishable": True,
                "verified_recipe_ids": [recipes[0]["id"]],
            }), encoding="utf-8")
            stale = RUNTIME.inspect_recipe_verification(stale_path, catalog_path, recipes)
            self.assertEqual("recipe_verification_catalog_mismatch", stale["status"])
            self.assertFalse(RUNTIME.recipe_execution_gate(recipes[0], stale)["verified"])

    def test_unverified_recipe_returns_bounded_evidence_then_verified_recipe_can_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.csv").write_text(
                "rule_id,title,rule_text\nR-1,duplicate alpha beta,duplicate alpha beta is not allowed\n",
                encoding="utf-8",
            )
            (root / "charges.csv").write_text(
                "visit_id,item,amount\nV-1,alpha service,10\nV-1,beta service,20\nV-2,alpha service,30\n",
                encoding="utf-8",
            )
            catalog_path = root / "compiled-recipes.json"
            catalog_path.write_text(json.dumps(recipe_catalog()), encoding="utf-8")
            recipes = RUNTIME.load_recipes(catalog_path)
            contract = {
                "sources": [
                    generated_tabular_source("rules", "rules.csv", "rules_view", ["rule_id", "title", "rule_text"]),
                    generated_tabular_source("charges", "charges.csv", "charges_view", ["visit_id", "item", "amount"]),
                ],
                "runtime_source_ids": ["rules", "charges"],
                "rule_source_ids": ["rules"],
                "links": [],
            }
            missing = RUNTIME.inspect_recipe_verification(root / "missing.json", catalog_path, recipes)
            evidence_only = EXECUTOR.execute(
                "review duplicate alpha beta", root, contract, flow_contract(), {}, 10, True,
                compiled_recipes=recipes, recipe_verification=missing,
            )
            self.assertEqual("ready_for_agent_judgment", evidence_only["status"])
            self.assertNotIn("deterministic_result", evidence_only)
            self.assertEqual("unverified_recipe_evidence_only", evidence_only["recipe_execution"]["status"])
            self.assertGreater(len(evidence_only["candidate_evidence"]["records"]), 0)
            self.assertIn(
                "runtime.recipe_replay_gate",
                [step["stage_id"] for step in evidence_only["execution_steps"]],
            )
            handoff = EXECUTOR.agent_handoff_payload(evidence_only)
            self.assertEqual("unverified_recipe_evidence_only", handoff["recipe_execution"]["status"])

            verification_path = root / "recipe-verification.json"
            verification_path.write_text(json.dumps({
                "schema_version": 1,
                "recipe_catalog_fingerprint": RUNTIME.recipe_catalog_fingerprint(catalog_path),
                "status": "verified",
                "verifiable": True,
                "publishable": True,
                "verified_recipe_ids": ["recipe-duplicate-alpha-beta"],
            }), encoding="utf-8")
            verified = RUNTIME.inspect_recipe_verification(verification_path, catalog_path, recipes)
            completed = EXECUTOR.execute(
                "review duplicate alpha beta", root, contract, flow_contract(), {}, 10, True,
                compiled_recipes=recipes, recipe_verification=verified,
            )
            self.assertEqual("completed_deterministically", completed["status"])
            self.assertEqual("recipe-duplicate-alpha-beta", completed["deterministic_result"]["recipe_id"])
            self.assertEqual(1, completed["deterministic_result"]["summary"]["matched_group_count"])
            self.assertTrue(completed["recipe_execution"]["verified"])


if __name__ == "__main__":
    unittest.main()
