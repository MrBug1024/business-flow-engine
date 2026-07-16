from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.studio import skill_secrets as secret_module
from app.studio.skill_secrets import SkillSecretStore


SKILL_KEYS = {
    "ocr-parser": ("OCR_API_KEY", "OCR_BASE_URL"),
    "vector-kb": ("VECTOR_KB_API_KEY", "VECTOR_KB_BASE_URL"),
}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _store(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
) -> SkillSecretStore:
    return SkillSecretStore(
        path=tmp_path / "private" / "skill_secrets.json",
        skills_root=tmp_path / "skills",
        skill_environment_keys=SKILL_KEYS,
        environment=environment or {},
    )


def test_settings_use_project_managed_venv_and_scope_environment_by_skill() -> None:
    configured = Settings(_env_file=None)

    assert configured.sandbox_provider == "venv"
    assert configured.sandbox_root_path.name == "system_sandbox"
    assert configured.sandbox_skill_environment_keys["ocr-parser"] == (
        "OCR_API_KEY",
        "OCR_BASE_URL",
        "OCR_TIMEOUT_SECONDS",
        "OCR_VERIFY_SSL",
    )
    assert "VECTOR_KB_API_KEY" not in configured.sandbox_skill_environment_keys[
        "ocr-parser"
    ]


def test_environment_requires_explicit_skill_context(tmp_path: Path) -> None:
    path = tmp_path / "private" / "skill_secrets.json"
    _write_json(
        path,
        {
            "version": 1,
            "skills": {
                "ocr-parser": {"OCR_API_KEY": "ocr-secret"},
                "vector-kb": {"VECTOR_KB_API_KEY": "vector-secret"},
            },
        },
    )
    store = _store(tmp_path)

    assert store.sandbox_environment() == {}
    assert store.sandbox_environment("python -m pip install httpx") == {}
    assert store.sandbox_environment("ls /skills/not-installed") == {}
    assert store.sandbox_environment("python /skills/ocr-parser/scripts/parse.py") == {
        "OCR_API_KEY": "ocr-secret"
    }
    assert store.sandbox_environment(
        "python /skills/vector-kb/scripts/kb_client.py"
    ) == {"VECTOR_KB_API_KEY": "vector-secret"}


def test_explicit_skill_names_and_host_environment_are_scoped(tmp_path: Path) -> None:
    path = tmp_path / "private" / "skill_secrets.json"
    _write_json(
        path,
        {
            "version": 1,
            "skills": {
                "ocr-parser": {
                    "OCR_API_KEY": "stored-ocr",
                    "OCR_BASE_URL": "https://stored.example",
                },
                "vector-kb": {"VECTOR_KB_API_KEY": "stored-vector"},
            },
        },
    )
    store = _store(
        tmp_path,
        environment={
            "OCR_API_KEY": "host-ocr",
            "VECTOR_KB_API_KEY": "host-vector",
        },
    )

    assert store.sandbox_environment(skill_names="ocr-parser") == {
        "OCR_API_KEY": "host-ocr",
        "OCR_BASE_URL": "https://stored.example",
    }
    assert store.sandbox_environment(skill_names=["vector-kb"]) == {
        "VECTOR_KB_API_KEY": "host-vector"
    }


def test_constructor_migrates_flat_and_package_secrets_by_skill(tmp_path: Path) -> None:
    store_path = tmp_path / "private" / "skill_secrets.json"
    ocr_defaults = tmp_path / "skills" / "ocr-parser" / "config" / "defaults.json"
    vector_defaults = tmp_path / "skills" / "vector-kb" / "config" / "defaults.json"
    _write_json(store_path, {"OCR_API_KEY": "flat-ocr"})
    _write_json(ocr_defaults, {"OCR_API_KEY": "package-ocr", "other": True})
    _write_json(vector_defaults, {"api_key": "package-vector"})

    _store(tmp_path)

    assert json.loads(store_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "skills": {
            "ocr-parser": {"OCR_API_KEY": "flat-ocr"},
            "vector-kb": {"VECTOR_KB_API_KEY": "package-vector"},
        },
    }
    assert json.loads(ocr_defaults.read_text(encoding="utf-8")) == {
        "OCR_API_KEY": "",
        "other": True,
    }
    assert json.loads(vector_defaults.read_text(encoding="utf-8")) == {
        "api_key": ""
    }


def test_package_secret_is_not_cleared_when_private_save_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults_path = (
        tmp_path / "skills" / "ocr-parser" / "config" / "defaults.json"
    )
    _write_json(defaults_path, {"OCR_API_KEY": "must-survive"})

    def fail_save(*args: object, **kwargs: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(secret_module, "_atomic_write_json", fail_save)

    with pytest.raises(OSError, match="disk unavailable"):
        _store(tmp_path)

    assert json.loads(defaults_path.read_text(encoding="utf-8"))[
        "OCR_API_KEY"
    ] == "must-survive"
