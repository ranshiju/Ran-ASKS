"""Bind API-only DSH tools to canonical runtime function metadata."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, TypeVar


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import function_registry


ToolT = TypeVar("ToolT")


def bind_tools(tools: Iterable[ToolT]) -> list[ToolT]:
    """Attach registry policy and reject unregistered API tool surfaces."""
    data = function_registry.load_registry()
    bound = list(tools)
    for tool in bound:
        metadata = function_registry.dsh_tool_metadata(tool.name, data)
        tool.function_id = metadata["function_id"]
        tool.audience = tuple(metadata["audience"])
        tool.allowed_callers = tuple(metadata["allowed_callers"])
        tool.effects = tuple(metadata["effects"])
        tool.maturity = metadata["maturity"]
    return bound
