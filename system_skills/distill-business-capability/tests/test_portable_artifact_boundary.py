"""Regression coverage for the portable artifact-root boundary."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


EXECUTOR = load_module(
    "portable_artifact_boundary_executor",
    "assets/portable-scenario-executor/scripts/execute_scenario.py",
)
TABULAR = load_module(
    "portable_artifact_boundary_tabular",
    "assets/portable-tabular-reader/scripts/query_tabular.py",
)
DOCUMENT = load_module(
    "portable_artifact_boundary_document",
    "assets/portable-document-reader/scripts/extract_documents.py",
)
STAGE = load_module(
    "portable_artifact_boundary_stage",
    "assets/portable-stage-runtime/scripts/run_stage.py",
)
ORCHESTRATOR = load_module(
    "portable_artifact_boundary_orchestrator",
    "assets/portable-orchestrator-runtime/scripts/orchestrate.py",
)


class PortableArtifactBoundaryTests(unittest.TestCase):
    def test_all_writers_accept_relative_and_translated_root_paths_only(self) -> None:
        modules = [EXECUTOR, TABULAR, DOCUMENT, STAGE, ORCHESTRATOR]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root.parent / "outside-artifact.json"
            with mock.patch.dict(os.environ, {"BUSINESS_ARTIFACT_ROOT": str(root)}, clear=False):
                for module in modules:
                    target, relative_path = module.resolve_artifact_name("nested/result.json", "--output")
                    self.assertEqual((root / "nested" / "result.json").resolve(), target)
                    self.assertEqual("nested/result.json", relative_path)

                    translated, translated_relative = module.resolve_artifact_name(
                        str(root / "legacy-sandbox-output.json"), "--output"
                    )
                    self.assertEqual((root / "legacy-sandbox-output.json").resolve(), translated)
                    self.assertEqual("legacy-sandbox-output.json", translated_relative)

                    with self.assertRaises(module.ArtifactPathError):
                        module.resolve_artifact_name(str(outside), "--output")
                    with self.assertRaises(module.ArtifactPathError):
                        module.resolve_artifact_name("../escape.json", "--output")

    def test_persisted_descriptors_never_publish_physical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.dict(os.environ, {"BUSINESS_ARTIFACT_ROOT": str(root)}, clear=False):
                evidence_path, _ = EXECUTOR.resolve_artifact_name("evidence.json")
                descriptor = EXECUTOR.write_artifact(evidence_path, {"status": "success"})
                self.assertEqual("evidence.json", descriptor["relative_path"])
                self.assertIn("artifact_id", descriptor)
                self.assertNotIn("path", descriptor)
                self.assertNotIn(str(root), str(descriptor))

                index_path, _ = DOCUMENT.resolve_artifact_name("policy.index.sqlite")
                index = DOCUMENT.create_index(
                    index_path, "policy.md", "a" * 64, [("line:1", "bounded policy evidence")]
                )
                self.assertTrue(index_path.is_file())
                self.assertEqual("policy.index.sqlite", index["relative_path"])
                self.assertIn("artifact_id", index)
                self.assertNotIn("index", index)
                self.assertNotIn("path", index["artifact"])


if __name__ == "__main__":
    unittest.main()
