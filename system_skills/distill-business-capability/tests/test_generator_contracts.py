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

SCENARIO_ENGINE_PATH = (
    SKILL_ROOT.parent / "discover-data-relations" / "scripts" / "scenario_engine.py"
)
sys.path.insert(0, str(SCENARIO_ENGINE_PATH.parent))
SCENARIO_SPEC = importlib.util.spec_from_file_location("scenario_engine_external_requirement_under_test", SCENARIO_ENGINE_PATH)
assert SCENARIO_SPEC is not None and SCENARIO_SPEC.loader is not None
SCENARIO_ENGINE = importlib.util.module_from_spec(SCENARIO_SPEC)
sys.modules[SCENARIO_SPEC.name] = SCENARIO_ENGINE
SCENARIO_SPEC.loader.exec_module(SCENARIO_ENGINE)


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
    def test_chinese_conditional_external_requirement_crosses_the_synthesis_boundary(self) -> None:
        description = (
            "## Scenario Description\n"
            "如果规则有药品的违规描述，需要调用外部知识库或者网络爬虫MCP服务获取相关药品的规格辅助判断审计！\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            goal = Path(directory) / "business-context.md"
            goal.write_text(description, encoding="utf-8")
            cards = SCENARIO_ENGINE.goal_card(goal)

        requirements = [item for item in cards if item.get("kind") == "external_capability_requirement"]
        self.assertEqual(1, len(requirements))
        requirement = requirements[0]
        facts = requirement["facts"]
        self.assertTrue(facts["required"])
        self.assertEqual(
            "selected_rule_matches_condition_text",
            facts["condition_evaluation"],
        )
        self.assertIn("药品", facts["condition_text"])
        self.assertIn("external_knowledge_retrieval", facts["capability_kinds"])
        self.assertIn("web_retrieval", facts["capability_kinds"])
        self.assertIn("mcp_capability", facts["capability_kinds"])

        trace = SCENARIO_ENGINE.make_card(
            "record_trace", "derived", "trace", [{"file": "rules.csv", "locator": "row:1"}],
            {"source_files": ["rules.csv"], "key_paths": []},
        )
        brief = SCENARIO_ENGINE.synthesis_brief({
            "cards": [*cards, trace],
            "coverage": {"files": ["rules.csv"]},
            "trace_samples": {"compact": {}},
        })
        self.assertEqual("ready_for_synthesis", brief["status"])
        self.assertIn(requirement["id"], [item["id"] for item in brief["cards"]])

    def test_delivery_contract_has_structured_and_unstructured_modes(self) -> None:
        contract = sample_delivery_contract()
        modes = contract["runtime_input_contract"]["modes"]
        self.assertEqual({item["kind"] for item in modes}, {"tabular", "document"})
        document = next(item for item in modes if item["kind"] == "document")
        self.assertIn("chunk_id", document["evidence"])
        self.assertEqual(contract["entrypoint"]["command"], "execute")
        self.assertIn("custom_business_field", contract["delivery_contract"]["required_fields"])
        artifact_output = contract["artifact_output_contract"]
        self.assertEqual("BUSINESS_ARTIFACT_ROOT", artifact_output["root_environment"])
        self.assertIn("relative_path", artifact_output["public_reference_fields"])

    def test_output_template_retains_data_free_single_sheet_structure(self) -> None:
        template = {
            "template_id": "historical-xlsx",
            "name": "historical-output",
            "format": "xlsx",
            "source_kind": "tabular",
            "evidence_ids": ["E-output"],
            "output_columns": ["case_id", "amount"],
            "column_semantics": [{"column": "case_id", "semantic_role": "identifier"}],
            "tables": [{
                "table_id": "table-1",
                "table_name": "Historical Outcome",
                "worksheet_name": "Historical Outcome",
                "header_row_index": 0,
                "columns": [
                    {"ordinal": 0, "name": "case_id", "semantic_kind": "id", "source_type": None},
                    {"ordinal": 1, "name": "amount", "semantic_kind": "number", "source_type": "decimal"},
                ],
                "column_order": ["case_id", "amount"],
            }],
            "materialization": {
                "status": "materializable_schema",
                "selected_table_id": "table-1",
                "column_order": ["case_id", "amount"],
            },
        }

        descriptor = MODULE.declared_output_template_descriptor(template)

        self.assertFalse(descriptor["runtime_required"])
        self.assertEqual("structure_metadata_only_no_historical_rows", descriptor["historical_data_policy"])
        self.assertEqual("Historical Outcome", descriptor["tables"][0]["worksheet_name"])
        self.assertEqual(["case_id", "amount"], descriptor["tables"][0]["column_order"])
        self.assertEqual("decimal", descriptor["tables"][0]["columns"][1]["source_type"])
        self.assertEqual("materializable_schema", descriptor["materialization"]["status"])
        self.assertRegex(descriptor["structure_fingerprint"], r"^[0-9a-f]{64}$")

    def test_output_template_marks_multitable_and_document_layouts_as_nonmaterializable(self) -> None:
        multi = MODULE._output_template_materialization("xlsx", "tabular", [
            {"table_id": "table-1", "columns": [{"name": "id"}], "header_row_index": 0},
            {"table_id": "table-2", "columns": [{"name": "id"}], "header_row_index": 0},
        ])
        document = MODULE._output_template_materialization("docx", "document", [])

        self.assertEqual("not_materializable", multi["status"])
        self.assertEqual("multiple_or_missing_tables", multi["reason"])
        self.assertEqual("not_materializable", document["status"])
        self.assertEqual("unsupported_format_or_source", document["reason"])

    def test_inferred_historical_template_keeps_structure_and_omits_historical_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relation_path = root / "relations.json"
            MODULE.atomic_json(relation_path, {
                "nodes": [{"type": "output", "evidence_ids": ["E-output"]}],
            })
            MODULE.atomic_json(root / "evidence-cards.json", {
                "cards": [{
                    "id": "E-output",
                    "sources": [{"file": "historical-output.xlsx", "locator": "table:Sheet1;header"}],
                }],
            })
            evidence_root = root / "_field-evidence"
            evidence_root.mkdir()
            MODULE.atomic_json(evidence_root / "catalog.json", {
                "files": [{
                    "path": "historical-output.xlsx",
                    "extension": ".xlsx",
                    "kind": "tabular",
                    "tables": [{
                        "table_name": "Historical Result",
                        "header_row": 0,
                        "row_count": 999,
                        "columns": [
                            {
                                "name": "case_id", "query_name": "case_id", "index": 0,
                                "kind": "id", "data_type": "string", "sample_value": "private-case-1",
                            },
                            {
                                "name": "amount", "query_name": "amount", "index": 1,
                                "kind": "number", "data_type": "decimal", "sample_value": 9999,
                            },
                        ],
                    }],
                }],
            })

            templates = MODULE.infer_design_time_output_templates(
                relation_path,
                {"nodes": [{"type": "output", "evidence_ids": ["E-output"]}]},
                {"sources": []},
            )

            self.assertEqual(1, len(templates))
            template = templates[0]
            self.assertEqual("xlsx", template["format"])
            self.assertEqual("materializable_schema", template["materialization"]["status"])
            table = template["tables"][0]
            self.assertEqual("Historical Result", table["worksheet_name"])
            self.assertEqual(["case_id", "amount"], table["column_order"])
            self.assertEqual("string", table["columns"][0]["source_type"])
            serialized = json.dumps(template, ensure_ascii=False)
            self.assertNotIn("private-case-1", serialized)
            self.assertNotIn("sample_value", serialized)
            self.assertNotIn("row_count", serialized)

    def test_delivery_contract_exposes_template_selection_policy(self) -> None:
        operational = sample_operational()
        operational["output_contract"] = {
            "templates": [{"template_id": "one"}],
            "selection_policy": {"automatic": "unique compatible template"},
        }
        contract = MODULE.portable_delivery_contract(
            {"scenario": {"name": "generic review"}},
            {"scenario": {"name": "generic review"}, "output_contract": operational["output_contract"]},
            operational,
        )

        self.assertEqual(2, contract["schema_version"])
        self.assertEqual(
            "unique compatible template",
            contract["delivery_contract"]["template_selection_policy"]["automatic"],
        )

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
            self.assertIn("artifact_name", delivery_properties)
            self.assertIn("delivery_artifact_name", delivery_properties)
            self.assertNotIn("out_dir", delivery_properties)
            self.assertNotIn("delivery_output", delivery_properties)
            self.assertEqual(["auto", "json", "csv", "xlsx"], delivery_properties["delivery_format"]["enum"])
            (output_root / "release" / "skill" / "main_skill" / "SKILL.md").write_text(
                guide + "\nsource-tampered\n", encoding="utf-8"
            )
            errors = MODULE.validate_release_bundle(output_root, executor_name)
            self.assertTrue(any("content differs from source" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
