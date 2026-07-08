"""Sandbox runtime helpers for the independent Agent platform.

The current implementation provides a per-user Python virtual environment
runtime. It isolates Python package resolution from the backend process and
keeps command execution rooted in the per-turn runtime workspace.
"""

from __future__ import annotations

import os
import re
import subprocess
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol


DEFAULT_EXECUTE_TIMEOUT = 120
MAX_OUTPUT_BYTES = 100_000
_ACTIVE_SANDBOX_ID: ContextVar[str] = ContextVar("bfe_active_sandbox_id", default="")


def venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def venv_python(venv_dir: Path) -> Path:
    return venv_bin_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")


def active_sandbox_id() -> str:
    return _ACTIVE_SANDBOX_ID.get()


def set_active_sandbox_id(sandbox_id: str):
    return _ACTIVE_SANDBOX_ID.set(str(sandbox_id or ""))


def reset_active_sandbox_id(token) -> None:
    _ACTIVE_SANDBOX_ID.reset(token)


def sandbox_execution_env(sandbox: dict[str, Any] | None) -> dict[str, str]:
    """Build a controlled process environment for Skill/MCP execution."""
    keep = [
        "SystemRoot",
        "COMSPEC",
        "ComSpec",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "LANG",
        "LC_ALL",
    ]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    existing_path = os.environ.get("PATH", "")
    path_parts: list[str] = []

    raw_venv = str((sandbox or {}).get("venv_path") or "").strip()
    if raw_venv:
        venv_dir = Path(raw_venv).resolve()
        bin_dir = venv_bin_dir(venv_dir)
        if venv_dir.exists() and bin_dir.exists():
            env["VIRTUAL_ENV"] = str(venv_dir)
            path_parts.append(str(bin_dir))

    raw_root = str((sandbox or {}).get("path") or "").strip()
    if raw_root:
        sandbox_root = Path(raw_root).resolve()
        node_root = sandbox_root / "node"
        node_modules = node_root / "node_modules"
        node_bin = node_modules / ".bin"
        if node_bin.exists():
            path_parts.append(str(node_bin))
        if node_modules.exists():
            env["NODE_PATH"] = str(node_modules)
        env["BFE_SANDBOX_ROOT"] = str(sandbox_root)
        if sandbox and sandbox.get("id"):
            env["BFE_SANDBOX_ID"] = str(sandbox.get("id"))
        env.setdefault("BFE_OUT_DIR", str(sandbox_root / "outputs"))

    env["PATH"] = os.pathsep.join([*path_parts, existing_path]) if path_parts else existing_path
    return env


class SkillSandboxBackend(FilesystemBackend, SandboxBackendProtocol):
    """Filesystem backend with command execution routed through an Agent sandbox.

    This is dependency isolation, not a VM/container security boundary. The
    command runs in the runtime workspace with the selected venv first on PATH.
    """

    def __init__(
        self,
        *,
        root_dir: str | Path,
        sandbox: dict[str, Any] | None = None,
        sandboxes: dict[str, dict[str, Any]] | None = None,
        timeout: int = DEFAULT_EXECUTE_TIMEOUT,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True, max_file_size_mb=10)
        self._sandbox = dict(sandbox or {})
        self._default_sandbox_id = str(self._sandbox.get("id") or "")
        self._sandboxes = {str(k): dict(v) for k, v in (sandboxes or {}).items() if k}
        if self._default_sandbox_id:
            self._sandboxes[self._default_sandbox_id] = self._sandbox
        self._env_cache: dict[str, dict[str, str]] = {}
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes

    @property
    def id(self) -> str:
        return f"bfe-skill-sandbox:{Path(self.cwd).resolve()}"

    def _physical_path(self, virtual_path: str) -> str:
        rel = virtual_path.lstrip("/\\")
        root = Path(self.cwd).resolve()
        resolved = (root / rel).resolve()
        if resolved != root and root not in resolved.parents:
            return str(root)
        return str(resolved)

    def _quote(self, value: str) -> str:
        return subprocess.list2cmdline([value])

    def _translate_virtual_paths(self, command: str) -> str:
        """Translate deepagents virtual absolute paths into host paths."""

        def quoted(match: re.Match[str]) -> str:
            path = match.group("path")
            return self._quote(self._physical_path(path))

        command = re.sub(
            r"(?P<quote>[\"'])(?P<path>/(?:skills|attachments|tmp|workspace|runs|data)[^\"']*)(?P=quote)",
            quoted,
            command,
        )

        def bare(match: re.Match[str]) -> str:
            return self._quote(self._physical_path(match.group("path")))

        return re.sub(
            r"(?<![\w:/\\])(?P<path>/(?:skills|attachments|tmp|workspace|runs|data)[^\s\"']*)",
            bare,
            command,
        )

    def _current_sandbox(self) -> dict[str, Any] | None:
        sandbox_id = active_sandbox_id()
        if sandbox_id and sandbox_id in self._sandboxes:
            return self._sandboxes[sandbox_id]
        if self._default_sandbox_id and self._default_sandbox_id in self._sandboxes:
            return self._sandboxes[self._default_sandbox_id]
        return self._sandbox or None

    def _current_env(self, sandbox: dict[str, Any] | None) -> dict[str, str]:
        sandbox_id = str((sandbox or {}).get("id") or "")
        if sandbox_id and sandbox_id in self._env_cache:
            return self._env_cache[sandbox_id]
        env = sandbox_execution_env(sandbox)
        if sandbox_id:
            self._env_cache[sandbox_id] = env
        return env

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(output="Error: Command must be a non-empty string.", exit_code=1, truncated=False)

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            return ExecuteResponse(output=f"Error: timeout must be positive, got {effective_timeout}.", exit_code=1, truncated=False)

        translated = self._translate_virtual_paths(command)
        sandbox = self._current_sandbox()
        if sandbox and str(sandbox.get("status") or "") not in {"ready", "host"}:
            return ExecuteResponse(
                output=(
                    "Error: selected sandbox is not ready. "
                    f"Sandbox {sandbox.get('name') or sandbox.get('id')} "
                    f"status={sandbox.get('status') or 'unknown'} "
                    f"error={sandbox.get('error') or ''}"
                ),
                exit_code=1,
                truncated=False,
            )
        try:
            result = subprocess.run(  # noqa: S602
                translated,
                check=False,
                shell=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=effective_timeout,
                env=self._current_env(sandbox),
                cwd=str(self.cwd),
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective_timeout} seconds.",
                exit_code=124,
                truncated=False,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(exc).__name__}): {exc}",
                exit_code=1,
                truncated=False,
            )

        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.extend(f"[stderr] {line}" for line in result.stderr.strip().splitlines())
        output = "\n".join(parts) if parts else "<no output>"

        truncated = False
        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes] + f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True
        if result.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.execute(command, timeout=timeout)
