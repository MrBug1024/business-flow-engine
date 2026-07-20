"""Dynamic discovery for LangChain tools installed in the project tool directory.

Tool modules are trusted local plugins. Discovery isolates import failures and keeps
them visible in the catalog, but it does not sandbox Python module execution.
"""

from __future__ import annotations

import hashlib
import importlib.util
import keyword
import re
import sys
from collections import Counter
from pathlib import Path
from threading import RLock
from types import ModuleType
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from app.core.config import PROJECT_ROOT


TOOLS_ROOT = PROJECT_ROOT / "tools"
ToolDiscoveryStatus = Literal["ready", "duplicate", "error"]
ToolDiscoveryType = Literal["tool", "module_error"]


class ToolMetadata(BaseModel):
    """Serializable discovery state for one tool or failed module import."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    source: str
    module: str
    status: ToolDiscoveryStatus
    error: str | None = None
    mounted: bool = False
    record_type: ToolDiscoveryType = "tool"


class DynamicToolRegistry:
    """Scan a directory and mount every unambiguous LangChain ``BaseTool``."""

    def __init__(self, root: Path = TOOLS_ROOT) -> None:
        self.root = Path(root).resolve()
        root_hash = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:12]
        self._namespace = f"_studio_dynamic_tools_{root_hash}_{id(self):x}"
        self._lock = RLock()
        self._metadata: tuple[ToolMetadata, ...] = ()
        self._tools: dict[str, BaseTool] = {}
        self._generation = 0
        self.refresh()

    @property
    def generation(self) -> int:
        """Return the monotonically increasing successful refresh generation."""

        with self._lock:
            return self._generation

    def list(self) -> list[ToolMetadata]:
        """Return a snapshot of tools and module-level discovery failures."""

        with self._lock:
            return list(self._metadata)

    def tools(self) -> list[BaseTool]:
        """Return all valid tools in deterministic name order."""

        with self._lock:
            return [self._tools[name] for name in sorted(self._tools, key=str.casefold)]

    def get_tools(self) -> list[BaseTool]:
        """Alias used by agent builders that expect a ``get_tools`` method."""

        return self.tools()

    def get(self, name: str) -> BaseTool | None:
        """Return a mounted tool by model-visible name."""

        with self._lock:
            return self._tools.get(name)

    def refresh(self) -> list[ToolMetadata]:
        """Rescan the root and atomically replace the mounted tool catalog."""

        with self._lock:
            self._clear_dynamic_modules()
            candidates, module_errors = self._discover_candidates()
            metadata, mounted = self._build_catalog(candidates, module_errors)
            self._metadata = tuple(metadata)
            self._tools = mounted
            self._generation += 1
            return list(self._metadata)

    def _discover_candidates(
        self,
    ) -> tuple[list[tuple[BaseTool, str, str]], list[ToolMetadata]]:
        candidates: list[tuple[BaseTool, str, str]] = []
        errors: list[ToolMetadata] = []
        if not self.root.exists():
            return candidates, errors
        if not self.root.is_dir():
            errors.append(
                self._module_error(
                    source=".",
                    module=self._namespace,
                    error=NotADirectoryError(f"Tool root is not a directory: {self.root}"),
                )
            )
            return candidates, errors

        self._install_namespace_package(self._namespace, self.root)
        for path in self._tool_module_paths():
            source = path.relative_to(self.root).as_posix()
            module_name = self._module_name(path)
            try:
                module = self._load_module(path, module_name)
                candidates.extend(self._module_tools(module, source, module_name))
            except Exception as exc:  # noqa: BLE001 - one plugin must not break discovery
                errors.append(self._module_error(source, module_name, exc))
                sys.modules.pop(module_name, None)
        return candidates, errors

    def _tool_module_paths(self) -> list[Path]:
        paths: list[Path] = []
        for path in self.root.rglob("*.py"):
            try:
                relative = path.resolve().relative_to(self.root)
            except (OSError, ValueError):
                continue
            if any(part == "__pycache__" or part.startswith(("_", ".")) for part in relative.parts):
                continue
            if path.is_file():
                paths.append(path.resolve())
        return sorted(paths, key=lambda item: item.relative_to(self.root).as_posix().casefold())

    def _module_name(self, path: Path) -> str:
        relative = path.relative_to(self.root).with_suffix("")
        parts = [self._module_segment(part) for part in relative.parts]
        return ".".join((self._namespace, *parts))

    @staticmethod
    def _module_segment(value: str) -> str:
        if value.isidentifier() and not keyword.iskeyword(value):
            return value
        normalized = re.sub(r"\W", "_", value, flags=re.ASCII).strip("_") or "module"
        if normalized[0].isdigit() or keyword.iskeyword(normalized):
            normalized = f"module_{normalized}"
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"{normalized}_{digest}"

    def _load_module(self, path: Path, module_name: str) -> ModuleType:
        existing = sys.modules.get(module_name)
        if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
            return existing

        self._install_parent_packages(path, module_name)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create an import specification for {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        previous_bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        finally:
            sys.dont_write_bytecode = previous_bytecode_setting
        return module

    def _install_parent_packages(self, path: Path, module_name: str) -> None:
        relative_parts = path.relative_to(self.root).parts[:-1]
        package_name = self._namespace
        package_path = self.root
        self._install_namespace_package(package_name, package_path)
        for raw_part, encoded_part in zip(
            relative_parts,
            module_name.split(".")[1:-1],
            strict=True,
        ):
            package_name = f"{package_name}.{encoded_part}"
            package_path /= raw_part
            self._install_namespace_package(package_name, package_path)

    @staticmethod
    def _install_namespace_package(name: str, path: Path) -> None:
        if name in sys.modules:
            return
        module = ModuleType(name)
        module.__package__ = name
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        module.__file__ = str(path)
        module.__spec__ = importlib.util.spec_from_loader(name, loader=None, is_package=True)
        sys.modules[name] = module

    def _module_tools(
        self,
        module: ModuleType,
        source: str,
        module_name: str,
    ) -> list[tuple[BaseTool, str, str]]:
        discovered: list[tuple[BaseTool, str, str]] = []
        seen_objects: set[int] = set()
        for attribute, value in sorted(vars(module).items()):
            if attribute.startswith("_") or not isinstance(value, BaseTool):
                continue
            if id(value) in seen_objects or self._belongs_to_other_local_module(value, module_name):
                continue
            seen_objects.add(id(value))
            discovered.append((value, source, module_name))
        return discovered

    def _belongs_to_other_local_module(self, tool: BaseTool, module_name: str) -> bool:
        callable_value = getattr(tool, "func", None) or getattr(tool, "coroutine", None)
        owner = getattr(callable_value, "__module__", "")
        if not owner:
            owner = getattr(type(tool), "__module__", "")
        return bool(owner.startswith(f"{self._namespace}.") and owner != module_name)

    def _build_catalog(
        self,
        candidates: list[tuple[BaseTool, str, str]],
        module_errors: list[ToolMetadata],
    ) -> tuple[list[ToolMetadata], dict[str, BaseTool]]:
        names = [str(tool.name).strip() for tool, _, _ in candidates]
        counts = Counter(name for name in names if name)
        metadata: list[ToolMetadata] = list(module_errors)
        mounted: dict[str, BaseTool] = {}

        for (tool, source, module), name in zip(candidates, names, strict=True):
            if not name:
                metadata.append(
                    self._tool_error(tool, source, module, "Tool name must not be empty.")
                )
                continue
            description = str(tool.description or "").strip()
            if not description:
                metadata.append(
                    self._tool_error(
                        tool,
                        source,
                        module,
                        "Tool description must not be empty.",
                        name=name,
                    )
                )
                continue
            if counts[name] > 1:
                duplicate_sources = sorted(
                    source_item
                    for candidate, source_item, _ in candidates
                    if str(candidate.name).strip() == name
                )
                metadata.append(
                    ToolMetadata(
                        name=name,
                        description=description,
                        source=source,
                        module=module,
                        status="duplicate",
                        error=(
                            f"Duplicate tool name {name!r}; conflicting sources: "
                            + ", ".join(duplicate_sources)
                        ),
                    )
                )
                continue
            try:
                input_schema = tool.get_input_schema().model_json_schema()
            except Exception as exc:  # noqa: BLE001 - invalid plugin schema is catalog state
                metadata.append(
                    self._tool_error(
                        tool,
                        source,
                        module,
                        f"Unable to build input schema: {type(exc).__name__}: {exc}",
                        name=name,
                    )
                )
                continue
            metadata.append(
                ToolMetadata(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    source=source,
                    module=module,
                    status="ready",
                    mounted=True,
                )
            )
            mounted[name] = tool

        metadata.sort(
            key=lambda item: (
                item.record_type != "tool",
                item.name.casefold(),
                item.source.casefold(),
            )
        )
        return metadata, mounted

    @staticmethod
    def _tool_error(
        tool: BaseTool,
        source: str,
        module: str,
        message: str,
        *,
        name: str | None = None,
    ) -> ToolMetadata:
        return ToolMetadata(
            name=name or str(tool.name or "<unnamed>"),
            description=str(tool.description or ""),
            source=source,
            module=module,
            status="error",
            error=message,
        )

    @staticmethod
    def _module_error(source: str, module: str, error: Exception) -> ToolMetadata:
        return ToolMetadata(
            name=source,
            description="Python tool module could not be loaded.",
            source=source,
            module=module,
            status="error",
            error=f"{type(error).__name__}: {error}",
            record_type="module_error",
        )

    def _clear_dynamic_modules(self) -> None:
        prefix = f"{self._namespace}."
        for name in tuple(sys.modules):
            if name == self._namespace or name.startswith(prefix):
                sys.modules.pop(name, None)


tool_registry = DynamicToolRegistry()


__all__ = [
    "DynamicToolRegistry",
    "TOOLS_ROOT",
    "ToolMetadata",
    "tool_registry",
]
