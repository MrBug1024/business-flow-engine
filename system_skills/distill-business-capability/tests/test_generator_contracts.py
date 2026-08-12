"""Focused regression checks for portable capability generation contracts."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "distill_capabilities.py"
SPEC = importlib.util.spec_from_file_location("distill_capabilities_under_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def sample_operational() -> dict[str, object]:
    return {
        "status": "ready",
        "runtime_source_ids": ["rules", "charges", "policy-doc"],
        "rule_source_ids": ["rules"],
        "sources": [
            {
                "source_id": "rules",
                "kind": "tabular",
                "runtime_required": True,
                "path": "rules.csv",
                "extension": ".csv",
                "tables": [{"columns": [{"name": "rule_text"}]}],
            },
            {
                "source_id": "charges",
                "kind": "tabular",
                "runtime_required": True,
                "path": "charges.xlsx",
                "extension": ".xlsx",
                "tables": [{"columns": [{"name": "visit_id"}, {"name": "item"}]}],
            },
            {
                "source_id": "policy-doc",
                "kind": "document",
                "runtime_required": True,
                "path": "policy.pdf",
                "extension": ".pdf",
                "tables": [],
            },
        ],
    }


def sample_delivery_contract() -> dict[str, object]:
    return MODULE.portable_delivery_contract(
        {"scenario": {"name": "generic review"}},
        {
            "scenario": {"name": "generic review"},
            "output_contract": {"required_result_fields": ["custom_business_field"]},
        },
        sample_operational(),
    )


def sample_recipe_catalog() -> dict[str, object]:
    return {
        "schema_version": 1,
        "recipes": [
            {
                "id": "recipe-a",
                "kind": "grouped_cooccurrence_from_rule_text",
                "rule_selector": {
                    "source_id": "rules",
                    "mode": "any_complete_rule",
                    "rule_text_fields": ["rule_text"],
                },
                "source_id": "charges",
                "item_field": "item",
                "group_by": ["visit_id"],
                "rule_text_fields": ["rule_text"],
                "minimum_terms": 2,
                "result_annotations_from_rule": {"rule": "rule_text"},
                "covered_output_node_ids": ["decision"],
                "historical_replay_assertions": [
                    {
                        "id": "assertion-a",
                        "status": "approved",
                        "trace_bundle_id": "trace-a",
                        "evidence_ref": "trace-review.json::approval",
                        "output_node_ids": ["decision"],
                    }
                ],
            }
        ],
    }


class GeneratorContractTests(unittest.TestCase):
    def test_delivery_contract_has_structured_and_unstructured_modes(self) -> None:
        contract = sample_delivery_contract()
        modes = contract["runtime_input_contract"]["modes"]
        self.assertEqual({item["kind"] for item in modes}, {"tabular", "document"})
        document = next(item for item in modes if item["kind"] == "document")
        self.assertIn("chunk_id", document["evidence"])
        self.assertEqual(contract["entrypoint"]["command"], "execute")
        self.assertIn("custom_business_field", contract["delivery_contract"]["required_fields"])

    def test_replay_contract_refreshes_to_current_recipe_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            MODULE.atomic_json(output_root / "compiled-recipes.json", sample_recipe_catalog())
            contract = MODULE.refresh_recipe_replay_contract(
                output_root, {"source": {}}, sample_operational()
            )
            persisted = MODULE.load_json(output_root / "recipe-replay-contract.json", MODULE.MAX_CANDIDATE_BYTES)
            self.assertEqual(contract, persisted)
            self.assertEqual(contract["recipe_ids"], ["recipe-a"])
            self.assertEqual(contract["required_cases"][0]["recipe_id"], "recipe-a")
            self.assertEqual(contract["required_cases"][0]["assertion_id"], "assertion-a")
            self.assertEqual(contract["required_cases"][0]["runtime_source_ids"], ["charges", "rules"])
            self.assertEqual(contract["recipe_catalog_fingerprint"], MODULE.sha256_file(output_root / "compiled-recipes.json"))

    def test_report_without_real_runner_never_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            MODULE.atomic_json(output_root / "compiled-recipes.json", sample_recipe_catalog())
            catalog = MODULE.compiled_recipe_catalog(output_root, sample_operational())
            missing = MODULE.compiled_recipe_replay_status(output_root, catalog)
            self.assertFalse(missing["passed"])
            self.assertEqual(missing["status"], "missing_real_replay_report")
            MODULE.atomic_json(output_root / "recipe-replay-report.json", {
                "schema_version": 1,
                "kind": "compiled_recipe_replay_report",
                "status": "passed",
                "recipe_catalog_fingerprint": MODULE.sha256_file(output_root / "compiled-recipes.json"),
                "replays": [{}],
            })
            pending = MODULE.compiled_recipe_replay_status(output_root, catalog)
            self.assertFalse(pending["passed"])
            self.assertEqual(pending["status"], "reported_pending_runner_validation")

    def test_runtime_recipe_verification_starts_with_no_deterministic_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            catalog = sample_recipe_catalog()
            MODULE.atomic_json(output_root / "compiled-recipes.json", catalog)
            runtime_policy = MODULE.portable_recipe_verification_contract(
                output_root / "compiled-recipes.json",
                catalog,
                {
                    "status": "unverified",
                    "verifiable": False,
                    "publishable": False,
                    "replay": {"status": "missing_real_replay_report"},
                    "reason": "No real replay was supplied.",
                },
            )
            self.assertEqual(runtime_policy["status"], "unverified")
            self.assertEqual(runtime_policy["verified_recipe_ids"], [])
            self.assertEqual(
                runtime_policy["recipe_catalog_fingerprint"],
                MODULE.sha256_file(output_root / "compiled-recipes.json"),
            )

    def test_human_publication_does_not_require_deterministic_recipe_verification(self) -> None:
        self.assertTrue(MODULE.human_package_publication_is_approved({
            "status": "approved",
            "verifiable": True,
            "publishable": True,
        }))
        self.assertFalse(MODULE.human_package_publication_is_approved({
            "status": "approved",
            "verifiable": False,
            "publishable": True,
        }))

    def test_primary_executor_embeds_document_runtime_and_hybrid_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor_root = Path(directory) / "executor"
            shutil.copytree(SKILL_ROOT / "assets" / "portable-scenario-executor", executor_root)
            installed = MODULE.install_primary_executor_runtimes(
                executor_root,
                {"foundation_skills": [{"kind": "tabular"}, {"kind": "document"}]},
                sample_operational(),
            )
            self.assertEqual(installed, {"tabular_adapter", "document_adapter"})
            self.assertTrue((executor_root / "scripts" / "query_tabular.py").is_file())
            self.assertTrue((executor_root / "scripts" / "extract_documents.py").is_file())
            dependencies = set(MODULE.python_dependencies(executor_root))
            self.assertIn("openpyxl>=3.1.0,<4.0.0", dependencies)
            self.assertIn("duckdb>=1.4.0,<2.0.0", dependencies)
            self.assertIn("pypdf>=5.0.0,<7.0.0", dependencies)
            self.assertIn("python-docx>=1.1.0,<2.0.0", dependencies)

    def test_release_root_uses_scenario_main_and_archive_matches_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "output"
            skills_root = root / "skills"
            executor_name = "generic-main-executor"
            executor_root = skills_root / executor_name
            shutil.copytree(SKILL_ROOT / "assets" / "portable-scenario-executor", executor_root)
            operational = sample_operational()
            MODULE.install_primary_executor_runtimes(
                executor_root,
                {"foundation_skills": [{"kind": "tabular"}, {"kind": "document"}]},
                operational,
            )
            MODULE.atomic_json(executor_root / "references" / "operational-data-contract.json", operational)
            MODULE.atomic_json(executor_root / "references" / "flow-contract.json", {"execution_plan": {}})
            MODULE.atomic_json(executor_root / "references" / "delivery-contract.json", sample_delivery_contract())
            catalog = {"schema_version": 1, "recipes": []}
            MODULE.atomic_json(executor_root / "references" / "compiled-recipes.json", catalog)
            MODULE.atomic_json(output_root / "compiled-recipes.json", catalog)
            MODULE.atomic_json(
                executor_root / "references" / "recipe-verification.json",
                MODULE.portable_recipe_verification_contract(
                    executor_root / "references" / "compiled-recipes.json",
                    catalog,
                    {"status": "verified", "verifiable": True, "publishable": True, "replay": {}},
                ),
            )
            executor_manifest = [{
                "name": executor_name,
                "python_dependencies": MODULE.python_dependencies(executor_root),
            }]
            MODULE.build_release_bundle(
                {
                    "scenario": {"name": "generic review", "purpose": "review a complete request"},
                    "bundle": {"description": "test package"},
                },
                output_root,
                skills_root,
                executor_name,
                executor_manifest,
            )
            self.assertEqual(MODULE.validate_release_bundle(output_root, executor_name), [])
            guide = (output_root / "release" / "skill" / "main_skill" / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(guide.startswith("---\nname: scenario-main\n"))
            self.assertIn("scripts/skill_executor.py", guide)
            self.assertIn("references/delivery-contract.json", guide)
            self.assertTrue((output_root / "release" / "skill" / "main_skill" / "scripts" / "extract_documents.py").is_file())
            release_dependencies = set(MODULE.python_dependencies(output_root / "release" / "skill" / "main_skill"))
            self.assertIn("openpyxl>=3.1.0,<4.0.0", release_dependencies)
            self.assertIn("pypdf>=5.0.0,<7.0.0", release_dependencies)
            mcp_dependencies = set(MODULE.python_dependencies(output_root / "release" / "mcp"))
            self.assertIn("openpyxl>=3.1.0,<4.0.0", mcp_dependencies)
            dispatch = json.loads((output_root / "release" / "skill" / "main_skill" / "dispatch_config.json").read_text(encoding="utf-8"))
            self.assertEqual("execute", dispatch["primary_action"])
            self.assertEqual(["execute"], dispatch["normal_action_allowlist"])
            self.assertTrue(dispatch["structured_passthrough_required"])
            output_specs = json.loads((output_root / "release" / "skill" / "main_skill" / "output_specs.json").read_text(encoding="utf-8"))
            self.assertEqual("execute_business_request", output_specs["outputs"][0]["output_id"])
            self.assertEqual("portable_business_request_transaction_result", output_specs["outputs"][0]["host_action"]["response_contract"])
            mcp = json.loads((output_root / "release" / "mcp" / "mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(["execute"], mcp["normal_action_allowlist"])
            self.assertEqual(["execute"], [item["action"] for item in mcp["tools"]])
            delivery_properties = mcp["tools"][0]["inputSchema"]["properties"]
            self.assertIn("delivery_output", delivery_properties)
            self.assertEqual(["auto", "json", "csv", "xlsx"], delivery_properties["delivery_format"]["enum"])
            (output_root / "release" / "skill" / "main_skill" / "SKILL.md").write_text(
                guide + "\nsource-tampered\n", encoding="utf-8"
            )
            errors = MODULE.validate_release_bundle(output_root, executor_name)
            self.assertTrue(any("content differs from source" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
