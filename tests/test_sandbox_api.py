from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import businesses
from app.main import app
from app.studio.registry import SYSTEM_SKILLS_ROOT
from app.studio.sandbox_runtime import SandboxUnavailableError
from app.studio.storage import store


client = TestClient(app)


class _FakeSandboxManager:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.environment_exists = True
        self.ready = False
        self.environment_path = "C:/studio/runtime/venv"
        self.bindings: set[str] = set()
        self.backend_calls: list[dict[str, Any]] = []
        self.remove_calls: list[tuple[str, str]] = []
        self.remove_workspace_exists: list[bool] = []

    def backend_for(self, **kwargs: Any) -> object:
        self.backend_calls.append(kwargs)
        if self.unavailable:
            raise SandboxUnavailableError("Managed Python environment is unavailable for test.")
        self.ready = True
        self.bindings.add(str(kwargs["business_id"]))
        return object()

    def status(self, business_id: str, session_id: str) -> dict[str, Any]:
        return {
            "available": not self.unavailable,
            "exists": self.environment_exists,
            "ready": self.ready,
            "status": "ready" if self.ready else ("unavailable" if self.unavailable else "not_ready"),
            "provider": "venv",
            "scope": "system",
            "shared": True,
            "environment_path": self.environment_path,
            "business_bound": business_id in self.bindings,
            "error": "Managed Python environment is unavailable for test." if self.unavailable else None,
        }

    def remove(self, business_id: str, session_id: str) -> bool:
        self.remove_calls.append((business_id, session_id))
        self.remove_workspace_exists.append(store.business_dir(business_id).exists())
        if self.unavailable:
            raise SandboxUnavailableError("Managed Python environment is unavailable for test.")
        released = business_id in self.bindings
        self.bindings.discard(business_id)
        return released


@pytest.fixture
def business_id() -> str:
    response = client.post("/api/businesses", json={"name": "Sandbox API test"})
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    yield identifier
    store.delete(identifier)


def test_project_sandbox_status_prepare_and_release(monkeypatch, business_id: str) -> None:
    manager = _FakeSandboxManager()
    monkeypatch.setattr(businesses, "sandbox_manager", manager)

    initial = client.get(f"/api/businesses/{business_id}/sandbox/status")
    assert initial.status_code == 200
    assert initial.json()["status"] == "not_ready"
    assert initial.json()["provider"] == "venv"
    assert initial.json()["scope"] == "system"
    assert initial.json()["shared"] is True

    prepared = client.post(f"/api/businesses/{business_id}/sandbox/prepare")
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["ready"] is True
    assert prepared.json()["business_bound"] is True
    assert manager.backend_calls == [
        {
            "business_id": business_id,
            "workspace_root": store.workspace_dir(business_id),
            "skills_root": SYSTEM_SKILLS_ROOT,
        }
    ]
    assert isinstance(manager.backend_calls[0]["workspace_root"], Path)

    removed = client.delete(f"/api/businesses/{business_id}/sandbox")
    assert removed.status_code == 200, removed.text
    assert removed.json()["released"] is True
    assert removed.json()["shared_environment_preserved"] is True
    assert removed.json()["status"]["status"] == "ready"
    assert removed.json()["status"]["exists"] is True
    assert removed.json()["status"]["business_bound"] is False
    assert removed.json()["status"]["environment_path"] == manager.environment_path
    assert manager.environment_exists is True
    assert manager.ready is True
    assert manager.remove_calls == [(business_id, "project")]


def test_prepare_reports_sandbox_unavailable(monkeypatch, business_id: str) -> None:
    manager = _FakeSandboxManager(unavailable=True)
    monkeypatch.setattr(businesses, "sandbox_manager", manager)

    response = client.post(f"/api/businesses/{business_id}/sandbox/prepare")

    assert response.status_code == 503
    assert response.json()["detail"] == "Managed Python environment is unavailable for test."
    assert store.business_dir(business_id).exists()


def test_business_delete_releases_mapping_but_preserves_shared_environment(
    monkeypatch, business_id: str
) -> None:
    manager = _FakeSandboxManager()
    manager.ready = True
    manager.bindings.add(business_id)
    monkeypatch.setattr(businesses, "sandbox_manager", manager)

    response = client.delete(f"/api/businesses/{business_id}")

    assert response.status_code == 200, response.text
    assert response.json()["sandbox_cleanup"] == {
        "attempted": True,
        "released": True,
        "shared_environment_preserved": True,
        "error": None,
    }
    assert manager.remove_workspace_exists == [True]
    assert manager.environment_exists is True
    assert manager.ready is True
    assert not store.business_dir(business_id).exists()


def test_environment_release_failure_does_not_block_business_delete(
    monkeypatch, business_id: str
) -> None:
    manager = _FakeSandboxManager(unavailable=True)
    monkeypatch.setattr(businesses, "sandbox_manager", manager)

    response = client.delete(f"/api/businesses/{business_id}")

    assert response.status_code == 200, response.text
    assert response.json()["sandbox_cleanup"] == {
        "attempted": True,
        "released": False,
        "shared_environment_preserved": True,
        "error": "Managed Python environment is unavailable for test.",
    }
    assert not store.business_dir(business_id).exists()


def test_missing_business_does_not_touch_sandbox(monkeypatch) -> None:
    manager = _FakeSandboxManager()
    monkeypatch.setattr(businesses, "sandbox_manager", manager)

    response = client.delete("/api/businesses/biz-does-not-exist")

    assert response.status_code == 404
    assert manager.remove_calls == []
