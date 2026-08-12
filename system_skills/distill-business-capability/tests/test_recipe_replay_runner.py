"""Regression tests for the private, fail-closed recipe replay runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "recipe_replay_runner.py"
EXECUTOR_PATH = (
    ROOT / "assets" / "portable-scenario-executor" / "scripts" / "execute_scenario.py"
)
RUNTIME_PATH = (
    ROOT / "assets" / "portable-scenario-executor" / "scripts" / "recipe_runtime.py"
)
RUNNER_SPEC = importlib.util.spec_from_file_location("recipe_replay_runner_tests", RUNNER_PATH)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)
EXECUTOR_SPEC = importlib.util.spec_from_file_location("recipe_replay_executor_tests", EXECUTOR_PATH)
assert EXECUTOR_SPEC and EXECUTOR_SPEC.loader
EXECUTOR = importlib.util.module_from_spec(EXECUTOR_SPEC)
EXECUTOR_SPEC.loader.exec_module(EXECUTOR)
RUNTIME_SPEC = importlib.util.spec_from_file_location("recipe_replay_runtime_tests", RUNTIME_PATH)
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(RUNTIME)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def source(source_id: str, path: str, view: str, columns: list[str]) -> dict:
    return {
        "source_id": source_id,
        "kind": "tabular",
        "path": path,
        "view_name": view,
        "lifecycle": "runtime_input",
        "runtime_required": True,
        "tables": [{
            "columns": [{"name": name, "query_name": name, "kind": "other"} for name in columns],
        }],
    }


def catalog() -> dict:
    return {
        "schema_version": 1,
        "recipes": [{
            "id": "recipe-duplicate-alpha-beta",
            "kind": "grouped_cooccurrence",
            "rule_selector": {"source_id": "rules", "equals": {"rule_id": "R-1"}},
            "source_id": "charges",
            "group_by": ["visit_id"],
            "all_of": [{"field": "item", "operator": "contains", "value": "alpha"}],
            "any_of": [{"field": "item", "operator": "contains", "value": "beta"}],
            "summary_measure": "amount",
            "result_fields": ["visit_id", "item", "amount"],
            "result_annotations": {},
        }],
    }


class RecipeReplayRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.replay_worker_environment = patch.dict(
            os.environ, {RUNNER.REPLAY_ISOLATION_ENV: "1"}, clear=False,
        )
        self.replay_worker_environment.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "historical-data"
        self.data_root.mkdir()
        # These intentionally recognizable strings make the no-body assertion
        # below meaningful.  The runner output must never contain them.
        self.private_request = "private request duplicate alpha beta token-9f4e"
        (self.data_root / "rules.csv").write_text(
            "rule_id,title,rule_text\nR-1,duplicate alpha beta,duplicate alpha beta is not allowed\n",
            encoding="utf-8",
        )
        (self.data_root / "charges.csv").write_text(
            "visit_id,item,amount\nV-1,alpha service,10\nV-1,beta service,20\nV-2,alpha service,30\n",
            encoding="utf-8",
        )
        self.catalog_path = self.root / "compiled-recipes.json"
        write_json(self.catalog_path, catalog())
        self.runtime_contract_path = self.root / "runtime-contract.json"
        write_json(self.runtime_contract_path, {
            "sources": [
                source("rules", "rules.csv", "rules_view", ["rule_id", "title", "rule_text"]),
                source("charges", "charges.csv", "charges_view", ["visit_id", "item", "amount"]),
            ],
            "runtime_source_ids": ["rules", "charges"],
            "rule_source_ids": ["rules"],
            "links": [],
        })
        self.flow_contract_path = self.root / "flow-contract.json"
        write_json(self.flow_contract_path, {
            "execution_mode": "evidence_pipeline",
            "main_flow": [],
            "stages": [],
            "controls": [],
            "output_contract": {},
        })
        self.review_path = self.root / "trace-review.json"
        write_json(self.review_path, {
            "schema_version": 1,
            "kind": "trace_review",
            "status": "approved",
            "approval": {"decision": "approved"},
            "trace": {"bundle_id": "trace-replay-unit"},
        })
        self.replay_contract_path = self.root / "recipe-replay-contract.json"
        write_json(self.replay_contract_path, {
            "schema_version": 1,
            "kind": "compiled_recipe_replay_report",
            "status": "pending_real_replay",
            "recipe_catalog_fingerprint": sha256_file(self.catalog_path),
            "approved_trace": {"bundle_ids": ["trace-replay-unit"]},
            "required_cases": [{
                "case_id": "recipe-duplicate-alpha-beta:approved-case",
                "recipe_id": "recipe-duplicate-alpha-beta",
                "assertion_id": "approved-case",
                "trace_bundle_id": "trace-replay-unit",
            }],
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self.replay_worker_environment.stop()

    def _actual_digest(self) -> str:
        recipes = RUNTIME.load_recipes(self.catalog_path)
        verification = {
            "status": "verified",
            "verified": True,
            "certificate_validated": True,
            "catalog_fingerprint": RUNTIME.recipe_catalog_fingerprint(self.catalog_path),
            "verification_path": "test-ephemeral",
            "verified_recipe_ids": ["recipe-duplicate-alpha-beta"],
        }
        payload = EXECUTOR.execute(
            self.private_request,
            self.data_root,
            json.loads(self.runtime_contract_path.read_text(encoding="utf-8")),
            json.loads(self.flow_contract_path.read_text(encoding="utf-8")),
            {},
            20,
            True,
            compiled_recipes=recipes,
            recipe_verification=verification,
        )
        self.assertEqual("completed_deterministically", payload["status"])
        return RUNNER.normalized_result_digest(payload["deterministic_result"])

    def _fixture_source_bindings(self) -> dict[str, str]:
        return {"rules": "rules.csv", "charges": "charges.csv"}

    def _fixture_source_fingerprints(self) -> dict[str, dict[str, int | str]]:
        return {
            source_id: {
                "sha256": sha256_file(self.data_root / relative),
                "size_bytes": (self.data_root / relative).stat().st_size,
            }
            for source_id, relative in self._fixture_source_bindings().items()
        }

    def _write_cases(
        self, expected_digest: str, *, request_digest: str | None = None,
        source_bindings: dict[str, str] | None = None,
        source_fingerprints: dict[str, dict[str, int | str]] | None = None,
    ) -> Path:
        path = self.root / "private-replay-cases.json"
        write_json(path, {
            "schema_version": 1,
            "kind": "compiled_recipe_replay_cases",
            "recipe_catalog_fingerprint": sha256_file(self.catalog_path),
            "replay_contract_fingerprint": sha256_file(self.replay_contract_path),
            "trace_review_fingerprint": sha256_file(self.review_path),
            "approval": {"kind": "approved_recipe_replay_oracle", "decision": "approved"},
            "cases": [{
                "case_id": "recipe-duplicate-alpha-beta:approved-case",
                "recipe_id": "recipe-duplicate-alpha-beta",
                "assertion_id": "approved-case",
                "trace_bundle_id": "trace-replay-unit",
                "request": self.private_request,
                "request_digest": request_digest or RUNNER.request_digest(self.private_request),
                "source_bindings": source_bindings if source_bindings is not None else self._fixture_source_bindings(),
                "source_fingerprints": (
                    source_fingerprints if source_fingerprints is not None else self._fixture_source_fingerprints()
                ),
                "expected": {
                    "normalized_result_digest": expected_digest,
                    # ``any_match_rows`` emits the matching beta row for the
                    # one qualified visit group, so the deterministic result
                    # contains one row (and one result anchor), not both
                    # source rows used to establish the co-occurrence.
                    "result_anchor_count": 1,
                    "approved_empty_result": False,
                },
            }],
        })
        return path

    def _run(
        self, cases_path: Path, *, bindings: list[str] | None = None,
        executor_path: Path = EXECUTOR_PATH,
    ) -> tuple[dict, Path]:
        output = self.root / "recipe-replay-report.json"
        arguments = [
            "--catalog", str(self.catalog_path),
            "--replay-contract", str(self.replay_contract_path),
            "--cases", str(cases_path),
            "--trace-review", str(self.review_path),
            "--runtime-contract", str(self.runtime_contract_path),
            "--flow-contract", str(self.flow_contract_path),
            "--data-root", str(self.data_root),
            "--executor", str(executor_path),
            "--output", str(output),
        ]
        for binding in bindings or []:
            arguments.extend(["--bind", binding])
        report = RUNNER.run(arguments)
        return report, output

    def _copy_executor_package(self) -> Path:
        """Make a mutable candidate package without touching tracked assets."""

        package_root = self.root / "candidate-executor"
        scripts_root = package_root / "scripts"
        scripts_root.mkdir(parents=True)
        (scripts_root / "execute_scenario.py").write_bytes(EXECUTOR_PATH.read_bytes())
        (scripts_root / "recipe_runtime.py").write_bytes(RUNTIME_PATH.read_bytes())
        (scripts_root / "query_tabular.py").write_bytes(
            (ROOT / "assets" / "portable-tabular-reader" / "scripts" / "query_tabular.py").read_bytes()
        )
        (scripts_root / "extract_documents.py").write_bytes(
            (ROOT / "assets" / "portable-document-reader" / "scripts" / "extract_documents.py").read_bytes()
        )
        (package_root / "requirements.txt").write_bytes(
            (ROOT / "assets" / "portable-scenario-executor" / "requirements.txt").read_bytes()
        )
        return scripts_root / "execute_scenario.py"

    def test_approved_oracle_runs_real_executor_and_emits_only_safe_metadata(self) -> None:
        cases = self._write_cases(self._actual_digest())
        report, output = self._run(cases)
        self.assertEqual("passed", report["status"])
        self.assertEqual(["recipe-duplicate-alpha-beta"], report["verified_recipe_ids"])
        self.assertEqual(
            RUNNER.executor_closure_fingerprint(EXECUTOR_PATH),
            report["executor"]["closure_sha256"],
        )
        replay = report["replays"][0]
        self.assertEqual("passed", replay["comparison"]["status"])
        self.assertEqual(1, replay["actual"]["result_anchor_count"])
        self.assertTrue(replay["actual"]["complete"])
        self.assertEqual(2, len(replay["source_fingerprints"]))
        serialized = output.read_text(encoding="utf-8")
        self.assertNotIn(self.private_request, serialized)
        self.assertNotIn("alpha service", serialized)
        self.assertNotIn(str(self.data_root), serialized)

    def test_mismatched_oracle_fail_closes_and_cannot_verify_recipe(self) -> None:
        cases = self._write_cases("0" * 64)
        report, output = self._run(cases)
        self.assertEqual("failed", report["status"])
        self.assertEqual([], report["verified_recipe_ids"])
        replay = report["replays"][0]
        self.assertEqual("failed", replay["comparison"]["status"])
        self.assertEqual("replay_oracle_mismatch", replay["failure_code"])
        self.assertEqual("replay_oracle_mismatch", report["failures"][0]["code"])
        self.assertNotIn(self.private_request, output.read_text(encoding="utf-8"))

    def test_bad_private_request_digest_blocks_without_echoing_request(self) -> None:
        cases = self._write_cases(self._actual_digest(), request_digest="f" * 64)
        report, output = self._run(cases)
        self.assertEqual("blocked", report["status"])
        self.assertEqual([], report["verified_recipe_ids"])
        self.assertEqual("request_digest_mismatch", report["failures"][0]["code"])
        self.assertNotIn(self.private_request, output.read_text(encoding="utf-8"))

    def test_replay_rejects_a_signing_environment_even_when_worker_flag_is_set(self) -> None:
        with patch.dict(os.environ, {
            RUNNER.REPLAY_ISOLATION_ENV: "1",
            "BUSINESS_FLOW_PLATFORM_APPROVAL_HMAC_KEY": "must-not-be-visible",
        }, clear=False):
            with self.assertRaises(RUNNER.ReplayError) as raised:
                RUNNER.require_isolated_replay_worker()
        self.assertEqual("replay_runner_signing_environment_forbidden", raised.exception.code)

    def test_executor_loader_discards_a_same_named_helper_from_another_skill(self) -> None:
        fake = types.ModuleType("recipe_runtime")
        fake.__file__ = str(self.root / "other-skill" / "recipe_runtime.py")
        with patch.dict(sys.modules, {"recipe_runtime": fake}):
            executor = RUNNER.load_executor(EXECUTOR_PATH)
            resolved = executor.recipe_runtime()
            self.assertEqual(RUNTIME_PATH.resolve(), Path(resolved.__file__).resolve())

    def test_catalog_toctou_uses_the_bytes_captured_before_source_mutation(self) -> None:
        """Changing the source catalog after capture cannot change execution."""

        expected_digest = self._actual_digest()
        cases = self._write_cases(expected_digest)
        original_catalog_digest = sha256_file(self.catalog_path)
        original_snapshot = RUNNER.materialize_executor_snapshot

        def mutate_catalog_then_snapshot(*args, **kwargs):
            altered = catalog()
            altered["recipes"][0]["id"] = "recipe-tampered-after-capture"
            write_json(self.catalog_path, altered)
            return original_snapshot(*args, **kwargs)

        with patch.object(RUNNER, "materialize_executor_snapshot", side_effect=mutate_catalog_then_snapshot):
            report, output = self._run(cases)

        self.assertEqual("passed", report["status"])
        self.assertEqual(original_catalog_digest, report["recipe_catalog_fingerprint"])
        self.assertNotEqual(original_catalog_digest, sha256_file(self.catalog_path))
        self.assertNotIn(self.private_request, output.read_text(encoding="utf-8"))

    def test_contract_case_and_review_toctou_use_the_captured_control_bytes(self) -> None:
        """Approved control files cannot be swapped between fingerprint and use."""

        cases = self._write_cases(self._actual_digest())
        original_digests = {
            "contract": sha256_file(self.replay_contract_path),
            "cases": sha256_file(cases),
            "review": sha256_file(self.review_path),
        }
        original_snapshot = RUNNER.materialize_executor_snapshot

        def mutate_controls_then_snapshot(*args, **kwargs):
            write_json(self.replay_contract_path, {"schema_version": 999})
            write_json(cases, {"schema_version": 999})
            write_json(self.review_path, {"schema_version": 999})
            return original_snapshot(*args, **kwargs)

        with patch.object(RUNNER, "materialize_executor_snapshot", side_effect=mutate_controls_then_snapshot):
            report, output = self._run(cases)

        self.assertEqual("passed", report["status"])
        self.assertEqual(original_digests["contract"], report["replay_contract_fingerprint"])
        self.assertEqual(original_digests["cases"], report["fixtures"]["sha256"])
        self.assertEqual(original_digests["review"], report["trace_review"]["fingerprint"])
        self.assertNotIn(self.private_request, output.read_text(encoding="utf-8"))

    def test_executor_toctou_runs_the_frozen_closure_not_the_mutated_candidate_path(self) -> None:
        """Changing candidate code after snapshotting cannot alter its replay."""

        candidate = self._copy_executor_package()
        expected_digest = self._actual_digest()
        cases = self._write_cases(expected_digest)
        candidate_digest = sha256_file(candidate)
        original_loader = RUNNER.load_executor

        def mutate_candidate_then_load(path, *args, **kwargs):
            self.assertNotEqual(candidate.resolve(), Path(path).resolve())
            candidate.write_text("raise RuntimeError('candidate path should not be imported')\n", encoding="utf-8")
            return original_loader(path, *args, **kwargs)

        with patch.object(RUNNER, "load_executor", side_effect=mutate_candidate_then_load):
            report, output = self._run(cases, executor_path=candidate)

        self.assertEqual("passed", report["status"])
        self.assertEqual(candidate_digest, report["executor"]["sha256"])
        self.assertNotIn(self.private_request, output.read_text(encoding="utf-8"))

    def test_global_bind_cannot_reverse_a_private_fixture_source_mapping(self) -> None:
        """Even a full CLI mapping is rejected when one private mapping differs."""

        shadow = self.data_root / "charges-shadow.csv"
        shadow.write_bytes((self.data_root / "charges.csv").read_bytes())
        cases = self._write_cases(self._actual_digest())
        report, output = self._run(
            cases,
            bindings=["rules=rules.csv", "charges=charges-shadow.csv"],
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual("global_binding_not_fixture_bound", report["failures"][0]["code"])
        serialized = output.read_text(encoding="utf-8")
        self.assertNotIn(self.private_request, serialized)
        self.assertNotIn("charges-shadow.csv", serialized)

    def test_global_bind_cannot_fill_a_fixture_that_omits_a_runtime_source(self) -> None:
        """A CLI value cannot turn an incomplete private fixture into a valid one."""

        expected_fingerprints = self._fixture_source_fingerprints()
        cases = self._write_cases(
            self._actual_digest(),
            source_bindings={"rules": "rules.csv"},
            source_fingerprints={"rules": expected_fingerprints["rules"]},
        )
        report, output = self._run(
            cases,
            bindings=["rules=rules.csv", "charges=charges.csv"],
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual("fixture_source_bindings_incomplete", report["failures"][0]["code"])
        self.assertNotIn(self.private_request, output.read_text(encoding="utf-8"))

    def test_all_executor_readable_sources_are_bound_into_the_snapshot_proof(self) -> None:
        contract = {
            "rule_source_ids": ["rules", "other-rules"],
            "sources": [
                {"source_id": "rules", "kind": "tabular", "runtime_required": True},
                {"source_id": "other-rules", "kind": "tabular", "runtime_required": True},
                {"source_id": "charges", "kind": "tabular", "runtime_required": True},
                {"source_id": "policy", "kind": "document", "runtime_required": True},
            ],
        }
        recipe = {
            "source_id": "charges",
            "rule_selector": {"source_id": "rules"},
            "context_source_ids": [],
        }
        self.assertEqual(
            {"rules", "other-rules", "charges", "policy"},
            RUNNER.replay_input_source_ids(recipe, contract),
        )


if __name__ == "__main__":
    unittest.main()
