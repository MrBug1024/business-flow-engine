from __future__ import annotations

import io
import socket
import stat
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.studio import registry, skill_installer
from app.studio.agent_runtime import _skill_instructions
from app.studio.capability_runtime import discover_capabilities
from app.studio.models import AIModelConfig, BusinessContext, BusinessRecord
from app.studio.settings import studio_settings
from app.studio.skill_installer import install_skill_archive, install_skill_files


client = TestClient(app)
SKILL_MARKDOWN = b"""---
name: demo-skill
description: A user-installed test skill.
---

# Demo Skill

Use DEMO_SKILL_INSTRUCTION when the user requests the demo workflow.
"""
@pytest.fixture
def isolated_skill_state(tmp_path, monkeypatch):
    skill_root = tmp_path / "system-skills"
    bundled = skill_root / "locked-skill"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text(
        "---\nname: locked-skill\ndescription: Project-bundled locked Skill.\n---\n",
        encoding="utf-8",
    )
    settings_root = tmp_path / "settings"
    monkeypatch.setattr(registry, "SYSTEM_SKILLS_ROOT", skill_root)
    monkeypatch.setattr(registry, "STUDIO_SKILL_STATE_PATH", settings_root / "installed_skills.json")
    monkeypatch.setattr(studio_settings, "root", settings_root)
    monkeypatch.setattr(studio_settings, "path", settings_root / "studio_settings.json")
    registry.clear_skill_registry_cache()
    yield skill_root
    registry.clear_skill_registry_cache()


def test_folder_upload_is_preserved_and_discovered_at_runtime(isolated_skill_state):
    response = client.post(
        "/api/skills/install/upload",
        headers={"X-Studio-Install-Consent": "true"},
        data={
            "paths": [
                "demo-skill/SKILL.md",
                "demo-skill/scripts/run.py",
                "demo-skill/references/guide.md",
                "demo-skill/assets/icon.txt",
            ]
        },
        files=[
            ("files", ("SKILL.md", SKILL_MARKDOWN, "text/markdown")),
            ("files", ("run.py", b"print('{}')\n", "text/x-python")),
            ("files", ("guide.md", b"# Guide\n", "text/markdown")),
            ("files", ("icon.txt", b"asset", "text/plain")),
        ],
    )

    assert response.status_code == 201, response.text
    assert response.json()["skill"]["name"] == "demo-skill"
    assert response.json()["skill"]["kind"] == "user"
    installed = isolated_skill_state / "demo-skill"
    assert (installed / "scripts" / "run.py").read_text(encoding="utf-8") == "print('{}')\n"
    assert (installed / "references" / "guide.md").is_file()
    assert (installed / "assets" / "icon.txt").read_bytes() == b"asset"

    listed = client.get("/api/user-skills")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["demo-skill"]
    assert "demo-skill" in client.get("/api/settings").json()["installed_skills"]
    assert "demo-skill: A user-installed test skill." in _skill_instructions()

    record = BusinessRecord(
        id="biz_skill_test",
        name="Skill test",
        created_at=0,
        updated_at=0,
        context=BusinessContext(business_id="biz_skill_test", name="Skill test"),
    )
    capabilities = discover_capabilities(record)
    assert not any(item.function_name.startswith("skill__") for item in capabilities)

    protected = client.delete("/api/skills/locked-skill")
    assert protected.status_code == 403
    deleted = client.delete("/api/skills/demo-skill")
    assert deleted.status_code == 200
    assert not installed.exists()
    assert "demo-skill" not in deleted.json()["settings"]["installed_skills"]


def test_skill_installation_rejects_unsafe_paths_and_missing_root(isolated_skill_state):
    denied = client.post(
        "/api/skills/install/upload",
        data={"paths": ["demo-skill/SKILL.md"]},
        files=[("files", ("SKILL.md", SKILL_MARKDOWN, "text/markdown"))],
    )
    assert denied.status_code == 403
    mismatch = client.post(
        "/api/skills/install/upload",
        headers={"X-Studio-Install-Consent": "true"},
        data={"paths": ["demo-skill/SKILL.md"]},
        files=[
            ("files", ("SKILL.md", SKILL_MARKDOWN, "text/markdown")),
            ("files", ("run.py", b"pass\n", "text/x-python")),
        ],
    )
    assert mismatch.status_code == 422
    assert "one relative path" in mismatch.json()["detail"]
    with pytest.raises(ValueError, match="unsafe segment"):
        install_skill_files(
            [
                ("SKILL.md", SKILL_MARKDOWN),
                ("scripts/../../escape.py", b"pass\n"),
            ]
        )
    with pytest.raises(ValueError, match="root"):
        install_skill_files([("nested/readme.md", b"missing")])
    assert not (isolated_skill_state / "demo-skill").exists()


def test_skill_zip_rejects_symbolic_links(isolated_skill_state):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("demo-skill/SKILL.md", SKILL_MARKDOWN)
        link = zipfile.ZipInfo("demo-skill/scripts/link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside.py")

    with pytest.raises(ValueError, match="symbolic links"):
        install_skill_archive(payload.getvalue())
    assert not (isolated_skill_state / "demo-skill").exists()


def test_https_zip_redirect_is_revalidated_and_private_targets_are_rejected(monkeypatch):
    denied = client.post("/api/skills/install/url", json={"url": "https://skills.example/demo.zip"})
    assert denied.status_code == 403

    def public_dns(host, port, type):
        del host, type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(skill_installer.socket, "getaddrinfo", public_dns)

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private.zip"}, request=request)

    with pytest.raises(ValueError, match="private or non-global"):
        skill_installer.download_https_zip(
            "https://skills.example/demo.zip",
            transport=httpx.MockTransport(redirect_handler),
        )
    with pytest.raises(ValueError, match="HTTPS"):
        skill_installer.download_https_zip("http://skills.example/demo.zip")


def test_https_zip_download_and_install(isolated_skill_state, monkeypatch):
    payload = _skill_zip()

    def public_dns(host, port, type):
        del host, type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(skill_installer.socket, "getaddrinfo", public_dns)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload, request=request))
    downloaded = skill_installer.download_https_zip("https://skills.example/demo.zip", transport=transport)
    monkeypatch.setattr(skill_installer, "download_https_zip", lambda url: downloaded)

    response = client.post(
        "/api/skills/install/url",
        headers={"X-Studio-Install-Consent": "true"},
        json={"url": "https://skills.example/demo.zip"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["skill"]["name"] == "demo-skill"
    assert "demo-skill" in response.json()["settings"]["installed_skills"]
    assert (isolated_skill_state / "demo-skill" / "scripts" / "run.py").is_file()
    assert not (isolated_skill_state / "demo-skill" / ".studio-skill.json").exists()
    assert registry.managed_skill_record("demo-skill")["source"] == "url"


def test_model_delete_protects_default_and_falls_back_from_active(isolated_skill_state):
    current = studio_settings.load()
    default_model = next(model for model in current.configured_models if model.default)
    forged_default = default_model.model_copy(
        update={
            "name": "Forged environment model",
            "base_url": "https://forged.invalid/v1",
            "enabled": False,
            "default": False,
        }
    )
    patched = client.patch(
        "/api/settings",
        json={
            "active_model": "user/model-a",
            "configured_models": [
                forged_default.model_dump(mode="json"),
                AIModelConfig(id="user-model-a", name="User A", model="user/model-a").model_dump(mode="json"),
                AIModelConfig(id="user-model-b", name="User B", model="user/model-b", default=True).model_dump(
                    mode="json"
                ),
                AIModelConfig(id="user-model-a", name="Duplicate ID", model="user/model-c").model_dump(mode="json"),
                AIModelConfig(id="user-model-d", name="Duplicate model", model="user/model-b").model_dump(mode="json"),
            ],
        },
    )
    assert patched.status_code == 200, patched.text
    normalized_models = patched.json()["configured_models"]
    assert normalized_models[0] == default_model.model_dump(mode="json")
    assert len({model["id"] for model in normalized_models}) == len(normalized_models)
    assert len({model["model"] for model in normalized_models}) == len(normalized_models)
    assert all(not model["default"] for model in normalized_models[1:])

    deleted = client.delete("/api/models/user-model-a")

    assert deleted.status_code == 200, deleted.text
    payload = deleted.json()
    assert all(model["id"] != "user-model-a" for model in payload["configured_models"])
    assert payload["active_model"] != "user/model-a"
    assert any(model["model"] == payload["active_model"] and model["enabled"] for model in payload["configured_models"])
    assert client.delete(f"/api/models/{default_model.id}").status_code == 409
    assert client.delete("/api/models/missing").status_code == 404


def _skill_zip() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("demo-skill/SKILL.md", SKILL_MARKDOWN)
        archive.writestr("demo-skill/scripts/run.py", "print('{}')\n")
    return payload.getvalue()
