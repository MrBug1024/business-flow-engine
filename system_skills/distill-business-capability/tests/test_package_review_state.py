"""Regression coverage for the final offline-capability package checkpoint."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.studio.models import DistillationApproval
from app.studio.storage import (
    CAPABILITY_MANIFEST_RELATIVE,
    CAPABILITY_MCP_ARCHIVE_RELATIVE,
    CAPABILITY_RELEASE_MANIFEST_RELATIVE,
    CAPABILITY_SKILL_ARCHIVE_RELATIVE,
    StudioStore,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CapabilityPackageReviewTests(unittest.TestCase):
    """Keep package publication separate from deterministic recipe verification."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temporary.name))
        self.record = self.store.create("Evidence-only package", owner_id="reviewer")
        self.record.distillation.current_phase = "package"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_candidate(self, *, publication_verifiable: bool = True) -> Path:
        workspace = self.store.workspace_dir(self.record.id)
        relation = workspace / "outputs/data-relations/scenario-relationship.json"
        flow = workspace / "outputs/business-flow/business-flow.json"
        write_json(relation, {"status": "complete", "kind": "relations"})
        write_json(flow, {"status": "complete", "kind": "flow"})

        skill_archive = workspace / CAPABILITY_SKILL_ARCHIVE_RELATIVE
        mcp_archive = workspace / CAPABILITY_MCP_ARCHIVE_RELATIVE
        skill_archive.parent.mkdir(parents=True, exist_ok=True)
        skill_archive.write_bytes(b"safe evidence-only skill archive")
        mcp_archive.write_bytes(b"safe evidence-only mcp archive")
        skill_digest = sha256_file(skill_archive)
        mcp_digest = sha256_file(mcp_archive)

        release = {
            "format": "portable-business-capability-release",
            "artifact_digests": {
                "skill_zip": skill_digest,
                "mcp_stdio_zip": mcp_digest,
            },
        }
        write_json(workspace / CAPABILITY_RELEASE_MANIFEST_RELATIVE, release)
        manifest_path = workspace / CAPABILITY_MANIFEST_RELATIVE
        manifest = {
            "status": "complete",
            # This intentional evidence-only state keeps the runtime out of
            # deterministic conclusion mode but remains human-publishable.
            "verification": {
                "status": "unverified",
                "verifiable": False,
                "publishable": False,
                "verified_recipe_ids": [],
            },
            "publication": {
                "status": "pending_human_platform_package_approval",
                "verifiable": publication_verifiable,
                "publishable": False,
            },
            "source": {
                "relation_fingerprint": sha256_file(relation),
                "flow_fingerprint": sha256_file(flow),
            },
            "release": {
                "schema_version": 1,
                "artifact_digests": {
                    "skill_zip": skill_digest,
                    "mcp_stdio_zip": mcp_digest,
                },
            },
            "artifact_digests": {
                "skill_archive": skill_digest,
                "mcp_stdio_archive": mcp_digest,
            },
        }
        write_json(manifest_path, manifest)
        self.record.distillation.approvals = [
            DistillationApproval(
                id="approval_capability",
                phase="capability",
                decision="approved",
                artifact_id=CAPABILITY_MANIFEST_RELATIVE,
                artifact_fingerprint=sha256_file(manifest_path),
                actor_id="reviewer",
                revision=self.record.distillation.revision,
                source_revision=self.record.distillation.source_revision,
                created_at=0,
            )
        ]
        return skill_archive

    def test_evidence_only_candidate_is_reviewable_and_publishable_by_human(self) -> None:
        archive = self._write_candidate()

        manifest_path, manifest, _ = self.store._capability_package_candidate(
            self.record,
            skill_archive_fingerprint=sha256_file(archive),
        )
        contract = self.store._build_distillation_artifact_contract(self.record, "package")

        self.assertFalse(manifest["verification"]["verifiable"])
        self.assertTrue(manifest_path.samefile(
            self.store.workspace_dir(self.record.id) / CAPABILITY_MANIFEST_RELATIVE,
        ))
        self.assertTrue(contract["reviewable"])
        self.assertEqual("pending_human_platform_package_approval", contract["status"])

    def test_package_question_is_not_offered_when_publication_is_not_verifiable(self) -> None:
        archive = self._write_candidate(publication_verifiable=False)

        with self.assertRaisesRegex(ValueError, "not a current pending human package-review candidate"):
            self.store._capability_package_candidate(
                self.record,
                skill_archive_fingerprint=sha256_file(archive),
            )
        contract = self.store._build_distillation_artifact_contract(self.record, "package")
        self.assertFalse(contract["reviewable"])
        self.assertEqual("not_current_package_review_candidate", contract["status"])


if __name__ == "__main__":
    unittest.main()
