from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.studio import registry
from app.studio.skill_installer import (
    delete_user_skill,
    install_skill_archive,
    install_skill_files,
)


def _skill_markdown(name: str) -> bytes:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Test Skill {name}.\n"
        "version: 1.2.3\n"
        "---\n\n"
        f"# {name}\n"
    ).encode("utf-8")


def _write_skill(directory: Path, name: str) -> None:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_bytes(_skill_markdown(name))


def _write_managed_record(name: str, *, valid: bool = True) -> None:
    registry.record_studio_managed_skill(name, source="test")
    if not valid:
        registry.STUDIO_SKILL_STATE_PATH.write_text(
            '{"schema_version": 999, "skills": {}}',
            encoding="utf-8",
        )


@pytest.fixture
def isolated_skill_root(tmp_path, monkeypatch):
    root = tmp_path / "system_skills"
    monkeypatch.setattr(registry, "SYSTEM_SKILLS_ROOT", root)
    monkeypatch.setattr(registry, "STUDIO_SKILL_STATE_PATH", tmp_path / "state" / "installed_skills.json")
    registry.clear_skill_registry_cache()
    yield root
    registry.clear_skill_registry_cache()


def test_registry_scans_one_root_and_external_state_controls_skill_ownership(isolated_skill_root):
    _write_skill(isolated_skill_root / "bundled", "bundled")
    _write_skill(isolated_skill_root / "managed", "managed")
    _write_skill(isolated_skill_root / "invalid-marker", "invalid-marker")
    _write_managed_record("invalid-marker", valid=False)
    _write_managed_record("managed")
    _write_skill(isolated_skill_root / ".studio-staging" / "partial", "partial")

    definitions = {item.name: item for item in registry.list_skills()}

    assert set(definitions) == {"bundled", "managed", "invalid-marker"}
    assert definitions["bundled"].kind == "system"
    assert definitions["bundled"].locked is True
    assert definitions["invalid-marker"].kind == "system"
    assert definitions["invalid-marker"].locked is True
    assert definitions["managed"].kind == "user"
    assert definitions["managed"].locked is False
    assert registry.find_skill_directory("managed") == isolated_skill_root / "managed"
    assert all(path.parent == isolated_skill_root for _, path in registry.iter_skill_directories())


def test_registry_reads_folded_standard_skill_description(isolated_skill_root):
    directory = isolated_skill_root / "folded-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: folded-skill\n"
        "description: >\n"
        "  First discovery sentence.\n"
        "  Second discovery sentence.\n"
        "---\n",
        encoding="utf-8",
    )

    definition = registry.list_skills()[0]

    assert definition.description == "First discovery sentence. Second discovery sentence."


@pytest.mark.parametrize(
    ("install_mode", "expected_source"),
    [("folder", "folder-upload"), ("zip", "zip-upload")],
)
def test_installs_target_unified_root_and_keeps_ownership_outside_package(
    isolated_skill_root,
    install_mode,
    expected_source,
):
    name = f"{install_mode}-skill"
    entries = [
        (f"{name}/SKILL.md", _skill_markdown(name)),
        (f"{name}/scripts/run.py", b"print('ok')\n"),
    ]
    if install_mode == "folder":
        installed = install_skill_files(entries)
    else:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, content in entries:
                archive.writestr(path, content)
        installed = install_skill_archive(payload.getvalue())

    directory = isolated_skill_root / name
    ownership = registry.managed_skill_record(name)
    assert installed.kind == "user"
    assert installed.locked is False
    assert directory.parent == isolated_skill_root
    assert (directory / "scripts" / "run.py").is_file()
    assert not (directory / ".studio-skill.json").exists()
    assert ownership == {
        "managed_by": registry.STUDIO_SKILL_MANAGER,
        "directory": name,
        "source": expected_source,
        "installed_at": ownership["installed_at"],
    }
    assert isinstance(ownership["installed_at"], float)

    deleted = delete_user_skill(name)
    assert deleted.name == name
    assert not directory.exists()
    assert registry.managed_skill_record(name) is None


def test_project_bundled_or_invalid_ownership_skill_cannot_be_deleted(isolated_skill_root):
    _write_skill(isolated_skill_root / "bundled", "bundled")
    _write_skill(isolated_skill_root / "invalid-marker", "invalid-marker")
    _write_managed_record("invalid-marker", valid=False)
    registry.clear_skill_registry_cache()

    with pytest.raises(PermissionError, match="cannot be deleted"):
        delete_user_skill("bundled")
    with pytest.raises(PermissionError, match="cannot be deleted"):
        delete_user_skill("invalid-marker")
    with pytest.raises(FileExistsError, match="locked system Skill"):
        install_skill_files([("bundled/SKILL.md", _skill_markdown("bundled"))])

    assert (isolated_skill_root / "bundled").is_dir()
    assert (isolated_skill_root / "invalid-marker").is_dir()


def test_uploaded_skill_rejects_unsafe_hidden_path_without_platform_marker(isolated_skill_root):
    with pytest.raises(ValueError, match="unsupported characters|reserved"):
        install_skill_files(
            [
                ("spoofed/SKILL.md", _skill_markdown("spoofed")),
                ("spoofed/.studio-skill.json", b"{}"),
            ]
        )

    assert not (isolated_skill_root / "spoofed").exists()
