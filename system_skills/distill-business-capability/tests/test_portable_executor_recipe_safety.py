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
    def test_request_semantics_excludes_delivery_from_rule_lookup_and_constraint_fallback(self) -> None:
        request = (
            "执行审计：天麻素注射液限定支付条件：支付不超过14天，"
            "告诉我有多少条违规数据，按照结果结构输出"
        )

        semantics = EXECUTOR.request_semantics(request)

        self.assertIn("天麻素注射液", semantics["rule_locator"])
        self.assertNotIn("多少条", semantics["rule_locator"])
        self.assertNotIn("违规数据", semantics["rule_locator"])
        self.assertEqual(["告诉我有多少条违规数据", "按照结果结构输出"], semantics["delivery_instructions"])
        terms = EXECUTOR.unique_terms(semantics["rule_locator"])
        self.assertFalse(any("违规数据" in term or "多少条" in term for term in terms))

        # A numeric count directive that looks like a rule constraint must not
        # leak into the locator fallback when no normative rule row is present.
        count_request = "执行审计：支付不超过14天，输出不超过99条结果"
        count_semantics = EXECUTOR.request_semantics(count_request)
        constraints = EXECUTOR.derive_rule_constraints(None, {}, count_semantics["rule_locator"])
        self.assertEqual([14], [item["threshold"] for item in constraints["constraints"]])
        self.assertEqual("rule_locator_fallback", constraints["constraint_source"])

    def test_required_external_enrichment_blocks_after_rule_selection_then_projects_evidence(self) -> None:
        external_requirement = {
            "requirement_id": "external-knowledge-1",
            "required": True,
            "condition_text": "如果规则有药品的违规描述，需要调用外部知识库或网络爬虫MCP辅助判断",
            "condition_evaluation": "selected_rule_matches_condition_text",
            "capability_kinds": ["knowledge_retrieval", "web_retrieval", "mcp"],
            "accepted_integrations": ["declared_host_capability", "mcp"],
            "evidence_contract": ["provider", "provider_capability", "query", "sources", "retrieved_at"],
            "failure_policy": "block_business_decision",
        }
        contract = {
            "sources": [generated_tabular_source("rules", "rules.csv", "rules_view", ["rule_id", "rule_text"])],
            "runtime_source_ids": ["rules"],
            "rule_source_ids": ["rules"],
            "links": [],
            "external_requirements": [external_requirement],
        }
        request = (
            "执行审计：天麻素注射液限定支付条件：支付不超过14天，"
            "告诉我有多少条违规数据，按照结果结构输出"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.csv").write_text(
                "rule_id,rule_text\nR-1,天麻素注射液限定支付条件：支付不超过14天\n",
                encoding="utf-8",
            )

            blocked = EXECUTOR.execute(request, root, contract, flow_contract(), {}, 10, True)

        self.assertEqual("blocked_required_external_enrichment", blocked["status"])
        self.assertEqual("unique", blocked["rule_selection"])
        self.assertEqual("天麻素注射液限定支付条件：支付不超过14天", blocked["selected_rule"]["row"]["rule_text"])
        self.assertNotIn("多少条", blocked["request_semantics"]["rule_locator"])
        self.assertEqual("host_capability_or_mcp", blocked["next_step"]["actor"])
        self.assertEqual("required_external_evidence_not_supplied", blocked["external_enrichment"]["blockers"][0]["reason"])
        self.assertIn(
            "runtime.required_external_enrichment",
            [step["stage_id"] for step in blocked["execution_steps"]],
        )

        external_evidence = {
            "requirement_id": "external-knowledge-1",
            "status": "success",
            "provider": "declared-knowledge-mcp",
            "provider_capability": "knowledge_retrieval",
            "query": "天麻素注射液 规格",
            "sources": [{"title": "authoritative specification", "locator": "item-1"}],
            "retrieved_at": "2026-08-12T00:00:00Z",
            "facts": {"specification": "example"},
        }
        selected = {
            "source_id": "rules",
            "row": {"rule_text": "天麻素注射液限定支付条件：支付不超过14天"},
        }
        completed = EXECUTOR.external_enrichment_gate(contract, {}, selected, external_evidence)
        self.assertEqual("completed", completed["status"])
        payload = {"status": "ready_for_agent_judgment", "external_enrichment": completed}
        self.assertEqual("example", EXECUTOR.compact_stdout_payload(payload)["external_enrichment"]["records"][0]["evidence"]["facts"]["specification"])
        self.assertEqual("declared-knowledge-mcp", EXECUTOR.agent_handoff_payload(payload)["external_enrichment"]["records"][0]["evidence"]["provider"])

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
