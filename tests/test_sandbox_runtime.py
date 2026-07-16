from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.studio.sandbox_runtime import (
    LocalVenvSandboxBackend,
    SandboxConfigurationError,
    SandboxManager,
)


@pytest.fixture(scope="module")
def runtime_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("managed-runtime")


def _directories(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    skills = tmp_path / "system_skills"
    workspace.mkdir()
    skills.mkdir()
    return workspace, skills


def _manager(runtime_root: Path, **kwargs) -> SandboxManager:
    return SandboxManager(runtime_root=runtime_root, **kwargs)


def test_prepare_creates_one_shared_system_venv_outside_business_workspace(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    manager = _manager(runtime_root)

    first = manager.prepare(
        "business-1",
        "session-1",
        workspace_path=workspace,
        skills_path=skills,
    )
    second = manager.prepare(
        "business-1",
        "session-1",
        workspace_path=workspace,
        skills_path=skills,
    )

    assert isinstance(first, LocalVenvSandboxBackend)
    assert first is second
    assert first.python_executable.is_file()
    assert first.venv_root == (runtime_root / "python").resolve()
    assert not first.venv_root.is_relative_to(workspace.resolve())
    assert not (workspace / ".sandbox-home").exists()
    assert not (workspace / ".studio").exists()


def test_two_businesses_share_dependencies_but_keep_workspace_and_tmp_separate(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    skills = tmp_path / "system_skills"
    first_workspace.mkdir()
    second_workspace.mkdir()
    skills.mkdir()
    manager = _manager(runtime_root)

    first = manager.backend_for("business-first", first_workspace, skills)
    second = manager.backend_for("business-second", second_workspace, skills)

    assert first.venv_root == second.venv_root
    assert first.workspace_root != second.workspace_root
    assert first.temp_root != second.temp_root
    assert first.temp_root.parent == second.temp_root.parent == (runtime_root / "tmp").resolve()


def test_execute_uses_managed_venv_and_translates_virtual_workspace_path(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    (workspace / "value.txt").write_text("from workspace", encoding="utf-8")
    backend = _manager(runtime_root).backend_for("business-exec", workspace, skills)

    result = backend.execute(
        'python -c "import pathlib,sys; '
        "print(pathlib.Path('/workspace/value.txt').read_text()); "
        'print(sys.prefix)"'
    )

    assert result.exit_code == 0
    assert result.truncated is False
    assert "from workspace" in result.output
    assert str(backend.venv_root) in result.output


def test_complete_skill_package_is_available_to_skill_script(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    package = skills / "demo-skill"
    scripts = package / "scripts"
    scripts.mkdir(parents=True)
    (package / "SKILL.md").write_text("# Demo", encoding="utf-8")
    (package / "resource.txt").write_text("whole package", encoding="utf-8")
    (scripts / "run.py").write_text(
        "from pathlib import Path\n"
        "print((Path(__file__).parents[1] / 'resource.txt').read_text())\n",
        encoding="utf-8",
    )
    backend = _manager(runtime_root).backend_for("business-skill", workspace, skills)

    result = backend.execute("python /skills/demo-skill/scripts/run.py")

    assert result.exit_code == 0
    assert result.output.strip() == "whole package"
    assert backend.read("/skills/demo-skill/SKILL.md").file_data == {
        "content": "# Demo",
        "encoding": "utf-8",
    }


def test_execute_injects_scoped_environment_only_for_child_and_redacts_output(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    secret = "private-ocr-token"
    commands: list[str] = []

    def environment(command: str) -> dict[str, str]:
        commands.append(command)
        return {"OCR_API_KEY": secret}

    backend = _manager(
        runtime_root,
        execution_environment_provider=environment,
    ).backend_for("business-env", workspace, skills)

    result = backend.execute(
        'python -c "import os; print(os.environ[\'OCR_API_KEY\'])"'
    )

    assert result.exit_code == 0
    assert secret not in result.output
    assert "********" in result.output
    assert commands == ['python -c "import os; print(os.environ[\'OCR_API_KEY\'])"']
    assert os.environ.get("OCR_API_KEY") != secret


def test_timeout_terminates_command_and_returns_standard_exit_code(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    backend = _manager(
        runtime_root,
        default_timeout=0.1,
        max_execute_timeout=1,
    ).backend_for("business-timeout", workspace, skills)

    result = backend.execute('python -c "import time; time.sleep(10)"')

    assert result.exit_code == 124
    assert "timed out" in result.output


def test_output_is_bounded_without_stopping_pipe_drain(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    backend = _manager(
        runtime_root,
        max_output_bytes=96,
    ).backend_for("business-output", workspace, skills)

    result = backend.execute('python -c "print(\'x\' * 10000)"')

    assert result.exit_code == 0
    assert result.truncated is True
    assert "Output truncated" in result.output
    assert len(result.output.encode("utf-8")) <= 96


def test_file_transfer_maps_workspace_and_tmp_and_keeps_skills_read_only(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    (skills / "demo").mkdir()
    (skills / "demo" / "SKILL.md").write_text("# Demo", encoding="utf-8")
    backend = _manager(runtime_root).backend_for("business-files", workspace, skills)

    uploaded = backend.upload_files([("/workspace/data/result.txt", b"runtime result")])
    temporary = backend.upload_files([("/tmp/result.txt", b"temporary")])
    denied = backend.upload_files([("/skills/demo/SKILL.md", b"changed")])
    invalid = backend.upload_files([("/workspace/../escape.txt", b"escape")])
    downloaded = backend.download_files(
        ["/workspace/data/result.txt", "/tmp/result.txt", "/skills/demo/SKILL.md"]
    )

    assert uploaded[0].error is None
    assert temporary[0].error is None
    assert denied[0].error == "permission_denied"
    assert invalid[0].error == "invalid_path"
    assert [item.content for item in downloaded] == [
        b"runtime result",
        b"temporary",
        b"# Demo",
    ]
    assert not (workspace / "tmp").exists()


def test_filesystem_helpers_are_cross_platform_and_preserve_virtual_paths(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    backend = _manager(runtime_root).backend_for("business-filesystem", workspace, skills)

    assert backend.write("/workspace/notes/a.txt", "alpha\nbeta").path == "/workspace/notes/a.txt"
    assert backend.ls("/workspace/notes").entries == [
        {"path": "/workspace/notes/a.txt", "is_dir": False, "size": 10}
    ]
    assert backend.read("/workspace/notes/a.txt", offset=1).file_data == {
        "content": "beta",
        "encoding": "utf-8",
    }
    assert backend.grep("alpha", "/workspace").matches == [
        {"path": "/workspace/notes/a.txt", "line": 1, "text": "alpha"}
    ]
    assert backend.edit("/workspace/notes/a.txt", "beta", "gamma").occurrences == 1
    assert backend.glob("**/*.txt", "/workspace").matches == [
        {"path": "/workspace/notes/a.txt", "is_dir": False}
    ]


def test_status_and_remove_release_binding_but_preserve_shared_venv(
    tmp_path: Path,
    runtime_root: Path,
) -> None:
    workspace, skills = _directories(tmp_path)
    manager = _manager(runtime_root)
    before = manager.status("business-clean", "project")
    assert before["provider"] == "venv"
    assert before["shared"] is True

    backend = manager.backend_for("business-clean", workspace, skills)
    temp_root = backend.temp_root
    venv_python = backend.python_executable
    ready = manager.status("business-clean", "project")

    assert ready["ready"] is True
    assert ready["workspace_path"] == str(workspace.resolve())
    assert manager.remove("business-clean", "project") is True
    assert not temp_root.exists()
    assert venv_python.is_file()
    assert manager.remove("business-clean", "project") is False


def test_runtime_path_inside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace, skills = _directories(tmp_path)
    manager = SandboxManager(runtime_root=workspace / "runtime")

    with pytest.raises(SandboxConfigurationError, match="outside the business workspace"):
        manager.backend_for("business-invalid", workspace, skills)


def test_no_external_runtime_dependency_is_declared() -> None:
    source = Path(sys.modules[SandboxManager.__module__].__file__).read_text(encoding="utf-8")

    assert "import docker" not in source.lower()
    assert "from docker" not in source.lower()
    assert "filelock" not in source.lower()
