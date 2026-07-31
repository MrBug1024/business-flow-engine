from __future__ import annotations

import asyncio
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from app.api import businesses as business_api
from app.api.businesses import (
    _clear_import_tombstones,
    _normalized_upload_paths,
    _prepare_upload_destination,
    _safe_upload_relative_path,
)
from app.studio.models import BusinessContext, BusinessRecord


class BusinessFileUploadPathTests(unittest.TestCase):
    def test_nested_folder_path_is_preserved(self) -> None:
        self.assertEqual(
            _safe_upload_relative_path(r"audit-pack\rules\national\rules.xlsx"),
            "audit-pack/rules/national/rules.xlsx",
        )

    def test_unsafe_or_nonportable_paths_are_rejected(self) -> None:
        for value in (
            "/absolute/file.csv",
            "C:/absolute/file.csv",
            "../outside.csv",
            "folder/../outside.csv",
            "folder/bad:name.csv",
            "folder/trailing. ",
        ):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                _safe_upload_relative_path(value)

    def test_batch_paths_must_match_files_and_be_unique_case_insensitively(self) -> None:
        files = [
            UploadFile(filename="a.csv", file=io.BytesIO(b"a")),
            UploadFile(filename="b.csv", file=io.BytesIO(b"b")),
        ]
        with self.assertRaisesRegex(HTTPException, "paths"):
            _normalized_upload_paths(files, ["folder/a.csv"])
        with self.assertRaisesRegex(HTTPException, "重复"):
            _normalized_upload_paths(files, ["folder/A.csv", "folder/a.csv"])

    def test_destination_creates_arbitrary_nested_directories_under_data(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            data_root = workspace / "data"
            data_root.mkdir()
            destination = _prepare_upload_destination(
                workspace,
                data_root,
                "hospital-a/2026/quarter-1/rules/rule.csv",
            )
            self.assertEqual(
                destination.relative_to(workspace).as_posix(),
                "data/hospital-a/2026/quarter-1/rules/rule.csv",
            )
            self.assertTrue(destination.parent.is_dir())

    def test_reimport_under_deleted_folder_clears_ancestor_tombstone(self) -> None:
        record = SimpleNamespace(
            workspace_deleted_paths=["data/archive", "data/unrelated", "data/archive/old.csv"]
        )
        _clear_import_tombstones(record, "data/archive/2026/new.csv")
        self.assertEqual(record.workspace_deleted_paths, ["data/unrelated", "data/archive/old.csv"])

    def test_data_upload_endpoint_writes_the_complete_relative_tree(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            data_root = workspace / "data"
            data_root.mkdir()
            record = BusinessRecord(
                id="biz-upload",
                owner_id="owner",
                name="Nested upload",
                created_at=1.0,
                updated_at=1.0,
                context=BusinessContext(business_id="biz-upload", name="Nested upload"),
            )
            files = [
                UploadFile(filename="rule.csv", file=io.BytesIO(b"id,rule\n1,test\n")),
                UploadFile(filename="policy.md", file=io.BytesIO(b"# Policy\n")),
            ]
            paths = [
                "audit-input/rules/2026/rule.csv",
                "audit-input/reference/policies/policy.md",
            ]
            with (
                patch.object(business_api, "_record_or_404", return_value=record),
                patch.object(business_api.store, "workspace_dir", return_value=workspace),
                patch.object(business_api.store, "files_dir", return_value=data_root),
                patch.object(business_api.store, "create_version"),
                patch.object(business_api.store, "save", side_effect=lambda value: value),
            ):
                result = asyncio.run(
                    business_api.upload_business_files("biz-upload", files=files, paths=paths)
                )

            self.assertTrue((data_root / paths[0]).is_file())
            self.assertTrue((data_root / paths[1]).is_file())
            self.assertEqual(len(result.files), 2)
            self.assertEqual(
                {Path(item.storage_path).relative_to(workspace).as_posix() for item in result.files},
                {f"data/{path}" for path in paths},
            )


if __name__ == "__main__":
    unittest.main()
