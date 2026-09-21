"""The Strategist's tools.

Importing the modules registers them; `REGISTRY` is the single source the
loop, the schema builder and the tests all read.
"""

from __future__ import annotations

from api.ai.tools import changes, issues, pages, performance  # noqa: F401
from api.ai.tools.base import (
    REGISTRY,
    ROW_CAP,
    StrategistScope,
    ToolError,
    definitions,
    run_tool,
)

__all__ = [
    "REGISTRY",
    "ROW_CAP",
    "StrategistScope",
    "ToolError",
    "definitions",
    "run_tool",
]
