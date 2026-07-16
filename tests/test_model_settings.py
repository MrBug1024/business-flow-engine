from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.studio.models import AIModelConfig, BusinessContext, BusinessRecord
from app.studio.settings import MODEL_SECRET_MASK, studio_settings


client = TestClient(app)


def test_model_api_keys_are_persisted_masked_and_restored(tmp_path: Path, monkeypatch):
    root = tmp_path / "settings"
    monkeypatch.setattr(studio_settings, "root", root)
    monkeypatch.setattr(studio_settings, "path", root / "studio_settings.json")

    initial = client.get("/api/settings").json()
    custom = {
        "id": "custom_model",
        "name": "Custom model",
        "provider": "openai-compatible",
        "model": "custom-model-v1",
        "base_url": "https://models.example.test/v1",
        "api_key": "sk-model-secret",
        "enabled": True,
        "default": False,
    }
    saved = client.patch(
        "/api/settings",
        json={
            "configured_models": [*initial["configured_models"], custom],
            "active_model": custom["model"],
        },
    )

    assert saved.status_code == 200, saved.text
    public_custom = next(item for item in saved.json()["configured_models"] if item["id"] == custom["id"])
    assert public_custom["api_key"] == MODEL_SECRET_MASK
    assert studio_settings.active_model_config().api_key == "sk-model-secret"
    assert "sk-model-secret" in studio_settings.path.read_text(encoding="utf-8")

    public_custom["enabled"] = False
    toggled = client.patch(
        "/api/settings",
        json={"configured_models": saved.json()["configured_models"][:-1] + [public_custom]},
    )
    assert toggled.status_code == 200, toggled.text
    stored = next(item for item in studio_settings.load().configured_models if item.id == custom["id"])
    assert stored.api_key == "sk-model-secret"
    assert stored.enabled is False

    unknown_mask = client.patch(
        "/api/settings",
        json={
            "configured_models": [
                {
                    **custom,
                    "id": "unknown_model",
                    "model": "unknown-model",
                    "api_key": MODEL_SECRET_MASK,
                }
            ]
        },
    )
    assert unknown_mask.status_code == 422


def test_model_gateway_uses_the_selected_model_key(monkeypatch):
    from app.studio import llm

    model = AIModelConfig(
        id="selected",
        name="Selected",
        model="selected-model",
        base_url="https://models.example.test/v1",
        api_key="selected-secret",
        default=False,
    )
    captured: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            yield b"data: [DONE]\n"

    def urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["url"] = request.full_url
        captured["timeout"] = str(timeout)
        return Response()

    monkeypatch.setattr(studio_settings, "active_model_config", lambda requested=None: model)
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    record = BusinessRecord(
        id="business-model",
        name="Model test",
        created_at=1,
        updated_at=1,
        context=BusinessContext(business_id="business-model", name="Model test"),
    )

    events = list(llm.stream_model_turn(record, [{"role": "user", "content": "hello"}]))

    assert captured == {
        "authorization": "Bearer selected-secret",
        "url": "https://models.example.test/v1/chat/completions",
        "timeout": "60",
    }
    assert "".join(item.content for item in events if item.kind == "content") == "ok"
