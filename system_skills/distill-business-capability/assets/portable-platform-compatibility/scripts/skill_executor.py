"""Package-host entrypoint delegating to the portable compatibility runtime."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compatibility_runtime import produce

__all__ = ["produce"]
