from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain.agents.middleware import ModelRequest

from app.studio.capabilities import readiness
from app.studio.capabilities.readiness import (
    ensure_capability_readiness,
    refresh_platform_capabilities,
)
from app.studio.capabilities.registry import list_skills
from app.studio.capabilities.skill_middleware import ReloadingSkillsMiddleware
from app.studio.capabilities.tools import DynamicToolRegistry, tool_registry
from app.studio.completion import has_positive_completion_claim, validate_task_completion
from app.studio.models import AIRun, SkillDefinition
from app.studio.orchestrator import (
    BusinessOrchestrator,
    _completion_claim_issues,
    _has_durable_task_checkpoint,
    _is_manual_continuation_message,
)
from app.studio.prompt_loader import load_prompt, prompt_fields, render_prompt
from app.studio.runtime.agent import _system_prompt
from app.studio.runtime.capabilities import (
    Capability,
    optional_capability_catalog,
    optional_capability_index,
)
from app.studio.runtime.sandbox import (
    LocalVenvSandboxBackend,
    _explicit_requirement_skill_names,
    _referenced_skill_names,
    _skill_sandbox_environment,
)
from app.studio.runtime.tool_context import StudioToolContext, bind_tool_context
from app.studio.storage import StudioStore
from tools.report_task_progress import report_task_progress


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


class DistillationCredentialScopeTests(unittest.TestCase):
    @patch("app.studio.capabilities.skill_secrets.skill_secret_store.sandbox_environment")
    def test_finalize_receives_source_skill_credentials_only_in_generation_phase(self, sandbox_environment) -> None:
        sandbox_environment.return_value = {"VECTOR_KB_API_KEY": "configured"}
        finalize = (
            'python "/skills/distill-business-capability/scripts/distill_capabilities.py" '
            'finalize --claims "/workspace/candidate.json"'
        )
        self.assertEqual(_skill_sandbox_environment(finalize), {"VECTOR_KB_API_KEY": "configured"})
        sandbox_environment.assert_called_once_with(
            finalize, skill_names=("ocr-parser", "vector-kb")
        )

        sandbox_environment.reset_mock()
        prepare = (
            'python "/skills/distill-business-capability/scripts/distill_capabilities.py" '
            'prepare --relations "/workspace/relations.json"'
        )
        _skill_sandbox_environment(prepare)
        sandbox_environment.assert_called_once_with(prepare, skill_names=())


class SkillDependencyRuntimeTests(unittest.TestCase):
    def test_skill_references_and_explicit_requirement_commands_are_detected(self) -> None:
        script = "python /skills/discover-data-relations/scripts/analyze_relations.py analyze"
        install = "python -m pip install -r /skills/discover-data-relations/requirements.txt"
        self.assertEqual(_referenced_skill_names(script), {"discover-data-relations"})
        self.assertEqual(
            _explicit_requirement_skill_names(install),
            {"discover-data-relations"},
        )
        self.assertNotIn("..", _referenced_skill_names("python /skills/../scripts/x.py"))

    @patch("app.studio.runtime.sandbox.subprocess.run")
    def test_requirements_are_installed_once_per_content_digest(self, run: Mock) -> None:
        run.return_value = Mock(returncode=0, stdout=b"ok")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            skills = root / "skills"
            temp = root / "temp"
            venv = root / "venv"
            for directory in (workspace, skills / "demo", temp, venv):
                directory.mkdir(parents=True, exist_ok=True)
            requirements = skills / "demo" / "requirements.txt"
            requirements.write_text("example-package==1.0\n", encoding="utf-8")
            backend = LocalVenvSandboxBackend(
                runtime_id="test",
                workspace_root=workspace,
                skills_root=skills,
                temp_root=temp,
                venv_root=venv,
            )
            environment = backend._base_environment()

            self.assertIsNone(backend._ensure_skill_dependencies({"demo"}, environment))
            self.assertIsNone(backend._ensure_skill_dependencies({"demo"}, environment))
            self.assertEqual(run.call_count, 1)

            requirements.write_text("example-package==2.0\n", encoding="utf-8")
            self.assertIsNone(backend._ensure_skill_dependencies({"demo"}, environment))
            self.assertEqual(run.call_count, 2)

    @patch("app.studio.runtime.sandbox._venv_python", return_value=Path(sys.executable))
    def test_multiline_inline_python_preserves_stdout_without_shell(self, _python: Mock) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            skills = root / "skills"
            temp = root / "temp"
            venv = root / "venv"
            for directory in (workspace / "nested", skills, temp, venv):
                directory.mkdir(parents=True, exist_ok=True)
            backend = LocalVenvSandboxBackend(
                runtime_id="test",
                workspace_root=workspace,
                skills_root=skills,
                temp_root=temp,
                venv_root=venv,
                execution_environment_provider=lambda _command: {},
            )

            response = backend.execute(
                "cd /workspace/nested && python -c \"from pathlib import Path\n"
                "print('line one')\n"
                "print(Path('/workspace/probe.txt').parent.name)\""
            )

        self.assertEqual(response.exit_code, 0, response.output)
        self.assertIn("line one", response.output)
        self.assertIn("workspace", response.output)


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


class ArtifactCompletionContractTests(unittest.TestCase):
    def test_model_limit_accepts_platform_evidence_as_a_durable_checkpoint(self) -> None:
        run = SimpleNamespace(
            task_progress={},
            events=[
                {
                    "type": "skill_activation",
                    "status": "succeeded",
                    "skill_name": "discover-data-relations",
                }
            ],
        )
        empty_run = SimpleNamespace(task_progress={}, events=[])

        self.assertTrue(_has_durable_task_checkpoint(run))
        self.assertFalse(_has_durable_task_checkpoint(empty_run))

    def test_bare_continuation_detection_is_conservative(self) -> None:
        self.assertTrue(_is_manual_continuation_message("继续"))
        self.assertTrue(_is_manual_continuation_message("Please continue."))
        self.assertFalse(_is_manual_continuation_message("继续讨论新的业务流程设计"))

    def test_platform_checkpoint_auto_continues_model_call_limit(self) -> None:
        with TemporaryDirectory() as temporary:
            temporary_store = StudioStore(Path(temporary))
            record = temporary_store.create("Relations", owner_id="owner_relations")
            calls: list[dict[str, object]] = []

            def fake_run_agent(*_args, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    yield {
                        "type": "skill_activation",
                        "status": "succeeded",
                        "skill_name": "discover-data-relations",
                        "path": "/skills/discover-data-relations/SKILL.md",
                    }
                    raise RuntimeError("Model call limits exceeded: run limit (64/64)")
                yield {"type": "token", "content": "已从平台检查点恢复，等待下一项处理。"}

            with (
                patch("app.studio.orchestrator.store", temporary_store),
                patch("app.studio.orchestrator.run_agent", fake_run_agent),
                patch(
                    "app.studio.orchestrator.studio_settings.active_model_name",
                    return_value="test-model",
                ),
            ):
                events = list(
                    BusinessOrchestrator().stream_chat(record, "推导数据关系")
                )

        self.assertEqual(sum(event["type"] == "task_handoff" for event in events), 1)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(len(record.runs), 2)
        self.assertEqual(record.runs[0].task_id, record.runs[1].task_id)
        self.assertEqual(record.runs[1].continued_from_run_id, record.runs[0].id)
        checkpoint = record.runs[0].task_progress["platform_checkpoint"]
        self.assertEqual(checkpoint["skills"], ["discover-data-relations"])
        self.assertIn("推导数据关系", str(calls[1]["user_prompt"]))

    def test_bare_continue_rejoins_task_created_before_legacy_detached_run(self) -> None:
        with TemporaryDirectory() as temporary:
            temporary_store = StudioStore(Path(temporary))
            record = temporary_store.create("Relations", owner_id="owner_relations")
            session = record.chat_sessions[0]
            original_message = temporary_store.append_message(
                record,
                "user",
                "推导数据关系",
                session_id=session.id,
            )
            original_message.created_at = 1.0
            original_run = AIRun(
                id="run_original",
                business_id=record.id,
                session_id=session.id,
                task_id="task_original",
                segment_index=1,
                status="failed",
                task_progress={
                    "status": "continuing",
                    "objective": "推导数据关系",
                    "summary": "候选关系已生成。",
                },
                started_at=2.0,
                finished_at=2.5,
                error="network error",
            )
            temporary_store.append_run(record, original_run)
            detached_message = temporary_store.append_message(
                record,
                "user",
                "继续",
                session_id=session.id,
            )
            detached_message.created_at = 3.0
            temporary_store.append_run(
                record,
                AIRun(
                    id="run_detached",
                    business_id=record.id,
                    session_id=session.id,
                    task_id="task_detached",
                    segment_index=1,
                    status="failed",
                    started_at=4.0,
                    finished_at=5.0,
                    error="Model call limits exceeded: run limit (64/64)",
                ),
            )
            calls: list[dict[str, object]] = []

            def fake_run_agent(*_args, **kwargs):
                calls.append(kwargs)
                yield {"type": "token", "content": "已接回原任务；当前仍有待处理工作。"}

            with (
                patch("app.studio.orchestrator.store", temporary_store),
                patch("app.studio.orchestrator.run_agent", fake_run_agent),
                patch(
                    "app.studio.orchestrator.studio_settings.active_model_name",
                    return_value="test-model",
                ),
            ):
                events = list(
                    BusinessOrchestrator().stream_chat(
                        record,
                        "继续",
                        session_id=session.id,
                    )
                )

        resumed = record.runs[-1]
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(resumed.task_id, "task_original")
        self.assertEqual(resumed.segment_index, 2)
        self.assertEqual(resumed.continued_from_run_id, "run_original")
        self.assertFalse(bool(calls[0]["include_history"]))
        self.assertIn("原始目标：\n推导数据关系", str(calls[0]["user_prompt"]))

    def test_derive_contract_rejects_missing_target_scenario_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "outputs" / "business-flow"
            partial.mkdir(parents=True)
            (partial / "business-flow-synthesis.json").write_text(
                json.dumps({"status": "ready"}), encoding="utf-8"
            )

            validation = validate_task_completion(root, prompt="推导业务流程")

        self.assertFalse(validation.valid)
        self.assertEqual(validation.skills, ("derive-business-flow",))
        self.assertTrue(
            any("business-flow.json" in issue for issue in validation.issues),
            validation.issues,
        )
        self.assertTrue(
            any("flow-claims.json" in issue for issue in validation.issues),
            validation.issues,
        )

    def test_distillation_contract_matches_natural_skill_package_request(self) -> None:
        with TemporaryDirectory() as temporary:
            validation = validate_task_completion(
                temporary,
                prompt="请生成这个业务场景的 Skill 技能包",
            )
        self.assertFalse(validation.valid)
        self.assertEqual(validation.skills, ("distill-business-capability",))
        self.assertTrue(
            any("capability-manifest.json" in issue for issue in validation.issues),
            validation.issues,
        )

    def test_distillation_contract_rejects_old_generator_outputs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations_dir = root / "outputs" / "data-relations"
            flow_dir = root / "outputs" / "business-flow"
            distill_dir = root / "outputs" / "capability-distillation"
            relations_dir.mkdir(parents=True)
            flow_dir.mkdir(parents=True)
            distill_dir.mkdir(parents=True)
            relation = relations_dir / "scenario-relationship.json"
            operational = relations_dir / "operational-data-contract.json"
            flow = flow_dir / "business-flow.json"
            prompt = distill_dir / "agent_prompts.md"
            relation.write_text('{"status":"complete"}', encoding="utf-8")
            operational.write_text('{"status":"ready"}', encoding="utf-8")
            flow.write_text('{"status":"complete"}', encoding="utf-8")
            prompt.write_text("# Agent prompt\n", encoding="utf-8")
            manifest_path = distill_dir / "capability-manifest.json"
            manifest = {
                "status": "complete",
                "source": {
                    "relation_fingerprint": _sha256(relation),
                    "flow_fingerprint": _sha256(flow),
                    "operational_contract_fingerprint": _sha256(operational),
                },
                "artifact_digests": {"agent_prompts": _sha256(prompt)},
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            for name, content in (
                ("capability-plan.json", "{}\n"),
                ("capability-map.mmd", "flowchart LR\n"),
                ("distillation-report.md", "# Report\n"),
            ):
                (distill_dir / name).write_text(content, encoding="utf-8")

            stale = validate_task_completion(root, prompt="生成业务场景能力包")
            self.assertFalse(stale.valid)
            self.assertTrue(any("generator_contract_version" in issue for issue in stale.issues))

            manifest["generator_contract_version"] = 2
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            current = validate_task_completion(root, prompt="生成业务场景能力包")
            self.assertTrue(current.valid, current.issues)

    def test_derive_contract_accepts_complete_files_and_current_fingerprints(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations_dir = root / "outputs" / "data-relations"
            flow_dir = root / "outputs" / "business-flow"
            relations_dir.mkdir(parents=True)
            flow_dir.mkdir(parents=True)
            relation_path = relations_dir / "scenario-relationship.json"
            operational_path = relations_dir / "operational-data-contract.json"
            relation_path.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
            operational_path.write_text(json.dumps({"status": "ready"}), encoding="utf-8")
            flow_path = flow_dir / "business-flow.json"
            flow_path.write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "source": {
                            "fingerprint": _sha256(relation_path),
                            "operational_data_contract": {
                                "fingerprint": _sha256(operational_path)
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (flow_dir / "business-flow.mmd").write_text("flowchart LR\n", encoding="utf-8")
            (flow_dir / "business-flow-report.md").write_text("# Flow\n", encoding="utf-8")
            (flow_dir / "flow-claims.json").write_text("{}\n", encoding="utf-8")
            artifacts = [
                "/workspace/outputs/business-flow/business-flow.json",
                "/workspace/outputs/business-flow/business-flow.mmd",
                "/workspace/outputs/business-flow/business-flow-report.md",
                "/workspace/outputs/business-flow/flow-claims.json",
            ]

            validation = validate_task_completion(
                root,
                prompt="推导业务流程",
                artifacts=artifacts,
                require_reported_artifacts=True,
            )
            self.assertTrue(validation.valid, validation.issues)

            relation_path.write_text(
                json.dumps({"status": "complete", "changed": True}),
                encoding="utf-8",
            )
            stale = validate_task_completion(root, prompt="推导业务流程", artifacts=artifacts)
            self.assertFalse(stale.valid)
            self.assertTrue(any("fingerprint" in issue for issue in stale.issues), stale.issues)

    def test_reported_artifact_must_be_a_real_nonempty_workspace_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = Path(temporary).parent / "outside.json"
            missing = validate_task_completion(
                root,
                artifacts=["/workspace/outputs/missing.json"],
                skills=[],
            )
            escaping = validate_task_completion(
                root,
                artifacts=[str(outside), "../../outside.json"],
                skills=[],
            )
        self.assertFalse(missing.valid)
        self.assertTrue(any("does not exist" in issue for issue in missing.issues))
        self.assertFalse(escaping.valid)
        self.assertTrue(
            any(
                "outside /workspace" in issue or "escapes /workspace" in issue
                for issue in escaping.issues
            )
        )

    def test_completion_claim_detection_distinguishes_blockers(self) -> None:
        self.assertTrue(has_positive_completion_claim("业务流程推导完成，产物已生成。"))
        self.assertTrue(has_positive_completion_claim("status=complete"))
        self.assertFalse(has_positive_completion_claim("业务流程尚未完成，缺少上游产物。"))
        self.assertFalse(has_positive_completion_claim("需要完成校验后才能交付。"))

    def test_orchestrator_rejects_false_flow_completion_before_persisting_final(self) -> None:
        with TemporaryDirectory() as temporary:
            run = SimpleNamespace(
                task_id="task_flow",
                session_id="session_flow",
                started_at=20.0,
                task_progress={},
                events=[],
            )
            record = SimpleNamespace(
                id="business_flow",
                owner_id="owner_flow",
                runs=[run],
                messages=[
                    SimpleNamespace(
                        role="user",
                        session_id="session_flow",
                        created_at=10.0,
                        content="推导业务流程",
                    )
                ],
            )
            with patch("app.studio.orchestrator.store.workspace_dir", return_value=Path(temporary)):
                issues = _completion_claim_issues(
                    record,
                    run,
                    "推导业务流程",
                    "业务流程推导完成，所有文件已经落盘。",
                )
        self.assertTrue(issues)
        self.assertTrue(any("business-flow.json" in issue for issue in issues), issues)

    def test_progress_tool_refuses_complete_when_skill_artifacts_are_missing(self) -> None:
        with TemporaryDirectory() as temporary:
            run = SimpleNamespace(
                id="run_flow",
                task_id="task_flow",
                session_id="session_flow",
                started_at=20.0,
                task_progress={},
                plan=[],
                events=[],
            )
            record = SimpleNamespace(
                owner_id="owner_flow",
                runs=[run],
                messages=[
                    SimpleNamespace(
                        role="user",
                        session_id="session_flow",
                        created_at=10.0,
                        content="推导业务流程",
                    )
                ],
            )
            context = StudioToolContext(
                business_id="business_flow",
                session_id="session_flow",
                run_id="run_flow",
                workspace_path=Path(temporary),
                record=record,
                _save=lambda: None,
            )
            with bind_tool_context(context):
                result = report_task_progress.invoke(
                    {
                        "action": "complete",
                        "summary": "业务流程已经全部完成",
                        "result": "业务流程已经完成",
                        "verification": "已验收",
                        "artifacts": [
                            "/workspace/outputs/business-flow/business-flow.json"
                        ],
                    }
                )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["recorded_action"], "update")
        self.assertEqual(run.task_progress["status"], "running")
        self.assertEqual(run.task_progress["summary"], "交付完成声明已被磁盘验收拒绝。")
        self.assertFalse(run.task_progress["completion_validation"]["valid"])
        self.assertEqual(context.emitted_events[-1]["action"], "update")

    def test_progress_tool_accepts_a_real_artifact_without_a_skill_contract(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "outputs" / "answer.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("# Answer\n", encoding="utf-8")
            run = SimpleNamespace(
                id="run_answer",
                task_id="task_answer",
                session_id="session_answer",
                started_at=20.0,
                task_progress={},
                plan=[],
                events=[],
            )
            record = SimpleNamespace(owner_id="owner", runs=[run], messages=[])
            context = StudioToolContext(
                business_id="business_answer",
                session_id="session_answer",
                run_id="run_answer",
                workspace_path=root,
                record=record,
                _save=lambda: None,
            )
            with bind_tool_context(context):
                result = report_task_progress.invoke(
                    {
                        "action": "complete",
                        "result": "报告已完成",
                        "verification": "文件存在且非空",
                        "artifacts": ["/workspace/outputs/answer.md"],
                    }
                )

        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["recorded_action"], "complete")
        self.assertEqual(run.task_progress["status"], "completed")

    def test_false_final_is_never_persisted_and_stops_after_bounded_retries(self) -> None:
        with TemporaryDirectory() as temporary:
            temporary_store = StudioStore(Path(temporary))
            record = temporary_store.create("Flow", owner_id="owner_flow")

            def fake_run_agent(*_args, **_kwargs):
                yield {
                    "type": "token",
                    "content": "业务流程推导完成，所有产物已经生成。",
                }

            with (
                patch("app.studio.orchestrator.store", temporary_store),
                patch("app.studio.orchestrator.run_agent", fake_run_agent),
                patch(
                    "app.studio.orchestrator.studio_settings.active_model_name",
                    return_value="test-model",
                ),
            ):
                events = list(BusinessOrchestrator().stream_chat(record, "推导业务流程"))

        self.assertEqual(sum(event["type"] == "task_handoff" for event in events), 3)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(record.runs[-1].status, "failed")
        self.assertFalse(
            any(
                message.role == "assistant"
                and message.kind == "final"
                and "推导完成" in message.content
                for message in record.messages
            )
        )
        self.assertTrue(
            any(message.role == "assistant" and message.kind == "error" for message in record.messages)
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
