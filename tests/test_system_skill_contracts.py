from __future__ import annotations

import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_SKILLS_ROOT = PROJECT_ROOT / "system_skills"


@pytest.mark.parametrize(
    ("name", "secret_name"),
    [
        ("ocr-parser", "OCR_API_KEY"),
        ("vector-kb", "VECTOR_KB_API_KEY"),
    ],
)
def test_bundled_python_skill_documents_the_studio_runtime_contract(
    name: str,
    secret_name: str,
) -> None:
    skill_root = SYSTEM_SKILLS_ROOT / name
    text = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert f"/skills/{name}" in text
    assert f"python -m pip install --disable-pip-version-check -r /skills/{name}/requirements.txt" in text
    assert f"python /skills/{name}/scripts/" in text
    assert secret_name in text
    assert "/workspace" in text
    assert "只读" in text
    assert "不是独立 Tool" in text
    assert "系统级共享 venv" in text
    assert "业务场景目录之外" in text

    # Skill scripts are package resources. They use the interpreter selected by
    # Studio instead of creating scene-local environments or becoming Tools.
    assert "run_skill_script" not in text
    assert "/workspace/.studio/venvs" not in text
    assert "/workspace/.studio" not in text
    assert ".sandbox-home" not in text
    assert "Docker" not in text
    assert "pip install httpx" not in text
    assert "pip install requests" not in text


@pytest.mark.parametrize(
    ("name", "secret_field"),
    [
        ("ocr-parser", "OCR_API_KEY"),
        ("vector-kb", "api_key"),
    ],
)
def test_bundled_skill_package_does_not_contain_credentials(
    name: str,
    secret_field: str,
) -> None:
    defaults_path = SYSTEM_SKILLS_ROOT / name / "config" / "defaults.json"
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))

    assert defaults[secret_field] == ""
