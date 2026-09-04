"""LLM tool registry (function calling).

Tools are OpenAI-style function definitions that the planner can invoke.
Each tool is an async callable that returns a JSON-safe dict.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.llm.tools.accessibility_tree import (
    TOOL_NAME as ACCESSIBILITY_TREE_NAME,
    TOOL_DEFINITION as ACCESSIBILITY_TREE_DEFINITION,
    get_accessibility_tree,
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [ACCESSIBILITY_TREE_DEFINITION]

_TOOL_EXECUTORS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    ACCESSIBILITY_TREE_NAME: get_accessibility_tree,
}


async def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a registered tool by name with the given arguments."""
    executor = _TOOL_EXECUTORS.get(name)
    if executor is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return await executor(**arguments)
    except Exception as exc:  # noqa: BLE001 - tool errors are fed back to the model
        return {"error": f"{exc.__class__.__name__}: {str(exc)[:500]}"}


__all__ = ["TOOL_DEFINITIONS", "execute_tool"]
