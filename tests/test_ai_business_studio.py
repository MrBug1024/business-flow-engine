from __future__ import annotations

import json
import sqlite3
import zipfile
import pytest
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from fastapi.testclient import TestClient

from app.main import app
from app.studio.capability_runtime import discover_capabilities, execute_capability
from app.studio.llm import ModelStreamEvent, ModelToolCall, ThinkingMarkupParser
from app.studio.models import AIRun, ChatMessage
from app.studio.orchestrator import orchestrator
from app.studio.registry import list_skills
from app.studio.settings import studio_settings
from app.studio.storage import StudioStore, store
from app.studio.tool_registry import tool_registry


client = TestClient(app)


class _APITestSandboxBackend(BaseSandbox):
    @property
    def id(self) -> str:
        return "api-test-runtime"

    def execute(self, _command: str, *, timeout: int | None = None) -> ExecuteResponse:
        del timeout
        return ExecuteResponse(output="ok", exit_code=0, truncated=False)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=None) for path, _content in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=path, content=b"", error=None) for path in paths]


@pytest.fixture(autouse=True)
def _use_fake_sandbox(monkeypatch):
    backend = _APITestSandboxBackend()
    monkeypatch.setattr(
        "app.studio.graph_runtime.sandbox_manager.backend_for",
        lambda **_kwargs: backend,
    )
    return backend


def test_ai_business_studio_agent_closed_loop(monkeypatch):
    observed_tools: list[str] = []

    def model_turn(record, messages, requested_model=None, tools=None):
        del record, messages, requested_model
        observed_tools.extend(item["function"]["name"] for item in tools or [])
        yield ModelStreamEvent(kind="reasoning", content="inspect the workspace contract")
        yield ModelStreamEvent(kind="content", content="## Workspace ready\n\nConfiguration is active.")
        yield ModelStreamEvent(kind="completed")

    monkeypatch.setattr("app.studio.agent_runtime.stream_model_turn", model_turn)
    create = client.post(
        "/api/businesses",
        json={
            "name": "电商客服 Agent",
            "goal": "Prepare an AI workspace",
            "description": "Keep files local to this workspace.",
        },
    )
    assert create.status_code == 201, create.text
    business_id = create.json()["id"]

    try:
        description = client.get(f"/api/businesses/{business_id}/description")
        assert description.status_code == 200
        assert description.json()["path"] == "description.md"
        assert description.json()["filename"] == "description.md"
        legacy_description = client.get(f"/api/businesses/{business_id}/scenario-description")
        assert legacy_description.json() == description.json()
        edited = description.json()["content"] + "\n\n## 新增规则\n退款必须保留证据链。\n"
        patched = client.patch(
            f"/api/businesses/{business_id}/description",
            json={"content": edited},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "created"
        assert any("退款必须保留证据链" in item["text"] for item in patched.json()["context"]["user_requirements"])
        assert any(
            item["summary"] == "Updated description.md" and item["trigger"] == "edit_description_markdown"
            for item in patched.json()["context"]["versions"]
        )
        legacy_patched = client.patch(
            f"/api/businesses/{business_id}/scenario-description",
            json={"content": edited},
        )
        assert legacy_patched.status_code == 200

        upload = client.post(
            f"/api/businesses/{business_id}/files",
            files=[
                ("files", ("arbitrary.data", b"opaque workspace data", "application/octet-stream")),
            ],
        )
        assert upload.status_code == 200, upload.text
        assert upload.json()["files"][0]["parse_status"] == "pending"
        assert upload.json()["files"][0]["parser"] == ""

        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/stream",
            json={"message": "请分析当前业务，并用 Markdown 总结。"},
        ) as stream:
            assert stream.status_code == 200
            events = _sse_events("".join(stream.iter_text()))
        event_types = {event["type"] for event in events}
        assert {"run_start", "token", "done"} <= event_types
        assert not {"reasoning", "model_call"} & event_types
        done = next(event for event in events if event["type"] == "done")
        assert "## Workspace ready" in done["assistant_message"]["content"]
        assert done["run"]["events"]
        assert not {"reasoning", "token"} & {
            event["type"] for event in done["run"]["events"]
        }
        assert "request_user_input" in observed_tools
        reloaded = client.get(f"/api/businesses/{business_id}").json()
        assert len(reloaded["context"]["source_files"]) == 1
        assert client.post(f"/api/businesses/{business_id}/outputs/prompt").status_code in {404, 405}
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_legacy_scenario_markdown_is_migrated_without_data_loss(tmp_path):
    local_store = StudioStore(root=tmp_path)
    record = local_store.create(
        name="Legacy workspace",
        goal="Preserve the existing description",
        description="Original metadata description",
    )
    canonical = local_store.description_markdown_path(record.id)
    legacy = local_store.workspace_dir(record.id) / "scenario.md"
    legacy_content = "# Legacy workspace\n\nThis content must survive migration.\n"
    canonical.unlink()
    legacy.write_text(legacy_content, encoding="utf-8")
    record.context.user_requirements[0]["source"] = "scenario.md"
    record.context.versions[0].snapshot["user_requirements"][0]["source"] = "scenario.md"
    metadata = local_store.business_dir(record.id) / "business.json"
    metadata.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    migrated = local_store.require(record.id)

    assert canonical.read_text(encoding="utf-8") == legacy_content
    assert not legacy.exists()
    assert migrated.context.user_requirements[0]["source"] == "description.md"
    assert migrated.context.versions[0].snapshot["user_requirements"][0]["source"] == "description.md"
    root_files = {item.name for item in local_store.workspace_tree(migrated).children}
    assert "description.md" in root_files
    assert "scenario.md" not in root_files


def test_conflicting_legacy_description_is_preserved_as_backup(tmp_path):
    local_store = StudioStore(root=tmp_path)
    record = local_store.create(name="Conflicting workspace", description="Canonical content")
    canonical = local_store.description_markdown_path(record.id)
    canonical_content = canonical.read_text(encoding="utf-8")
    legacy = local_store.workspace_dir(record.id) / "scenario.md"
    legacy.write_text("# Different legacy content\n", encoding="utf-8")

    local_store.workspace_tree(record)

    assert canonical.read_text(encoding="utf-8") == canonical_content
    assert not legacy.exists()
    assert (local_store.workspace_dir(record.id) / "scenario.legacy.md").read_text(encoding="utf-8") == (
        "# Different legacy content\n"
    )


def test_workspace_tree_hides_internal_field_evidence(tmp_path):
    local_store = StudioStore(root=tmp_path)
    record = local_store.create(name="Internal evidence boundary")
    relation_root = local_store.workspace_dir(record.id) / "outputs" / "data-relations"
    internal = relation_root / "_field-evidence"
    internal.mkdir(parents=True)
    (internal / "relations.mmd").write_text("flowchart LR\n  field_a --> field_b\n", encoding="utf-8")
    (relation_root / "relations.mmd").write_text("flowchart TB\n  data --> result\n", encoding="utf-8")

    tree = local_store.workspace_tree(record)

    def paths(node):
        result = {node.path}
        for child in node.children:
            result.update(paths(child))
        return result

    tree_paths = paths(tree)
    assert "outputs/data-relations/relations.mmd" in tree_paths
    assert "outputs/data-relations/_field-evidence" not in tree_paths
    assert "outputs/data-relations/_field-evidence/relations.mmd" not in tree_paths


def test_workspace_files_in_any_directory_have_bounded_previews():
    openpyxl = pytest.importorskip("openpyxl")
    created = client.post("/api/businesses", json={"name": "Workspace preview"}).json()
    business_id = created["id"]
    try:
        preview_root = store.workspace_dir(business_id) / "outputs" / "nested"
        preview_root.mkdir(parents=True)
        (preview_root / "flow.mmd").write_text("flowchart LR\n  A --> B\n", encoding="utf-8")
        (preview_root / "report.md").write_text("# Report\n\nEvidence is ready.\n", encoding="utf-8")
        (preview_root / "rows.csv").write_text("name,status\nalpha,ready\nbeta,done\n", encoding="utf-8")
        (preview_root / "document.pdf").write_bytes(b"%PDF-1.4\n% bounded fixture\n")
        (preview_root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (preview_root / "large.log").write_text("x" * (2 * 1024 * 1024 + 32), encoding="utf-8")
        with zipfile.ZipFile(preview_root / "notes.docx", "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="urn:test"><w:body><w:p><w:r><w:t>Previewed Word content</w:t></w:r></w:p></w:body></w:document>',
            )
        with zipfile.ZipFile(preview_root / "slides.pptx", "w") as archive:
            archive.writestr(
                "ppt/slides/slide1.xml",
                '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><a:t>Previewed slide content</a:t></p:cSld></p:sld>',
            )
        with zipfile.ZipFile(preview_root / "bundle.zip", "w") as archive:
            archive.writestr("reports/result.json", "{}")
        database = sqlite3.connect(preview_root / "evidence.sqlite3")
        try:
            database.execute("CREATE TABLE evidence(id TEXT, status TEXT)")
            database.execute("INSERT INTO evidence VALUES ('E-1', 'ready')")
            database.commit()
        finally:
            database.close()
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.title = "Results"
        worksheet.append(["Audit results", None])
        worksheet.merge_cells("A1:B1")
        worksheet.append(["rule", "outcome"])
        worksheet.append(["duplicate charge", "matched"])
        workbook.save(preview_root / "results.xlsx")

        mermaid = client.get(
            f"/api/businesses/{business_id}/workspace/preview",
            params={"path": "outputs/nested/flow.mmd"},
        )
        assert mermaid.status_code == 200, mermaid.text
        assert mermaid.json()["kind"] == "mermaid"
        assert "flowchart LR" in mermaid.json()["text"]
        assert mermaid.json()["path"] == "outputs/nested/flow.mmd"

        markdown = client.get(
            f"/api/businesses/{business_id}/workspace/preview",
            params={"path": "outputs/nested/report.md"},
        ).json()
        assert markdown["kind"] == "markdown"
        assert markdown["raw_url"].startswith(f"/api/businesses/{business_id}/workspace/raw")

        table = client.get(
            f"/api/businesses/{business_id}/workspace/preview",
            params={"path": "outputs/nested/rows.csv"},
        ).json()
        assert table["kind"] == "table"
        assert table["columns"] == ["name", "status"]
        assert table["sample_rows"][1] == {"name": "beta", "status": "done"}

        spreadsheet = client.get(
            f"/api/businesses/{business_id}/workspace/preview",
            params={"path": "outputs/nested/results.xlsx"},
        ).json()
        assert spreadsheet["kind"] == "table"
        assert spreadsheet["sheets"][0]["name"] == "Results"
        assert spreadsheet["sheets"][0]["header_row"] == 2
        assert spreadsheet["columns"] == ["rule", "outcome"]
        assert spreadsheet["sample_rows"][0]["outcome"] == "matched"

        pdf = client.get(
            f"/api/businesses/{business_id}/workspace/preview",
            params={"path": "outputs/nested/document.pdf"},
        ).json()
        assert pdf["kind"] == "pdf"

        expected_previews = {
            "outputs/nested/image.png": ("image", ""),
            "outputs/nested/notes.docx": ("document", "Previewed Word content"),
            "outputs/nested/slides.pptx": ("document", "Previewed slide content"),
            "outputs/nested/bundle.zip": ("archive", "reports/result.json"),
            "outputs/nested/evidence.sqlite3": ("database", "ready"),
        }
        for path, (kind, expected_text) in expected_previews.items():
            response = client.get(
                f"/api/businesses/{business_id}/workspace/preview",
                params={"path": path},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["kind"] == kind
            assert expected_text in json.dumps(body, ensure_ascii=False)

        large_text = client.get(
            f"/api/businesses/{business_id}/workspace/preview",
            params={"path": "outputs/nested/large.log"},
        ).json()
        assert large_text["kind"] == "text"
        assert large_text["truncated"] is True
        assert len(large_text["text"].encode("utf-8")) == 2 * 1024 * 1024

        inline = client.get(
            f"/api/businesses/{business_id}/workspace/raw",
            params={"path": "outputs/nested/flow.mmd"},
        )
        assert inline.status_code == 200
        assert inline.headers["content-disposition"].startswith("inline;")
        download = client.get(
            f"/api/businesses/{business_id}/workspace/raw",
            params={"path": "outputs/nested/flow.mmd", "download": "true"},
        )
        assert download.headers["content-disposition"].startswith("attachment;")

        traversal = client.get(
            f"/api/businesses/{business_id}/workspace/preview",
            params={"path": "../business.json"},
        )
        assert traversal.status_code == 400
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_workspace_files_in_any_directory_can_be_deleted_without_regeneration():
    created = client.post("/api/businesses", json={"name": "Workspace deletion"}).json()
    business_id = created["id"]
    try:
        nested = store.workspace_dir(business_id) / "outputs" / "nested"
        nested.mkdir(parents=True)
        (nested / "result.json").write_text('{"status":"ready"}', encoding="utf-8")
        upload = client.post(
            f"/api/businesses/{business_id}/files",
            files=[("files", ("source.csv", b"id,status\n1,ready\n", "text/csv"))],
        )
        assert upload.status_code == 200

        arbitrary = client.delete(
            f"/api/businesses/{business_id}/workspace/file",
            params={"path": "outputs/nested/result.json"},
        )
        assert arbitrary.status_code == 200, arbitrary.text
        assert arbitrary.json()["deleted"]["path"] == "outputs/nested/result.json"
        assert not (nested / "result.json").exists()

        registered = client.delete(
            f"/api/businesses/{business_id}/workspace/file",
            params={"path": "data/source.csv"},
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["business"]["files"] == []

        for path in ("context/business_context.json", "graphs/flow.mmd", "description.md"):
            response = client.delete(
                f"/api/businesses/{business_id}/workspace/file",
                params={"path": path},
            )
            assert response.status_code == 200, response.text

        reloaded = client.get(f"/api/businesses/{business_id}")
        assert reloaded.status_code == 200
        body = reloaded.json()
        assert {
            "context/business_context.json", "graphs/flow.mmd", "description.md",
        } <= set(body["workspace_deleted_paths"])
        assert not store.description_markdown_path(business_id).exists()
        assert not (store.context_dir(business_id) / "business_context.json").exists()
        assert not (store.graphs_dir(business_id) / "flow.mmd").exists()

        tree = client.get(f"/api/businesses/{business_id}/workspace/tree").json()

        def paths(node):
            result = {node["path"]}
            for child in node.get("children", []):
                result.update(paths(child))
            return result

        tree_paths = paths(tree)
        assert "description.md" not in tree_paths
        assert "context/business_context.json" not in tree_paths
        assert "graphs/flow.mmd" not in tree_paths
        traversal = client.delete(
            f"/api/businesses/{business_id}/workspace/file",
            params={"path": "../business.json"},
        )
        assert traversal.status_code == 400
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_long_agent_task_automatically_continues_in_a_fresh_run(monkeypatch):
    observed_segments: list[tuple[int, str, bool]] = []

    def segmented_agent(record, run, *, user_prompt=None, include_history=True, **_kwargs):
        del record
        observed_segments.append((run.segment_index, str(user_prompt or ""), include_history))
        if run.segment_index == 1:
            raise RuntimeError("maximum context length exceeded")
        if run.segment_index == 2:
            run.task_progress = {
                "task_id": run.task_id,
                "status": "continuing",
                "objective": "推导数据关系",
                "summary": "证据检查点已保存",
                "work_items": [
                    {"id": "prepare", "title": "准备证据", "status": "completed"},
                    {"id": "synthesize", "title": "综合关系", "status": "pending"},
                ],
                "artifacts": ["/workspace/outputs/data-relations/synthesis-brief.json"],
            }
            yield {"type": "agent_progress", "action": "compact", **run.task_progress}
            yield {"type": "token", "content": "我已经定位问题，下一步准备修复。"}
            return
        assert run.task_progress["status"] == "running"
        run.task_progress = {
            **run.task_progress,
            "status": "completed",
            "summary": "关系图谱已通过校验",
            "work_items": [
                {"id": "prepare", "title": "准备证据", "status": "completed"},
                {"id": "synthesize", "title": "综合关系", "status": "completed"},
            ],
        }
        yield {"type": "agent_progress", "action": "complete", **run.task_progress}
        yield {
            "type": "token",
            "content": "任务已从工作区检查点继续并完成。",
        }

    monkeypatch.setattr("app.studio.orchestrator.run_agent", segmented_agent)
    created = client.post("/api/businesses", json={"name": "Segmented task"}).json()
    business_id = created["id"]
    session_id = created["chat_sessions"][0]["id"]
    try:
        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/stream",
            json={"message": "推导数据关系", "session_id": session_id},
        ) as stream:
            assert stream.status_code == 200
            events = _sse_events("".join(stream.iter_text()))

        assert not any(item["type"] == "error" for item in events)
        handoffs = [item for item in events if item["type"] == "task_handoff"]
        done = next(item for item in events if item["type"] == "done")
        assert [item["segment_index"] for item in handoffs] == [2, 3]
        assert done["run"]["segment_index"] == 3
        assert done["run"]["continued_from_run_id"] == handoffs[-1]["from_run_id"]
        assert done["assistant_message"]["content"] == "任务已从工作区检查点继续并完成。"
        assert observed_segments[0] == (1, "推导数据关系", True)
        assert observed_segments[1][0] == 2
        assert "多阶段接力的目的只是避免上下文过长" in observed_segments[1][1]
        assert "从第一个未完成的工作项继续" in observed_segments[1][1]
        assert observed_segments[1][2] is False
        assert observed_segments[2][0] == 3
        assert observed_segments[2][2] is False

        reloaded = store.require(business_id)
        task_runs = [item for item in reloaded.runs if item.task_id == done["run"]["task_id"]]
        assert len(task_runs) == 3
        assert task_runs[0].status == "succeeded"
        assert "fresh Agent run" in task_runs[0].summary
        assistant_messages = [item for item in reloaded.messages if item.role == "assistant"]
        assert [item.kind for item in assistant_messages] == ["progress", "final"]
        assert assistant_messages[0].progress_action == "compact"
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_model_call_safety_limit_does_not_create_more_task_segments(monkeypatch):
    observed_segments: list[int] = []

    def limited_agent(record, run, **_kwargs):
        del record
        observed_segments.append(run.segment_index)
        raise RuntimeError("Model call limits exceeded: run limit (64/64)")
        yield  # pragma: no cover

    monkeypatch.setattr("app.studio.orchestrator.run_agent", limited_agent)
    created = client.post("/api/businesses", json={"name": "Bounded task"}).json()
    business_id = created["id"]
    session_id = created["chat_sessions"][0]["id"]
    try:
        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/stream",
            json={"message": "推导数据关系", "session_id": session_id},
        ) as stream:
            events = _sse_events("".join(stream.iter_text()))

        assert observed_segments == [1]
        assert not any(item["type"] == "task_handoff" for item in events)
        error = next(item for item in events if item["type"] == "error")
        assert "Model call limits exceeded" in error["message"]
        assert error["assistant_message"]["kind"] == "error"
        assert "未达到最终验收标准" in error["assistant_message"]["content"]
        reloaded = store.require(business_id)
        assert [item.kind for item in reloaded.messages if item.role == "assistant"] == ["error"]
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_capability_discovery_is_registry_and_skill_markdown_driven():
    created = client.post("/api/businesses", json={"name": "能力发现测试"}).json()
    try:
        record = store.require(created["id"])
        capabilities = discover_capabilities(record)
        tool_names = {item.display_name for item in capabilities if item.kind == "tool"}
        assert tool_names == {item.name for item in tool_registry.get_tools()}
        assert "request_user_input" in tool_names
        assert all(item.kind in {"tool", "mcp"} for item in capabilities)
        assert not any(item.function_name.startswith("skill__") for item in capabilities)
        assert {item.name for item in list_skills()}
        configured_mcp = {
            str(item.get("name"))
            for item in studio_settings.load().mcp_configs
                if item.get("enabled")
                and (item.get("config") or {}).get("transport") in {"streamable_http", "stdio"}
                and (item.get("config") or {}).get("tools_discovered") is True
                and (item.get("config") or {}).get("tools")
            }
        discovered_mcp = {
            str(item.config.get("_server_name") or item.display_name)
            for item in capabilities
            if item.kind == "mcp"
        }
        assert discovered_mcp == configured_mcp
    finally:
        client.delete(f"/api/businesses/{created['id']}")


def test_user_input_tool_is_directory_discovered_and_protocol_driven():
    created = client.post("/api/businesses", json={"name": "动态问答测试"}).json()
    try:
        record = store.require(created["id"])
        capability = next(
            item for item in discover_capabilities(record)
            if item.function_name == "request_user_input"
        )
        assert capability.protocol == "user_input"
        assert capability.source == "request_user_input.py"
        result = execute_capability(
            capability,
            record,
            {
                "question": "退款证据应保留多久？",
                "reason": "当前资料没有规定证据留存期限。",
                "category": "证据留存",
                "options": [
                    {"label": "三年", "description": "覆盖常规审计周期", "recommended": True},
                    {"label": "五年", "description": "覆盖长期追溯", "recommended": True},
                ],
            },
        )
        assert [item["label"] for item in result.output["options"]] == ["三年", "五年"]
        assert result.emitted_events == []

        open_ended = execute_capability(
            capability,
            record,
            {
                "question": "请描述特殊退款审批流程。",
                "reason": "这是开放式流程说明。",
            },
        )
        assert open_ended.output["options"] == []
    finally:
        client.delete(f"/api/businesses/{created['id']}")


def test_task_progress_merges_business_steps_instead_of_appending_call_logs():
    created = client.post("/api/businesses", json={"name": "Semantic progress"}).json()
    business_id = created["id"]
    try:
        record = store.require(business_id)
        session_id = record.chat_sessions[0].id
        run = AIRun(
            id="run-progress-test",
            business_id=business_id,
            session_id=session_id,
            task_id="task-progress-test",
            started_at=1,
        )
        record.runs.append(run)
        capability = next(
            item for item in discover_capabilities(record)
            if item.function_name == "report_task_progress"
        )

        plan = execute_capability(
            capability,
            record,
            {
                "action": "plan",
                "objective": "推导整个业务场景的数据关系",
                "work_items": [
                    {
                        "id": "prepare-evidence",
                        "title": "提取并压缩关系证据",
                        "status": "running",
                        "why": "先建立有界、可追溯的证据基础",
                        "expected": "形成证据卡和综合简报",
                    },
                    {
                        "id": "verify-deliver",
                        "title": "校验并交付关系产物",
                        "status": "pending",
                        "expected": "生成 mmd、md 和 json",
                    },
                ],
                "acceptance_criteria": ["最终状态为 complete"],
                "message": "我会先形成证据简报，再综合并验收宏观关系图。",
            },
            run_id=run.id,
            session_id=session_id,
        )
        update = execute_capability(
            capability,
            record,
            {
                "action": "update",
                "work_item_id": "prepare-evidence",
                "title": "提取并压缩关系证据",
                "result": "已形成 24 张核心证据卡",
                "verification": "prepare-status 为 ready_for_synthesis",
                "next_step": "综合主链与分支",
                "artifacts": ["/workspace/outputs/data-relations/synthesis-brief.json"],
                "message": "5 份材料的证据简报已完成，接下来综合宏观数据关系。",
            },
            run_id=run.id,
            session_id=session_id,
        )

        assert plan.output["work_item_count"] == 2
        assert update.output["revision"] == 2
        assert run.plan == ["提取并压缩关系证据", "校验并交付关系产物"]
        assert run.task_progress["objective"] == "推导整个业务场景的数据关系"
        assert run.task_progress["work_items"][0] == {
            "id": "prepare-evidence",
            "title": "提取并压缩关系证据",
            "status": "completed",
            "why": "先建立有界、可追溯的证据基础",
            "expected": "形成证据卡和综合简报",
            "result": "已形成 24 张核心证据卡",
            "verification": "prepare-status 为 ready_for_synthesis",
        }
        progress_event = next(
            event for event in update.emitted_events if event["type"] == "agent_progress"
        )
        assert len(progress_event["work_items"]) == 2
        assert progress_event["artifacts"] == [
            "/workspace/outputs/data-relations/synthesis-brief.json"
        ]
        assert progress_event["message"] == "5 份材料的证据简报已完成，接下来综合宏观数据关系。"
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_semantic_progress_becomes_separate_durable_ai_messages(monkeypatch):
    def staged_agent(record, run, **_kwargs):
        del record
        plan = {
            "task_id": run.task_id,
            "status": "planned",
            "objective": "推导宏观数据关系",
            "work_items": [
                {"id": "evidence", "title": "形成证据", "status": "running"},
                {"id": "synthesis", "title": "综合关系", "status": "pending"},
            ],
        }
        run.task_progress = plan
        yield {
            "type": "agent_progress",
            "action": "plan",
            "message": "我会先盘点材料形成证据，再综合并校验宏观关系。",
            **plan,
        }
        yield {
            "type": "sandbox_command",
            "call_id": "scan-1",
            "name": "证据扫描",
            "status": "succeeded",
            "summary": "5 files covered",
        }
        update = {
            **plan,
            "status": "running",
            "summary": "5 份材料已形成有界证据",
            "next_step": "综合宏观关系",
            "revision": 2,
            "work_items": [
                {"id": "evidence", "title": "形成证据", "status": "completed"},
                {"id": "synthesis", "title": "综合关系", "status": "running"},
            ],
        }
        run.task_progress = update
        yield {
            "type": "agent_progress",
            "action": "update",
            "work_item_id": "evidence",
            "message": "已覆盖 5 份材料；字段联系只保留为证据，下一步综合宏观关系。",
            **update,
        }
        run.task_progress = {**update, "status": "completed", "revision": 3}
        yield {
            "type": "agent_progress",
            "action": "complete",
            "message": "宏观关系图已通过验收。",
            **run.task_progress,
        }
        yield {"type": "token", "content": "关系图、说明和 JSON 已生成。"}

    monkeypatch.setattr("app.studio.orchestrator.run_agent", staged_agent)
    created = client.post("/api/businesses", json={"name": "Multi-message task"}).json()
    business_id = created["id"]
    session_id = created["chat_sessions"][0]["id"]
    try:
        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/stream",
            json={"message": "推导数据关系", "session_id": session_id},
        ) as stream:
            events = _sse_events("".join(stream.iter_text()))

        progress_events = [item for item in events if item["type"] == "progress_message"]
        assert [item["message"]["progress_action"] for item in progress_events] == [
            "plan", "update",
        ]
        reloaded = store.require(business_id)
        messages = [item for item in reloaded.messages if item.role == "assistant"]
        assert [item.kind for item in messages] == ["progress", "progress", "final"]
        assert messages[0].content.startswith("我会先盘点材料")
        assert not any(item["type"] == "sandbox_command" for item in messages[0].activity_events)
        assert any(item["type"] == "sandbox_command" for item in messages[1].activity_events)
        assert messages[2].content == "关系图、说明和 JSON 已生成。"
        assert messages[2].progress["status"] == "completed"
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_thinking_markup_is_streamed_as_a_separate_channel():
    parser = ThinkingMarkupParser()
    parts = list(parser.feed("<thi"))
    parts.extend(parser.feed("nk>检查资料</think>最终回答"))
    parts.extend(parser.flush())
    assert ("reasoning", "检查资料") in parts
    assert "".join(text for kind, text in parts if kind == "content") == "最终回答"


def test_chat_sessions_are_persistent_isolated_and_context_safe(monkeypatch):
    histories: list[list[str]] = []

    def fake_session_turn(record, messages, requested_model=None, tools=None):
        del record, requested_model, tools
        histories.append([str(item.get("content") or "") for item in messages if item.get("role") != "system"])
        yield ModelStreamEvent(kind="content", content="session reply")
        yield ModelStreamEvent(kind="completed")

    monkeypatch.setattr("app.studio.agent_runtime.stream_model_turn", fake_session_turn)
    created = client.post("/api/businesses", json={"name": "Session isolation"}).json()
    business_id = created["id"]

    try:
        initial = client.get(f"/api/businesses/{business_id}/chat/sessions")
        assert initial.status_code == 200
        assert len(initial.json()) == 1
        first_session_id = initial.json()[0]["id"]

        second = client.post(
            f"/api/businesses/{business_id}/chat/sessions",
            json={"title": "Second thread"},
        )
        assert second.status_code == 201
        second_session_id = second.json()["chat_sessions"][-1]["id"]

        first_turn = client.post(
            f"/api/businesses/{business_id}/chat",
            json={"message": "alpha only", "session_id": first_session_id},
        )
        assert first_turn.status_code == 200, first_turn.text
        assert first_turn.json()["assistant_message"]["session_id"] == first_session_id
        assert first_turn.json()["run"]["session_id"] == first_session_id

        second_turn = client.post(
            f"/api/businesses/{business_id}/chat",
            json={"message": "beta only", "session_id": second_session_id},
        )
        assert second_turn.status_code == 200, second_turn.text

        third_turn = client.post(
            f"/api/businesses/{business_id}/chat",
            json={"message": "alpha follow-up", "session_id": first_session_id},
        )
        assert third_turn.status_code == 200, third_turn.text

        assert "alpha only" in histories[0]
        assert "beta only" not in histories[0]
        assert "beta only" in histories[1]
        assert "alpha only" not in histories[1]
        assert "alpha only" in histories[2]
        assert "alpha follow-up" in histories[2]
        assert "beta only" not in histories[2]

        record = store.require(business_id)
        record.context.rules.append({"id": "rule_keep", "statement": "keep context"})
        store.save(record)
        before_clear = client.get(f"/api/businesses/{business_id}").json()

        cleared = client.delete(
            f"/api/businesses/{business_id}/chat/sessions/{first_session_id}/messages"
        )
        assert cleared.status_code == 200, cleared.text
        after_clear = client.get(f"/api/businesses/{business_id}").json()
        assert after_clear["context"] == before_clear["context"]
        assert after_clear["files"] == before_clear["files"]
        assert not [item for item in after_clear["messages"] if item["session_id"] == first_session_id]
        assert not [item for item in after_clear["runs"] if item["session_id"] == first_session_id]
        assert [item for item in after_clear["messages"] if item["session_id"] == second_session_id]

        deleted = client.delete(
            f"/api/businesses/{business_id}/chat/sessions/{second_session_id}"
        )
        assert deleted.status_code == 200, deleted.text
        after_delete = client.get(f"/api/businesses/{business_id}").json()
        assert second_session_id not in {item["id"] for item in after_delete["chat_sessions"]}
        assert not [item for item in after_delete["messages"] if item["session_id"] == second_session_id]
        assert after_delete["context"] == before_clear["context"]

        replaced_last = client.delete(
            f"/api/businesses/{business_id}/chat/sessions/{first_session_id}"
        )
        assert replaced_last.status_code == 200, replaced_last.text
        remaining = replaced_last.json()["chat_sessions"]
        assert len(remaining) == 1
        assert remaining[0]["id"] != first_session_id

        missing = client.post(
            f"/api/businesses/{business_id}/chat",
            json={"message": "invalid", "session_id": second_session_id},
        )
        assert missing.status_code == 404
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_legacy_chat_history_migrates_to_one_default_session(tmp_path):
    local_store = StudioStore(root=tmp_path)
    record = local_store.create("Legacy chat")
    chat_run_id = "run_legacy_chat"
    background_run_id = "run_legacy_analysis"
    record.messages = [
        ChatMessage(id="msg_user", role="user", content="legacy question", created_at=10),
        ChatMessage(
            id="msg_assistant",
            role="assistant",
            content="legacy answer",
            created_at=11,
            run_id=chat_run_id,
        ),
    ]
    record.runs = [
        AIRun(id=chat_run_id, business_id=record.id, started_at=10.5, finished_at=11),
        AIRun(id=background_run_id, business_id=record.id, started_at=9, finished_at=9.5),
    ]
    payload = record.model_dump(mode="json")
    payload.pop("chat_sessions", None)
    for message in payload["messages"]:
        message.pop("session_id", None)
    for run in payload["runs"]:
        run.pop("session_id", None)
    local_store._meta_file(record.id).write_text(  # noqa: SLF001 - explicit legacy fixture
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    migrated = local_store.require(record.id)
    assert len(migrated.chat_sessions) == 1
    session_id = migrated.chat_sessions[0].id
    assert migrated.chat_sessions[0].title == "legacy question"
    assert {item.session_id for item in migrated.messages} == {session_id}
    migrated_runs = {item.id: item for item in migrated.runs}
    assert migrated_runs[chat_run_id].session_id == session_id
    assert migrated_runs[background_run_id].session_id is None
    assert local_store.description_markdown_path(record.id).name == "description.md"

    reloaded = local_store.require(record.id)
    assert reloaded.chat_sessions[0].id == session_id
    assert {item.session_id for item in reloaded.messages} == {session_id}


def test_waiting_run_resumes_without_a_synthetic_user_message(monkeypatch):
    resume_prompts: list[str] = []

    def fake_waiting_turn(record, messages, requested_model=None, tools=None):
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        prompt = next(
            (str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        if prompt == "need confirmation" and not tool_messages:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call_question",
                        name="request_user_input",
                        arguments={
                            "question": "Which retention period should be used?",
                            "reason": "The source does not define one.",
                            "options": [
                                {
                                    "label": "Three years",
                                    "description": "Use the standard audit period.",
                                    "recommended": True,
                                }
                            ],
                        },
                    ),
                    ModelToolCall(
                        id="call_must_not_run",
                        name="set_plan",
                        arguments={"items": ["This tool must be paused"]},
                    ),
                ],
            )
            return
        resume_prompts.append(
            "\n".join(str(item.get("content") or "") for item in tool_messages)
            if prompt == "need confirmation"
            else prompt
        )
        yield ModelStreamEvent(kind="content", content="continued from confirmations")
        yield ModelStreamEvent(kind="completed")

    monkeypatch.setattr("app.studio.agent_runtime.stream_model_turn", fake_waiting_turn)
    created = client.post("/api/businesses", json={"name": "Resume flow"}).json()
    business_id = created["id"]
    session_id = created["chat_sessions"][0]["id"]

    try:
        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/stream",
            json={"message": "need confirmation", "session_id": session_id},
        ) as response:
            assert response.status_code == 200
            waiting_events = _sse_events("".join(response.iter_text()))

        waiting_done = next(item for item in waiting_events if item["type"] == "done")
        waiting_run_id = waiting_done["run"]["id"]
        assert waiting_done["run"]["status"] == "waiting_for_user"
        assert waiting_done["run"]["finished_at"] is None
        assert "waiting_for_user" in {item["type"] for item in waiting_events}
        assert not any(item.get("call_id") == "call_must_not_run" for item in waiting_events)

        question_event = next(item for item in waiting_events if item["type"] == "question")
        first_question = question_event["question"]
        assert first_question["run_id"] == waiting_run_id
        assert first_question["session_id"] == session_id

        other_session_record = client.post(
            f"/api/businesses/{business_id}/chat/sessions",
            json={"title": "Other session"},
        ).json()
        other_session_id = other_session_record["chat_sessions"][-1]["id"]
        mismatched_confirmation = client.post(
            f"/api/businesses/{business_id}/confirmations",
            json={
                "question_id": first_question["id"],
                "session_id": other_session_id,
                "answer": "Wrong session answer",
            },
        )
        assert mismatched_confirmation.status_code == 409

        record = store.require(business_id)
        record.context.questions.append(
            {
                "id": "q_second_waiting",
                "question": "Should archived records remain searchable?",
                "reason": "The archive behavior is unspecified.",
                "options": [],
                "status": "open",
                "run_id": waiting_run_id,
                "session_id": session_id,
                "source": "agent",
            }
        )
        store.save(record)

        first_answer = client.post(
            f"/api/businesses/{business_id}/confirmations",
            json={
                "question_id": first_question["id"],
                "session_id": session_id,
                "answer": "Three years",
            },
        )
        assert first_answer.status_code == 200
        assert first_answer.json()["resume"]["ready"] is False

        blocked = client.post(
            f"/api/businesses/{business_id}/chat/sessions/{session_id}/resume/stream",
            json={"run_id": waiting_run_id},
        )
        assert blocked.status_code == 409

        second_answer = client.post(
            f"/api/businesses/{business_id}/confirmations",
            json={
                "question_id": "q_second_waiting",
                "session_id": session_id,
                "answer": "Yes, keep them searchable",
            },
        )
        assert second_answer.status_code == 200
        assert second_answer.json()["resume"]["ready"] is True

        user_messages_before = [
            item for item in store.require(business_id).messages
            if item.session_id == session_id and item.role == "user"
        ]
        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/sessions/{session_id}/resume/stream",
            json={"run_id": waiting_run_id},
        ) as response:
            assert response.status_code == 200
            resumed_events = _sse_events("".join(response.iter_text()))

        resumed_done = next(item for item in resumed_events if item["type"] == "done")
        continuation_run_id = resumed_done["run"]["id"]
        assert resumed_done["run"]["status"] == "succeeded"
        assert resumed_done["run"]["resumed_from_run_id"] == waiting_run_id
        assert "Three years" in resume_prompts[-1]
        assert "keep them searchable" in resume_prompts[-1]

        after_resume = store.require(business_id)
        original_run = next(item for item in after_resume.runs if item.id == waiting_run_id)
        assert original_run.status == "succeeded"
        consumed = [item for item in after_resume.context.questions if item.get("run_id") == waiting_run_id]
        assert {item.get("continuation_run_id") for item in consumed} == {continuation_run_id}
        user_messages_after = [
            item for item in after_resume.messages
            if item.session_id == session_id and item.role == "user"
        ]
        assert [item.id for item in user_messages_after] == [item.id for item in user_messages_before]
        assert all("用户已经完成待确认问题" not in item.content for item in user_messages_after)

        after_resume.context.questions.append(
            {
                "id": "q_review_only",
                "question": "Use the reviewed threshold?",
                "status": "open",
                "source": "agent",
            }
        )
        store.save(after_resume)
        review_answer = client.post(
            f"/api/businesses/{business_id}/confirmations",
            json={
                "question_id": "q_review_only",
                "session_id": session_id,
                "answer": "Use the reviewed threshold",
            },
        )
        assert review_answer.status_code == 200
        assert review_answer.json()["resume"]["ready"] is True

        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/sessions/{session_id}/resume/stream",
            json={},
        ) as response:
            assert response.status_code == 200
            review_events = _sse_events("".join(response.iter_text()))
        review_done = next(item for item in review_events if item["type"] == "done")
        assert review_done["run"]["resumed_from_run_id"] is None
        assert "reviewed threshold" in resume_prompts[-1]

        consumed_again = client.post(
            f"/api/businesses/{business_id}/chat/sessions/{session_id}/resume/stream",
            json={},
        )
        assert consumed_again.status_code == 409
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_failed_continuation_keeps_source_waiting_and_can_retry(monkeypatch):
    continuation_attempts = 0

    def fake_retry_turn(record, messages, requested_model=None, tools=None):
        nonlocal continuation_attempts
        del record, requested_model, tools
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        prompt = next(
            (str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        if prompt == "wait then retry" and not tool_messages:
            yield ModelStreamEvent(
                kind="completed",
                tool_calls=[
                    ModelToolCall(
                        id="call_retry_question",
                        name="request_user_input",
                        arguments={
                            "question": "Proceed with the retry?",
                            "reason": "Confirmation is required.",
                        },
                    )
                ],
            )
            return
        continuation_attempts += 1
        if continuation_attempts <= 3:
            raise RuntimeError("continuation failed")
        yield ModelStreamEvent(kind="content", content="retry succeeded")
        yield ModelStreamEvent(kind="completed")

    monkeypatch.setattr("app.studio.agent_runtime.stream_model_turn", fake_retry_turn)
    created = client.post("/api/businesses", json={"name": "Retry continuation"}).json()
    business_id = created["id"]
    session_id = created["chat_sessions"][0]["id"]

    try:
        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/stream",
            json={"message": "wait then retry", "session_id": session_id},
        ) as response:
            waiting_events = _sse_events("".join(response.iter_text()))
        waiting_done = next(item for item in waiting_events if item["type"] == "done")
        source_run_id = waiting_done["run"]["id"]
        question = next(item["question"] for item in waiting_events if item["type"] == "question")

        answered = client.post(
            f"/api/businesses/{business_id}/confirmations",
            json={
                "question_id": question["id"],
                "session_id": session_id,
                "answer": "Proceed",
            },
        )
        assert answered.status_code == 200

        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/sessions/{session_id}/resume/stream",
            json={"run_id": source_run_id},
        ) as response:
            failed_events = _sse_events("".join(response.iter_text()))
        assert "error" in {item["type"] for item in failed_events}

        after_failure = store.require(business_id)
        source_after_failure = next(item for item in after_failure.runs if item.id == source_run_id)
        question_after_failure = next(item for item in after_failure.context.questions if item.get("id") == question["id"])
        assert source_after_failure.status == "waiting_for_user"
        assert question_after_failure.get("continuation_run_id") is None
        assert question_after_failure.get("continued_at") is None

        with client.stream(
            "POST",
            f"/api/businesses/{business_id}/chat/sessions/{session_id}/resume/stream",
            json={"run_id": source_run_id},
        ) as response:
            assert response.status_code == 200
            retry_events = _sse_events("".join(response.iter_text()))
        retry_done = next(item for item in retry_events if item["type"] == "done")
        assert retry_done["run"]["status"] == "succeeded"

        after_retry = store.require(business_id)
        source_after_retry = next(item for item in after_retry.runs if item.id == source_run_id)
        question_after_retry = next(item for item in after_retry.context.questions if item.get("id") == question["id"])
        assert source_after_retry.status == "succeeded"
        assert question_after_retry.get("continuation_run_id") == retry_done["run"]["id"]
    finally:
        client.delete(f"/api/businesses/{business_id}")


def test_closing_continuation_stream_marks_child_failed_and_keeps_retryable_source():
    created = client.post("/api/businesses", json={"name": "Cancelled continuation"}).json()
    business_id = created["id"]
    session_id = created["chat_sessions"][0]["id"]

    try:
        record = store.require(business_id)
        source_run = AIRun(
            id="run_waiting_cancel_test",
            business_id=business_id,
            session_id=session_id,
            status="waiting_for_user",
            started_at=record.created_at,
            summary="Waiting for user confirmation.",
        )
        store.append_run(record, source_run)
        record.context.questions.append(
            {
                "id": "q_cancel_test",
                "question": "Continue after confirmation?",
                "answer": "Continue",
                "status": "answered",
                "run_id": source_run.id,
                "session_id": session_id,
                "source": "agent",
            }
        )
        store.save(record)

        preparation = orchestrator.prepare_resume(record, session_id, run_id=source_run.id)
        continuation = orchestrator.stream_resume(record, preparation)
        run_start = next(continuation)
        child_run_id = run_start["run"]["id"]
        assert run_start["type"] == "run_start"
        assert run_start["resume"]["from_run_id"] == source_run.id

        continuation.close()

        reloaded = store.require(business_id)
        child_run = next(item for item in reloaded.runs if item.id == child_run_id)
        original_run = next(item for item in reloaded.runs if item.id == source_run.id)
        question = next(item for item in reloaded.context.questions if item.get("id") == "q_cancel_test")
        assert child_run.status == "failed"
        assert child_run.finished_at is not None
        assert "disconnected" in child_run.error
        assert original_run.status == "waiting_for_user"
        assert question.get("continued_at") is None
        assert question.get("continuation_run_id") is None

        retry = orchestrator.prepare_resume(reloaded, session_id, run_id=source_run.id)
        assert retry.source_run_id == source_run.id
        assert retry.question_ids == ("q_cancel_test",)
    finally:
        client.delete(f"/api/businesses/{business_id}")


def _fake_model_turn(record, messages, requested_model=None, tools=None):
    del record, requested_model, tools
    prompt = next((str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"), "")
    called = [
        call["function"]["name"]
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]
    yield ModelStreamEvent(kind="reasoning", content="检查当前上下文并选择最小必要能力。")

    if "工作区资料刚刚发生变化" in prompt:
        if "set_plan" not in called:
            yield _tool_call("set_plan", {"items": ["读取工作区资料", "更新业务上下文"]}, len(called))
            return
        if "list_workspace_files" not in called:
            yield _tool_call("list_workspace_files", {}, len(called))
            return
        if "update_business_context" not in called:
            yield _tool_call(
                "update_business_context",
                {
                    "summary": "根据业务描述和已解析资料更新上下文",
                    "patch": {
                        "entities": [
                            {"name": "订单", "type": "business_entity", "attributes": ["order_id", "status", "amount"]},
                            {"name": "客户", "type": "business_entity", "attributes": ["customer_name"]},
                        ],
                        "relations": [{"source": "客户", "target": "订单", "type": "owns"}],
                        "rules": [{"statement": "退款必须保留证据链", "source": "description.md", "confidence": 0.95}],
                        "evidence": [{"claim": "订单资料包含状态和金额", "source": "orders.csv", "confidence": 0.9}],
                        "questions": [],
                    },
                },
                len(called),
            )
            return
        yield ModelStreamEvent(kind="content", content="工作区上下文已同步。")
        yield ModelStreamEvent(kind="completed")
        return

    if "只生成可预览草稿" in prompt or "最终 Skill 能力包" in prompt:
        if "write_skill_artifacts" not in called:
            yield _tool_call("write_skill_artifacts", _artifact_arguments(), len(called))
            return
        if "最终 Skill 能力包" in prompt and "build_skill_package" not in called:
            yield _tool_call("build_skill_package", {}, len(called))
            return
        yield ModelStreamEvent(kind="content", content="## 已生成\n\n能力草稿与依赖声明已完成。")
        yield ModelStreamEvent(kind="completed")
        return

    if "set_plan" not in called:
        yield _tool_call("set_plan", {"items": ["读取 Business Context", "形成 Markdown 回答"]}, len(called))
        return
    if "read_business_context" not in called:
        yield _tool_call("read_business_context", {}, len(called))
        return
    yield ModelStreamEvent(kind="content", content="## 分析结果\n\n- 已读取当前 Business Context\n- 结论来自真实资料和确认记录")
    yield ModelStreamEvent(kind="completed")


def _tool_call(name: str, arguments: dict, index: int) -> ModelStreamEvent:
    return ModelStreamEvent(
        kind="completed",
        tool_calls=[ModelToolCall(id=f"call_{index}_{name}", name=name, arguments=arguments)],
    )


def _artifact_arguments() -> dict:
    return {
        "skill_name": "ecommerce_customer_service",
        "system_prompt": (
            "你是电商客服业务 Agent。所有回答必须读取 Business Context，引用订单与售后政策证据；"
            "规则缺失时提出确认问题，只调用运行时真实提供的 Tool、Skill 和 MCP，不得编造调用结果。"
        ),
        "skill_markdown": (
            "# 电商客服 Skill\n\n## When to use\n\n处理售前、订单状态与售后政策问题时使用。\n\n"
            "## Workflow\n\n1. 读取 Business Context。\n2. 根据证据回答。\n3. 边界不明确时追问。\n\n"
            "## Capabilities\n\n按需调用平台 Tool、已声明 Skill 与已配置 MCP；不得把业务判断写入固定脚本。\n\n"
            "## Safety\n\n退款、金额和审批结论必须带证据，外部写操作必须再次确认。"
        ),
    }


def _sse_events(payload: str) -> list[dict]:
    events: list[dict] = []
    for block in payload.split("\n\n"):
        lines = [line for line in block.splitlines() if line.startswith("data:")]
        if lines:
            events.append(json.loads("\n".join(line[5:].strip() for line in lines)))
    return events
