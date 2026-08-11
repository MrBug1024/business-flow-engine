"""Knowledge listing contract expected by package-oriented MCP hosts."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "main_skill" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from compatibility_runtime import columns as get_columns, list_all  # noqa: E402

__all__ = ["get_columns", "list_all"]
