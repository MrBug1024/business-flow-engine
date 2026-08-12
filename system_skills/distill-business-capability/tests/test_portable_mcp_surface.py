"""Regression checks for the default one-action MCP surface."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "assets" / "portable-mcp-adapter" / "mcp_server.py"
SPEC = importlib.util.spec_from_file_location("portable_mcp_surface_under_test", SERVER_PATH)
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVER
SPEC.loader.exec_module(SERVER)


class PortableMcpSurfaceTests(unittest.TestCase):
    def test_default_surface_exposes_only_execute(self) -> None:
        self.assertEqual({"execute"}, SERVER.EXPOSED_ACTIONS)
        self.assertEqual([f"{SERVER.NAMESPACE}__execute"], [item["name"] for item in SERVER.TOOLS])
        schema = SERVER.TOOLS[0]["inputSchema"]
        self.assertIn("request", schema["required"])
        self.assertIn("data_dir", schema["properties"])
        self.assertIn("delivery_output", schema["properties"])
        self.assertEqual(["auto", "json", "csv", "xlsx"], schema["properties"]["delivery_format"]["enum"])

    def test_hidden_action_is_rejected_even_if_called_directly(self) -> None:
        response = SERVER.tool_response(f"{SERVER.NAMESPACE}__query_data", {"data_dir": "unused", "sql": "SELECT 1"})
        self.assertTrue(response["isError"])
        self.assertIn("only the primary execute", response["content"][0]["text"])

    def test_execute_forwards_a_verified_delivery_request_in_the_same_transaction(self) -> None:
        class FakeExecutor:
            argv: list[str] = []

            def run(self, argv: list[str]) -> dict[str, str]:
                self.argv = argv
                return {"status": "completed_deterministically"}

        fake = FakeExecutor()
        with mock.patch.object(SERVER, "executor", return_value=fake):
            result = SERVER.execute_business_request({
                "request": "export the verified result",
                "data_dir": "C:/runtime/data",
                "out_dir": "C:/runtime/outputs",
                "delivery_output": "C:/runtime/outputs/result.csv",
                "delivery_format": "csv",
                "delivery_template_id": "exact-columns",
            })

        self.assertEqual({"status": "completed_deterministically"}, result)
        self.assertEqual([
            "execute", "--request", "export the verified result", "--data-root", "C:/runtime/data",
            "--max-rows", "50", "--output", str(Path("C:/runtime/outputs") / "scenario-evidence.json"),
            "--delivery-output", "C:/runtime/outputs/result.csv", "--delivery-format", "csv",
            "--delivery-template-id", "exact-columns",
        ], fake.argv)


if __name__ == "__main__":
    unittest.main()
