"""UTF-8-safe read backend for the immutable Studio Skill view.

``deepagents.backends.filesystem.FilesystemBackend`` delegates ``grep`` to
``rg``.  On Windows its ``text=True`` child-process decoding follows the host
ANSI code page, while ripgrep emits UTF-8 JSON.  A Chinese Skill can therefore
leave ``CompletedProcess.stdout`` unset after a decoder failure and terminate a
whole Agent turn.  The Skill view is bounded and read-only, so use a small
deterministic UTF-8 search implementation for that route instead.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from time import monotonic

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import GrepMatch, GrepResult


_SKILL_GREP_TIMEOUT_SECONDS = 20.0
_BINARY_SAMPLE_BYTES = 8 * 1024


class Utf8SkillFilesystemBackend(FilesystemBackend):
    """Filesystem backend whose Skill searches never depend on host encoding.

    Other filesystem operations retain DeepAgents' implementation.  Only
    ``grep`` is overridden because it is the operation that launches ripgrep
    with locale-dependent text decoding in the affected DeepAgents release.
    """

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        if not isinstance(pattern, str):
            return GrepResult(error="Error: grep pattern must be text.", matches=[])

        try:
            base_path = self._resolve_path(path or ".")
        except ValueError:
            return GrepResult(matches=[])
        except (OSError, RuntimeError):
            return GrepResult(
                error="Error: Skill search path is unavailable.",
                matches=[],
            )

        try:
            if not base_path.exists():
                return GrepResult(matches=[])
            is_directory = base_path.is_dir()
        except OSError:
            return GrepResult(
                error="Error: Skill search path is unavailable.",
                matches=[],
            )

        root = base_path if is_directory else base_path.parent
        deadline = monotonic() + _SKILL_GREP_TIMEOUT_SECONDS
        matches: list[GrepMatch] = []
        candidates = base_path.rglob("*") if is_directory else (base_path,)

        try:
            for candidate in candidates:
                if monotonic() > deadline:
                    return GrepResult(
                        error=(
                            "Skill search timed out; retry with a narrower path or a file glob."
                        ),
                        matches=matches,
                    )
                try:
                    if (
                        not candidate.is_file()
                        or candidate.stat().st_size > self.max_file_size_bytes
                    ):
                        continue
                    relative = candidate.relative_to(root).as_posix()
                    if glob and not fnmatch.fnmatch(relative, glob):
                        continue
                    raw = candidate.read_bytes()
                except (OSError, RuntimeError, ValueError):
                    continue
                # System Skill assets can contain archives or images.  Do not
                # decode those as text merely because they are below the size limit.
                if b"\0" in raw[:_BINARY_SAMPLE_BYTES]:
                    continue
                text = raw.decode("utf-8", errors="replace")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if pattern not in line:
                        continue
                    try:
                        virtual_path = (
                            self._to_virtual_path(candidate)
                            if self.virtual_mode
                            else str(candidate)
                        )
                    except (OSError, RuntimeError, ValueError):
                        continue
                    matches.append(
                        {
                            "path": virtual_path,
                            "line": line_number,
                            "text": line,
                        }
                    )
        except (OSError, RuntimeError):
            return GrepResult(
                error="Skill search was interrupted; retry with a narrower path.",
                matches=matches,
            )

        return GrepResult(matches=matches)


__all__ = ["Utf8SkillFilesystemBackend"]
