"""Regression checks for the legacy package-host compatibility bridge."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATH = (
    ROOT / "assets" / "portable-platform-compatibility" / "scripts" / "compatibility_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("portable_platform_compatibility_under_test", RUNTIME_PATH)
assert SPEC and SPEC.loader
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)


class PlatformCompatibilityTests(unittest.TestCase):
    def test_modern_artifacts_are_safe_for_string_only_legacy_hosts(self) -> None:
        payload = {
            "status": "ready_for_agent_judgment",
            "candidate_evidence": {"records": [{"id": "a"}, {"id": "b"}]},
            "artifact": {"kind": "scenario_evidence_package", "path": "C:/runtime/outputs/scenario-evidence.json"},
            "agent_handoff": {"kind": "scenario_agent_handoff", "path": "C:/runtime/outputs/scenario-evidence.agent.json"},
        }
        result = RUNTIME.host_action_envelope(payload)

        self.assertEqual("scenario_execution", result["mode"])
        self.assertEqual(2, result["rows"])
        self.assertEqual(
            "bounded_evidence_record_groups_not_final_business_rows", result["row_semantics"]
        )
        self.assertEqual("C:/runtime/outputs/scenario-evidence.json", result["artifact"])
        self.assertEqual(payload["artifact"], result["evidence_artifact"])
        self.assertEqual(
            "C:/runtime/outputs/scenario-evidence.agent.json",
            result["host_response_contract"]["agent_handoff_path"],
        )
        self.assertTrue(result["host_response_contract"]["requires_structured_passthrough"])

    def test_deterministic_rows_remain_distinct_from_evidence_groups(self) -> None:
        result = RUNTIME.host_action_envelope({
            "status": "completed_deterministically",
            "deterministic_result": {"rows": [{"id": "a"}]},
        })
        self.assertEqual(1, result["rows"])
        self.assertEqual("deterministic_business_result_rows", result["row_semantics"])


if __name__ == "__main__":
    unittest.main()
