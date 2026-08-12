"""Regression checks for promotion of signed, hash-only recipe replays."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "distill_capabilities.py"
SPEC = importlib.util.spec_from_file_location("distill_recipe_replay_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def recipe_catalog() -> dict:
    return {
        "schema_version": 1,
        "recipes": [{
            "id": "recipe-a",
            "kind": "grouped_cooccurrence_from_rule_text",
            "source_id": "charges",
            "context_source_ids": [],
            "rule_selector": {"source_id": "rules", "mode": "any_complete_rule"},
        }],
    }


class RecipeReplayGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.relation_root = self.root / "relations"
        self.relation_root.mkdir()
        self.catalog_path = self.output / "compiled-recipes.json"
        self.catalog = recipe_catalog()
        MODULE.atomic_json(self.catalog_path, self.catalog)
        self.contract_path = self.output / "recipe-replay-contract.json"
        MODULE.atomic_json(self.contract_path, {
            "schema_version": 1,
            "kind": "compiled_recipe_replay_report",
            "status": "pending_real_replay",
            "recipe_catalog_fingerprint": MODULE.sha256_file(self.catalog_path),
            "required_cases": [{
                "case_id": "recipe-a:case-a",
                "recipe_id": "recipe-a",
                "assertion_id": "case-a",
                "trace_bundle_id": "bundle-a",
            }],
        })
        self.executor_root = self.root / "executor"
        scripts = self.executor_root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "execute_scenario.py").write_text("# executor\n", encoding="utf-8")
        (scripts / "recipe_runtime.py").write_text("# runtime\n", encoding="utf-8")
        (scripts / "query_tabular.py").write_text("# table reader\n", encoding="utf-8")
        (self.executor_root / "requirements.txt").write_text("duckdb>=1.4.0,<2.0.0\n", encoding="utf-8")
        references = self.executor_root / "references"
        references.mkdir()
        MODULE.atomic_json(references / "compiled-recipes.json", self.catalog)
        MODULE.atomic_json(references / "operational-data-contract.json", {"status": "ready"})
        MODULE.atomic_json(references / "flow-contract.json", {"execution_mode": "test", "main_flow": []})
        self.runtime_artifacts = {
            "catalog": references / "compiled-recipes.json",
            "executor": scripts / "execute_scenario.py",
            "runtime_contract": references / "operational-data-contract.json",
            "flow_contract": references / "flow-contract.json",
        }
        self.digests, errors = MODULE.recipe_replay_runtime_digests(self.runtime_artifacts)
        self.assertEqual([], errors)
        self.fixture_digest = "a" * 64
        self.trace_digest = "b" * 64
        self.review_digest = "c" * 64
        self.context = {
            "relation_root": self.relation_root,
            "trace_fingerprint": self.trace_digest,
            "trace_review_fingerprint": self.review_digest,
            "trace_bundle_ids": {"bundle-a"},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _report(self, actual_digest: str = "d" * 64) -> dict:
        return {
            "schema_version": 1,
            "kind": "compiled_recipe_replay_report",
            "status": "passed",
            "recipe_catalog_fingerprint": self.digests["recipe_catalog_fingerprint"],
            "replay_contract_fingerprint": MODULE.sha256_file(self.contract_path),
            "trace_review": {
                "fingerprint": self.review_digest,
                "status": "approved",
                "trace_bundle_ids": ["bundle-a"],
            },
            "executor": {
                "sha256": self.digests["executor_fingerprint"],
                "closure_sha256": self.digests["executor_closure_fingerprint"],
                "interface": "execute_scenario.execute",
            },
            "runtime_contract": {"sha256": self.digests["runtime_contract_fingerprint"]},
            "flow_contract": {"sha256": self.digests["flow_contract_fingerprint"]},
            "fixtures": {"sha256": self.fixture_digest, "case_count": 1},
            "replays": [{
                "case_id": "recipe-a:case-a",
                "recipe_id": "recipe-a",
                "assertion_id": "case-a",
                "trace_bundle_id": "bundle-a",
                "case_fingerprint": "e" * 64,
                "request_digest": "f" * 64,
                "expected": {
                    "normalized_result_digest": "d" * 64,
                    "result_anchor_count": 1,
                    "approved_empty_result": False,
                },
                "actual": {
                    "normalized_result_digest": actual_digest,
                    "result_anchor_count": 1,
                    "matched_group_count": 1,
                    "complete": True,
                },
                "comparison": {
                    "status": "passed",
                    "digest_match": True,
                    "count_match": True,
                    "nonempty_policy_match": True,
                    "complete_match": True,
                },
                "execution": {
                    "status": "completed_deterministically",
                    "recipe_execution_status": "verified_recipe",
                },
                "source_fingerprints": [
                    {"source_id": "charges", "sha256": "1" * 64, "size_bytes": 10},
                    {"source_id": "rules", "sha256": "2" * 64, "size_bytes": 20},
                ],
                "failure_code": "",
            }],
            "verified_recipe_ids": ["recipe-a"],
            "failures": [],
        }

    def _signed_envelope(self, kind: str, artifact_fingerprint: str, **extra: object) -> dict:
        envelope: dict[str, object] = {
            "schema_version": 1,
            "issuer": MODULE.PLATFORM_APPROVAL_ISSUER,
            "artifact_kind": kind,
            "decision": "approved",
            "artifact_fingerprint": artifact_fingerprint,
            "trace_fingerprint": self.trace_digest,
            "trace_review_fingerprint": self.review_digest,
            "recipe_catalog_fingerprint": self.digests["recipe_catalog_fingerprint"],
            "replay_contract_fingerprint": MODULE.sha256_file(self.contract_path),
            "executor_fingerprint": self.digests["executor_fingerprint"],
            "executor_closure_fingerprint": self.digests["executor_closure_fingerprint"],
            "runtime_contract_fingerprint": self.digests["runtime_contract_fingerprint"],
            "flow_contract_fingerprint": self.digests["flow_contract_fingerprint"],
            "approval_id": f"approval-{kind}",
            "subject": "unit-test",
            "issued_at": "2026-08-12T00:00:00Z",
            **extra,
        }
        envelope["signature"] = MODULE.platform_approval_signature(envelope, "test-secret")
        return envelope

    def _write_signed_ledger(self, report_path: Path) -> None:
        ledger = {
            "schema_version": 1,
            "kind": "platform_approval_envelopes",
            "issuer": MODULE.PLATFORM_APPROVAL_ISSUER,
            "approvals": [
                self._signed_envelope("recipe_replay_cases", self.fixture_digest),
                self._signed_envelope(
                    "recipe_replay_report",
                    MODULE.sha256_file(report_path),
                    fixture_fingerprint=self.fixture_digest,
                    verified_recipe_ids=["recipe-a"],
                ),
            ],
        }
        MODULE.atomic_json(self.relation_root / "platform-approvals.json", ledger)

    def _status(self) -> dict:
        return MODULE.compiled_recipe_replay_status(
            self.output, self.catalog, {"source": {}}, self.runtime_artifacts,
        )

    def test_signed_report_with_exact_hashes_promotes_only_its_recipe(self) -> None:
        report_path = self.output / "recipe-replay-report.json"
        MODULE.atomic_json(report_path, self._report())
        self._write_signed_ledger(report_path)
        with patch.object(MODULE, "approved_recipe_replay_context", return_value=(self.context, "")), patch.dict(
            os.environ, {MODULE.PLATFORM_APPROVAL_KEY_ENV: "test-secret"}, clear=False,
        ):
            status = self._status()
        self.assertTrue(status["passed"])
        self.assertEqual(["recipe-a"], status["verified_recipe_ids"])

    def test_re_signed_report_cannot_claim_digest_match_when_values_differ(self) -> None:
        report_path = self.output / "recipe-replay-report.json"
        MODULE.atomic_json(report_path, self._report(actual_digest="3" * 64))
        self._write_signed_ledger(report_path)
        with patch.object(MODULE, "approved_recipe_replay_context", return_value=(self.context, "")), patch.dict(
            os.environ, {MODULE.PLATFORM_APPROVAL_KEY_ENV: "test-secret"}, clear=False,
        ):
            status = self._status()
        self.assertFalse(status["passed"])
        self.assertEqual([], status["verified_recipe_ids"])
        self.assertIn("result comparison did not pass", status["reason"])

    def test_changed_runtime_dependency_invalidates_an_otherwise_signed_report(self) -> None:
        report_path = self.output / "recipe-replay-report.json"
        MODULE.atomic_json(report_path, self._report())
        self._write_signed_ledger(report_path)
        (self.executor_root / "scripts" / "recipe_runtime.py").write_text("# changed runtime\n", encoding="utf-8")
        with patch.object(MODULE, "approved_recipe_replay_context", return_value=(self.context, "")), patch.dict(
            os.environ, {MODULE.PLATFORM_APPROVAL_KEY_ENV: "test-secret"}, clear=False,
        ):
            status = self._status()
        self.assertFalse(status["passed"])
        self.assertIn("executor does not match", status["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
