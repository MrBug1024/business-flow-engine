from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain.agents.middleware import ModelRequest

from app.studio.capabilities import readiness
from app.studio.capabilities.readiness import (
    ensure_capability_readiness,
    refresh_platform_capabilities,
)
from app.studio.capabilities.registry import list_skills
from app.studio.capabilities.skill_middleware import ReloadingSkillsMiddleware
from app.studio.capabilities.tools import DynamicToolRegistry, tool_registry
from app.studio.models import SkillDefinition
from app.studio.prompt_loader import load_prompt, prompt_fields, render_prompt
from app.studio.runtime.agent import _system_prompt
from app.studio.runtime.capabilities import (
    Capability,
    optional_capability_catalog,
    optional_capability_index,
)


class PromptCatalogTests(unittest.TestCase):
    def test_prompt_templates_have_expected_fields(self) -> None:
        self.assertEqual(
            prompt_fields("agent/core-system.md"),
            {"optional_capability_index"},
        )
        self.assertEqual(
            prompt_fields("agent/skills-system.md"),
            {"skills_locations", "skills_load_warnings", "skills_list"},
        )
        self.assertEqual(prompt_fields("runtime/context-summary.md"), {"messages"})
        self.assertEqual(prompt_fields("runtime/resume.md"), {"answers"})
        self.assertEqual(
            prompt_fields("runtime/auto-continuation.md"),
            {"segment_index", "original_goal", "continuation_error", "task_manifest"},
        )

    def test_render_prompt_rejects_missing_or_extra_values(self) -> None:
        with self.assertRaises(ValueError):
            render_prompt("runtime/resume.md")
        with self.assertRaises(ValueError):
            render_prompt("runtime/resume.md", answers="ok", extra="not allowed")

    def test_core_prompt_requires_proactive_capability_selection(self) -> None:
        rendered = _system_prompt(
            optional_capability_index="- Optional Tools: 1 available\n  - `lookup`: Find records"
        )
        self.assertIn("Capability-selection gate", rendered)
        self.assertIn("Do not wait for the user", rendered)
        self.assertIn("`lookup`", rendered)

    def test_skill_prompt_is_enabled_and_bounded(self) -> None:
        middleware = ReloadingSkillsMiddleware(
            backend=object(),
            sources=[("/skills/", "Studio")],
            system_prompt=load_prompt("agent/skills-system.md"),
        )
        skills = [
            {
                "name": f"skill-{index:02d}",
                "description": "specialized task " + ("x" * 400),
                "path": f"/skills/skill-{index:02d}/SKILL.md",
                "allowed_tools": [],
            }
            for index in range(20)
        ]
        catalog = middleware._format_skills_list(skills)
        self.assertIn("skill-00", catalog)
        self.assertNotIn("skill-19", catalog)
        self.assertIn("8 additional Skills", catalog)
        self.assertLess(len(catalog), 4_000)

    def test_skills_middleware_injects_catalog_into_model_request(self) -> None:
        middleware = ReloadingSkillsMiddleware(
            backend=object(),
            sources=[("/skills/", "Studio")],
            system_prompt=load_prompt("agent/skills-system.md"),
        )
        request = ModelRequest(
            model=object(),
            messages=[],
            system_prompt="CORE",
            state={
                "skills_metadata": [
                    {
                        "name": "ocr-parser",
                        "description": "Parse scanned documents with OCR.",
                        "path": "/skills/ocr-parser/SKILL.md",
                        "allowed_tools": [],
                    }
                ],
                "skills_load_errors": [],
            },
        )
        modified = middleware.modify_request(request)
        content = str(modified.system_message.content)
        self.assertIn("CORE", content)
        self.assertIn("ocr-parser", content)
        self.assertIn("Do not wait for the user", content)


class CapabilityReadinessTests(unittest.TestCase):
    def test_first_run_fallback_refreshes_import_time_registry(self) -> None:
        generation = tool_registry.generation
        with patch.object(readiness, "_BOOTSTRAPPED", False):
            report = ensure_capability_readiness()
        self.assertGreater(tool_registry.generation, generation)
        self.assertTrue(report["bootstrapped"])

    def test_bundled_capabilities_are_ready_and_declared(self) -> None:
        report = refresh_platform_capabilities()
        self.assertEqual(report["status"], "ready", report["issues"])
        self.assertEqual(report["tools"]["errors"], 0)
        self.assertEqual(report["tools"]["undeclared_contracts"], [])
        self.assertEqual(report["skills"]["errors"], 0)
        self.assertEqual(report["skills"]["undeclared_system_contracts"], [])
        self.assertGreaterEqual(report["tools"]["mounted"], 3)
        self.assertGreaterEqual(report["skills"]["system"], 3)
        for skill in list_skills():
            self.assertEqual(skill.contract_status, "declared", skill.name)
            self.assertTrue(skill.capability_id, skill.name)
            self.assertTrue(skill.responsibility, skill.name)
            self.assertTrue(skill.excludes, skill.name)

    def test_malformed_tool_contract_is_not_mounted(self) -> None:
        source = """from langchain_core.tools import tool

@tool(description=\"Invalid contract test Tool.\")
def invalid_contract_tool(value: str) -> str:
    return value

invalid_contract_tool.metadata = {
    \"studio\": {
        \"capability\": {
            \"id\": \"invalid-contract\",
            \"responsibility\": \"Validate one test contract.\",
            \"excludes\": \"must be a list\",
        }
    }
}
"""
        with TemporaryDirectory() as temporary:
            Path(temporary, "invalid_tool.py").write_text(source, encoding="utf-8")
            registry = DynamicToolRegistry(Path(temporary))
            metadata = registry.list()
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].status, "error")
        self.assertFalse(metadata[0].mounted)
        self.assertIn("excludes must be a list", metadata[0].error or "")

    def test_health_endpoint_also_initializes_readiness(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with patch.object(readiness, "_BOOTSTRAPPED", False):
            with TestClient(app) as client:
                response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["capabilities"]["bootstrapped"])


class CapabilityRoutingTests(unittest.TestCase):
    def test_optional_index_prefers_declared_responsibility_and_is_bounded(self) -> None:
        tools = [
            Capability(
                function_name=f"tool_{index}",
                display_name=f"Tool {index}",
                kind="tool",
                description="generic description",
                input_schema={"type": "object"},
                source="tests",
                responsibility=f"responsibility {index}",
            )
            for index in range(20)
        ]
        rendered = optional_capability_index(tools, [])
        self.assertIn("responsibility 0", rendered)
        self.assertNotIn("generic description", rendered)
        self.assertIn("10 more omitted", rendered)
        self.assertLess(len(rendered), 4_000)

    def test_discovery_returns_responsibility_and_exclusions(self) -> None:
        tool = Capability(
            function_name="relation_probe",
            display_name="Relation probe",
            kind="tool",
            description="Find links",
            input_schema={"type": "object", "properties": {}},
            capability_id="probe-relations",
            responsibility="Find direct relation evidence.",
            excludes=["Derive business flow"],
        )
        skill = SkillDefinition(
            name="flow-skill",
            description="Derive a flow",
            capability_id="derive-flow",
            responsibility="Derive a validated business flow.",
            excludes=["Package a Skill"],
            contract_status="declared",
        )
        result = optional_capability_catalog(
            [tool],
            [],
            [skill],
            kind="all",
            limit=10,
        )
        by_name = {item["name"]: item for item in result["items"]}
        self.assertEqual(by_name["relation_probe"]["capability_id"], "probe-relations")
        self.assertEqual(by_name["relation_probe"]["excludes"], ["Derive business flow"])
        self.assertEqual(by_name["flow-skill"]["capability_id"], "derive-flow")
        self.assertEqual(by_name["flow-skill"]["excludes"], ["Package a Skill"])


if __name__ == "__main__":
    unittest.main()
