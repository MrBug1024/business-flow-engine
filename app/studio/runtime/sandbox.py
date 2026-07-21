"""Project-managed Python runtime for Studio workspaces and complete Skills.

This module intentionally has no external runtime-service dependency.
It provides dependency isolation through one system-level virtual environment
and maps the virtual ``/workspace``, ``/skills`` and ``/tmp`` roots onto local
directories.  The venv, HOME, caches and temporary files live outside every
business workspace.

This is an application runtime boundary, not an operating-system security
boundary.  It keeps Skill dependencies away from the user's physical Python
environment while retaining the DeepAgents ``BaseSandbox`` contract.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import inspect
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import venv
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox


DEFAULT_EXECUTE_TIMEOUT = 120
DEFAULT_MAX_EXECUTE_TIMEOUT = 900
DEFAULT_MAX_OUTPUT_BYTES = 100_000
DEFAULT_MAX_TRANSFER_BYTES = 64 * 1024 * 1024
_READ_OUTPUT_LIMIT = 500 * 1024
_VIRTUAL_ROOT_PATTERN = re.compile(
    r"/(?:workspace|skills|tmp)(?:/[^\s\"';&|<>)]*)?"
)


class SandboxError(RuntimeError):
    """Base error for managed runtime lifecycle failures."""


class SandboxUnavailableError(SandboxError):
    """Raised when the project-managed Python environment cannot be created."""


class SandboxConfigurationError(SandboxError):
    """Raised when runtime paths violate the system/workspace separation."""


class LocalVenvSandboxBackend(BaseSandbox):
    """DeepAgents backend backed by a shared venv and mapped local folders."""

    def __init__(
        self,
        *,
        runtime_id: str,
        workspace_root: str | Path,
        skills_root: str | Path,
        temp_root: str | Path,
        venv_root: str | Path,
        default_timeout: float = DEFAULT_EXECUTE_TIMEOUT,
        max_execute_timeout: float = DEFAULT_MAX_EXECUTE_TIMEOUT,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_transfer_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
        execution_environment_provider: Callable[..., dict[str, str]] | None = None,
    ) -> None:
        if default_timeout <= 0 or max_execute_timeout <= 0:
            raise ValueError("Sandbox timeouts must be positive.")
        if default_timeout > max_execute_timeout:
            raise ValueError("Default timeout cannot exceed the maximum timeout.")
        if max_output_bytes <= 0 or max_transfer_bytes <= 0:
            raise ValueError("Sandbox byte limits must be positive.")

        self._runtime_id = runtime_id
        self.workspace_root = _require_directory(workspace_root, "workspace")
        self.skills_root = _require_directory(skills_root, "Skills")
        self.temp_root = _require_directory(temp_root, "temporary")
        self.venv_root = _require_directory(venv_root, "virtual environment")
        self._default_timeout = float(default_timeout)
        self._max_execute_timeout = float(max_execute_timeout)
        self._max_output_bytes = int(max_output_bytes)
        self._max_transfer_bytes = int(max_transfer_bytes)
        self._execution_environment_provider = (
            execution_environment_provider or _skill_sandbox_environment
        )
        self._execution_lock = threading.RLock()

    @property
    def id(self) -> str:
        return self._runtime_id

    @property
    def python_executable(self) -> Path:
        return _venv_python(self.venv_root)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Run a command with the managed venv first on PATH.

        Virtual paths in ordinary shell commands are translated to their local
        roots. Commands start in ``/workspace``. Environment values supplied by
        the Skill secret store exist only for this child process and are
        redacted from captured output.
        """

        if not isinstance(command, str) or not command.strip():
            return ExecuteResponse(
                output="Error: command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )
        effective_timeout = self._default_timeout if timeout is None else float(timeout)
        if effective_timeout <= 0:
            raise ValueError("Sandbox execute timeout must be positive.")
        if effective_timeout > self._max_execute_timeout:
            return ExecuteResponse(
                output=(
                    f"Error: timeout {effective_timeout:g}s exceeds the runtime maximum "
                    f"of {self._max_execute_timeout:g}s."
                ),
                exit_code=1,
                truncated=False,
            )

        try:
            scoped_environment = _resolve_execution_environment(
                self._execution_environment_provider,
                command,
            )
        except Exception:
            return ExecuteResponse(
                output="Error: Skill execution environment could not be prepared.",
                exit_code=1,
                truncated=False,
            )
        secret_values = tuple(
            sorted({value for value in scoped_environment.values() if value}, key=len, reverse=True)
        )
        environment = self._base_environment()
        environment.update(scoped_environment)
        translated = self._translate_command(command)

        try:
            with self._execution_lock:
                output, exit_code, timed_out, truncated = _run_command(
                    translated,
                    cwd=self.workspace_root,
                    environment=environment,
                    timeout=effective_timeout,
                    output_limit=self._max_output_bytes,
                )
        except OSError as exc:
            output = f"Error starting managed runtime command: {type(exc).__name__}: {exc}"
            output = _redact_values(output, secret_values)
            return ExecuteResponse(output=output, exit_code=1, truncated=False)

        output = _redact_values(output, secret_values)
        if timed_out:
            suffix = f"\n\nError: command timed out after {effective_timeout:g} seconds."
            output, suffix_truncated = _append_with_limit(
                output,
                suffix,
                self._max_output_bytes,
            )
            truncated = truncated or suffix_truncated
            exit_code = 124
        if truncated and "Output truncated" not in output:
            output, _ = _append_with_limit(
                output,
                f"\n\n... Output truncated at {self._max_output_bytes} bytes.",
                self._max_output_bytes,
            )
        return ExecuteResponse(
            output=output or "<no output>",
            exit_code=exit_code,
            truncated=truncated,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for requested_path, content in files:
            try:
                target = self._resolve_virtual_path(requested_path, writable=True)
                if not isinstance(content, bytes):
                    raise ValueError("invalid_content")
                if len(content) > self._max_transfer_bytes:
                    raise ValueError("file_too_large")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.is_dir():
                    responses.append(FileUploadResponse(requested_path, "is_directory"))
                    continue
                target.write_bytes(content)
            except Exception as exc:
                responses.append(
                    FileUploadResponse(requested_path, _file_operation_error(exc))
                )
            else:
                responses.append(FileUploadResponse(requested_path, None))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for requested_path in paths:
            try:
                source = self._resolve_virtual_path(requested_path, writable=False)
                if not source.exists():
                    raise FileNotFoundError(source)
                if source.is_dir():
                    raise IsADirectoryError(source)
                if not source.is_file():
                    raise PermissionError(source)
                if source.stat().st_size > self._max_transfer_bytes:
                    raise ValueError("file_too_large")
                content = source.read_bytes()
            except Exception as exc:
                responses.append(
                    FileDownloadResponse(
                        requested_path,
                        None,
                        _file_operation_error(exc),
                    )
                )
            else:
                responses.append(FileDownloadResponse(requested_path, content, None))
        return responses

    # Direct filesystem implementations avoid relying on a platform-specific
    # shell for DeepAgents' standard file tools.
    def ls(self, path: str) -> LsResult:
        try:
            directory = self._resolve_virtual_path(path, writable=False, allow_root=True)
            if not directory.exists():
                return LsResult(error=f"Path '{path}': path_not_found")
            if not directory.is_dir():
                return LsResult(error=f"Path '{path}': not_a_directory")
            entries = [
                {
                    "path": _virtual_child(path, child.name),
                    "is_dir": child.is_dir(),
                    "size": child.stat().st_size,
                }
                for child in sorted(directory.iterdir(), key=lambda item: item.name.lower())
            ]
            return LsResult(entries=entries)
        except PermissionError:
            return LsResult(error=f"Path '{path}': permission_denied")
        except ValueError:
            return LsResult(error=f"Path '{path}': invalid_path")

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        try:
            source = self._resolve_virtual_path(file_path, writable=False)
            if not source.exists():
                return ReadResult(error=f"File '{file_path}': file_not_found")
            if not source.is_file():
                return ReadResult(error=f"File '{file_path}': not_a_file")
            raw = source.read_bytes()
            if not raw:
                return ReadResult(
                    file_data={
                        "content": "System reminder: File exists but has empty contents",
                        "encoding": "utf-8",
                    }
                )
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                if len(raw) > _READ_OUTPUT_LIMIT:
                    return ReadResult(error=f"File '{file_path}': binary file is too large")
                return ReadResult(
                    file_data={
                        "content": base64.b64encode(raw).decode("ascii"),
                        "encoding": "base64",
                    }
                )
            lines = text.splitlines()
            if offset < 0 or limit <= 0:
                return ReadResult(error=f"File '{file_path}': invalid pagination")
            if offset >= len(lines):
                return ReadResult(
                    error=f"File '{file_path}': Line offset {offset} exceeds file length ({len(lines)} lines)"
                )
            content = "\n".join(lines[offset : offset + limit])
            content, truncated = _truncate_utf8(content, _READ_OUTPUT_LIMIT)
            if truncated:
                content += "\n\n[Output was truncated; continue with a larger offset or smaller limit.]"
            return ReadResult(file_data={"content": content, "encoding": "utf-8"})
        except PermissionError:
            return ReadResult(error=f"File '{file_path}': permission_denied")
        except ValueError:
            return ReadResult(error=f"File '{file_path}': invalid_path")

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            target = self._resolve_virtual_path(file_path, writable=True)
            if target.exists():
                return WriteResult(error=f"File '{file_path}' already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
            return WriteResult(path=file_path)
        except (PermissionError, ValueError) as exc:
            return WriteResult(error=f"Failed to write file '{file_path}': {_file_operation_error(exc)}")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        try:
            target = self._resolve_virtual_path(file_path, writable=True)
            if not target.exists():
                return EditResult(error=f"Error: File '{file_path}' not found")
            text = target.read_text(encoding="utf-8")
            occurrences = text.count(old_string)
            if occurrences == 0:
                return EditResult(error=f"Error: String not found in file: '{old_string}'")
            if occurrences > 1 and not replace_all:
                return EditResult(error="Error: String appears multiple times. Use replace_all=True.")
            target.write_bytes(
                text.replace(old_string, new_string, -1 if replace_all else 1).encode(
                    "utf-8"
                )
            )
            return EditResult(
                path=file_path,
                occurrences=occurrences if replace_all else 1,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return EditResult(error=f"Error editing file '{file_path}': {_file_operation_error(exc)}")

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        virtual_path = path or "/workspace"
        try:
            target = self._resolve_virtual_path(virtual_path, writable=False, allow_root=True)
            candidates: Iterable[Path] = target.rglob("*") if target.is_dir() else (target,)
            matches = []
            for candidate in candidates:
                if not candidate.is_file() or (glob and not fnmatch.fnmatch(candidate.name, glob)):
                    continue
                try:
                    lines = candidate.read_text(encoding="utf-8").splitlines()
                except (UnicodeError, OSError):
                    continue
                candidate_virtual = self._to_virtual_path(candidate)
                for line_number, line in enumerate(lines, start=1):
                    if pattern in line:
                        matches.append(
                            {"path": candidate_virtual, "line": line_number, "text": line}
                        )
            return GrepResult(matches=matches)
        except (OSError, ValueError):
            return GrepResult(error=f"Path '{virtual_path}': invalid_path")

    def glob(self, pattern: str, path: str = "/workspace") -> GlobResult:
        try:
            target = self._resolve_virtual_path(path, writable=False, allow_root=True)
            matches = [
                {"path": self._to_virtual_path(item), "is_dir": item.is_dir()}
                for item in target.glob(pattern)
            ]
            return GlobResult(matches=matches)
        except (OSError, ValueError):
            return GlobResult(error=f"Path '{path}': invalid_path")

    def _base_environment(self) -> dict[str, str]:
        scripts = _venv_scripts(self.venv_root)
        home = self.venv_root.parent / "home"
        cache = self.venv_root.parent / "cache"
        home.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        environment = {
            "PATH": os.pathsep.join((str(scripts), os.environ.get("PATH", ""))),
            "VIRTUAL_ENV": str(self.venv_root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CACHE_DIR": str(cache / "pip"),
            "HOME": str(home),
            "TMP": str(self.temp_root),
            "TEMP": str(self.temp_root),
            "TMPDIR": str(self.temp_root),
        }
        for key in (
            "COMSPEC",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "WINDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        ):
            if value := os.environ.get(key):
                environment[key] = value
        return environment

    def _translate_command(self, command: str) -> str:
        def replace(match: re.Match[str]) -> str:
            requested = match.group(0)
            try:
                local_path = self._resolve_virtual_path(
                    requested,
                    writable=False,
                    allow_root=True,
                ).as_posix()
                previous = command[match.start() - 1] if match.start() else ""
                following = command[match.end()] if match.end() < len(command) else ""
                if previous in {"'", '"'} or following in {"'", '"'}:
                    return local_path
                return _shell_quote(local_path)
            except (PermissionError, ValueError):
                return requested

        translated = _VIRTUAL_ROOT_PATTERN.sub(replace, command)
        if os.name == "nt":
            translated = re.sub(r"(?<![\w.-])python3(?=\s|$)", "python", translated)
            translated = re.sub(r"(?<![\w.-])pip3(?=\s|$)", "pip", translated)
            translated = translated.replace("/dev/null", "NUL")
        return translated

    def _resolve_virtual_path(
        self,
        requested_path: str,
        *,
        writable: bool,
        allow_root: bool = False,
    ) -> Path:
        if not isinstance(requested_path, str) or "\0" in requested_path or "\\" in requested_path:
            raise ValueError("invalid_path")
        if not requested_path.startswith("/"):
            raise ValueError("invalid_path")
        pure = PurePosixPath(requested_path)
        if ".." in pure.parts:
            raise ValueError("invalid_path")
        roots = {
            "workspace": self.workspace_root,
            "skills": self.skills_root,
            "tmp": self.temp_root,
        }
        if len(pure.parts) < 2 or pure.parts[1] not in roots:
            raise PermissionError("permission_denied")
        root_name = pure.parts[1]
        if writable and root_name == "skills":
            raise PermissionError("permission_denied")
        if len(pure.parts) == 2 and not allow_root:
            raise ValueError("invalid_path")
        root = roots[root_name].resolve()
        target = root.joinpath(*pure.parts[2:]).resolve(strict=False)
        if not _is_relative_to(target, root):
            raise PermissionError("permission_denied")
        return target

    def _to_virtual_path(self, path: Path) -> str:
        resolved = path.resolve(strict=False)
        for name, root in (
            ("workspace", self.workspace_root),
            ("skills", self.skills_root),
            ("tmp", self.temp_root),
        ):
            root_resolved = root.resolve()
            if _is_relative_to(resolved, root_resolved):
                relative = resolved.relative_to(root_resolved).as_posix()
                return f"/{name}" + (f"/{relative}" if relative else "")
        raise ValueError("Path is outside the managed runtime roots.")


class SandboxManager:
    """Own one shared system venv and project-scoped filesystem bindings."""

    def __init__(
        self,
        *,
        runtime_root: str | Path | None = None,
        venv_path: str | Path | None = None,
        default_timeout: float = DEFAULT_EXECUTE_TIMEOUT,
        max_execute_timeout: float = DEFAULT_MAX_EXECUTE_TIMEOUT,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_transfer_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
        execution_environment_provider: Callable[..., dict[str, str]] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root or _default_runtime_root()).expanduser().resolve()
        self.venv_path = Path(venv_path or self.runtime_root / "python").expanduser().resolve()
        self.temp_root = self.runtime_root / "tmp"
        self.default_timeout = float(default_timeout)
        self.max_execute_timeout = float(max_execute_timeout)
        self.max_output_bytes = int(max_output_bytes)
        self.max_transfer_bytes = int(max_transfer_bytes)
        self._execution_environment_provider = (
            execution_environment_provider or _skill_sandbox_environment
        )
        self._lock = threading.RLock()
        self._backends: dict[str, LocalVenvSandboxBackend] = {}

    def backend_for(
        self,
        business_id: str,
        workspace_root: str | Path,
        skills_root: str | Path,
    ) -> LocalVenvSandboxBackend:
        return self.prepare(
            business_id,
            "project",
            workspace_path=workspace_root,
            skills_path=skills_root,
        )

    def prepare(
        self,
        business_id: str,
        session_id: str,
        *,
        workspace_path: str | Path,
        skills_path: str | Path,
    ) -> LocalVenvSandboxBackend:
        workspace = _require_directory(workspace_path, "workspace")
        skills = _require_directory(skills_path, "Skills")
        runtime_id = self.runtime_id(business_id, session_id)
        with self._lock:
            self._validate_system_paths(workspace, skills)
            self._ensure_venv()
            temp = self.temp_root / runtime_id
            temp.mkdir(parents=True, exist_ok=True)
            existing = self._backends.get(runtime_id)
            if (
                existing is not None
                and existing.workspace_root == workspace
                and existing.skills_root == skills
            ):
                return existing
            backend = LocalVenvSandboxBackend(
                runtime_id=runtime_id,
                workspace_root=workspace,
                skills_root=skills,
                temp_root=temp,
                venv_root=self.venv_path,
                default_timeout=self.default_timeout,
                max_execute_timeout=self.max_execute_timeout,
                max_output_bytes=self.max_output_bytes,
                max_transfer_bytes=self.max_transfer_bytes,
                execution_environment_provider=self._execution_environment_provider,
            )
            self._backends[runtime_id] = backend
            return backend

    def status(self, business_id: str, session_id: str) -> dict[str, object]:
        runtime_id = self.runtime_id(business_id, session_id)
        python = _venv_python(self.venv_path)
        backend = self._backends.get(runtime_id)
        ready = python.is_file()
        return {
            "provider": "venv",
            "available": True,
            "exists": ready,
            "running": ready,
            "ready": ready,
            "status": "ready" if ready else "not_prepared",
            "runtime_id": runtime_id,
            "python": str(python) if ready else None,
            "venv_path": str(self.venv_path),
            "root": str(self.runtime_root),
            "shared": True,
            "workspace_path": str(backend.workspace_root) if backend else None,
            "skills_path": str(backend.skills_root) if backend else None,
            "error": None,
        }

    def remove(self, business_id: str, session_id: str) -> bool:
        """Remove only the project binding and temp files; preserve shared venv."""

        runtime_id = self.runtime_id(business_id, session_id)
        with self._lock:
            backend = self._backends.pop(runtime_id, None)
            temp = self.temp_root / runtime_id
            existed = backend is not None or temp.exists()
            if temp.exists():
                shutil.rmtree(temp)
            return existed

    @staticmethod
    def runtime_id(business_id: str, session_id: str) -> str:
        business = business_id.strip()
        session = session_id.strip()
        if not business or not session:
            raise ValueError("business_id and session_id are required.")
        digest = hashlib.sha256(f"{business}\0{session}".encode()).hexdigest()[:24]
        return f"studio-runtime-{digest}"

    def _validate_system_paths(self, workspace: Path, skills: Path) -> None:
        for managed in (self.runtime_root, self.venv_path):
            if _paths_overlap(managed, workspace):
                raise SandboxConfigurationError(
                    "The managed runtime and virtual environment must be outside the business workspace."
                )
            if _paths_overlap(managed, skills):
                raise SandboxConfigurationError(
                    "The managed runtime must be separate from the installed Skills directory."
                )

    def _ensure_venv(self) -> None:
        python = _venv_python(self.venv_path)
        if python.is_file():
            return
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        try:
            venv.EnvBuilder(
                system_site_packages=False,
                clear=False,
                symlinks=os.name != "nt",
                with_pip=True,
                upgrade=False,
            ).create(self.venv_path)
        except Exception as exc:
            raise SandboxUnavailableError(
                f"Unable to create the project-managed Python environment at {self.venv_path}: {exc}"
            ) from exc
        if not python.is_file():
            raise SandboxUnavailableError(
                f"Managed Python executable was not created at {python}."
            )


def _run_command(
    command: str,
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
    output_limit: int,
) -> tuple[str, int, bool, bool]:
    creationflags = 0
    popen_kwargs: dict[str, object] = {"start_new_session": True}
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        popen_kwargs = {"creationflags": creationflags}
    process = subprocess.Popen(  # noqa: S602 - model shell access is the runtime's purpose
        command,
        shell=True,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **popen_kwargs,
    )
    assert process.stdout is not None
    captured = bytearray()
    truncated = False

    def drain() -> None:
        nonlocal truncated
        while chunk := process.stdout.read(64 * 1024):
            remaining = max(0, output_limit - len(captured))
            if remaining:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True

    reader = threading.Thread(target=drain, name="studio-runtime-output", daemon=True)
    reader.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
        exit_code = 124
    reader.join(timeout=2)
    if reader.is_alive():
        truncated = True
    return captured.decode("utf-8", errors="replace"), exit_code, timed_out, truncated


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603 - taskkill is an OS-provided process-tree terminator
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()


def _default_runtime_root() -> Path:
    from app.core.config import settings  # noqa: PLC0415

    configured = getattr(settings, "sandbox_root_path", None)
    if configured is not None:
        return Path(configured)
    return settings.sandbox_root_path


def _configured_sandbox_manager() -> SandboxManager:
    from app.core.config import settings  # noqa: PLC0415

    max_timeout = max(1, int(settings.sandbox_command_timeout))
    return SandboxManager(
        runtime_root=_default_runtime_root(),
        default_timeout=min(DEFAULT_EXECUTE_TIMEOUT, max_timeout),
        max_execute_timeout=max_timeout,
        max_output_bytes=settings.sandbox_output_limit,
        execution_environment_provider=_skill_sandbox_environment,
    )


def _skill_sandbox_environment(command: str) -> dict[str, str]:
    from app.studio.capabilities.skill_secrets import skill_secret_store  # noqa: PLC0415

    return skill_secret_store.sandbox_environment(command)


def _resolve_execution_environment(
    provider: Callable[..., dict[str, str]],
    command: str,
) -> dict[str, str]:
    try:
        signature = inspect.signature(provider)
    except (TypeError, ValueError):
        try:
            provided = provider(command)
        except TypeError:
            provided = provider()
    else:
        try:
            signature.bind(command)
        except TypeError:
            provided = provider()
        else:
            provided = provider(command)
    return {
        str(key): str(value)
        for key, value in dict(provided).items()
        if value is not None
    }


def _require_directory(path: str | Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise SandboxConfigurationError(f"Sandbox {label} directory does not exist: {resolved}")
    return resolved


def _venv_scripts(venv_root: Path) -> Path:
    return venv_root / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv_root: Path) -> Path:
    return _venv_scripts(venv_root) / ("python.exe" if os.name == "nt" else "python")


def _paths_overlap(first: Path, second: Path) -> bool:
    a = first.resolve(strict=False)
    b = second.resolve(strict=False)
    return _is_relative_to(a, b) or _is_relative_to(b, a)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _virtual_child(parent: str, name: str) -> str:
    return f"{parent.rstrip('/')}/{name}"


def _shell_quote(value: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _file_operation_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "file_not_found"
    if isinstance(exc, IsADirectoryError):
        return "is_directory"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, ValueError) and str(exc) == "invalid_path":
        return "invalid_path"
    if isinstance(exc, ValueError) and str(exc) in {"file_too_large", "invalid_content"}:
        return "invalid_path"
    return "permission_denied"


def _redact_values(output: str, secret_values: tuple[str, ...]) -> str:
    redacted = output
    for value in secret_values:
        redacted = redacted.replace(value, "********")
    return redacted


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _append_with_limit(value: str, suffix: str, limit: int) -> tuple[str, bool]:
    combined = f"{value}{suffix}"
    encoded = combined.encode("utf-8")
    if len(encoded) <= limit:
        return combined, False
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) >= limit:
        return suffix_bytes[-limit:].decode("utf-8", errors="ignore"), True
    head = value.encode("utf-8")[: limit - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return f"{head}{suffix}", True


sandbox_manager = _configured_sandbox_manager()


__all__ = [
    "LocalVenvSandboxBackend",
    "SandboxConfigurationError",
    "SandboxError",
    "SandboxManager",
    "SandboxUnavailableError",
    "sandbox_manager",
]
