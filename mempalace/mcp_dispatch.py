"""Transport-neutral MemPalace MCP tool registration and dispatch."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any


class ToolDispatchError(Exception):
    """Base error with the JSON-RPC code used by the stdio transport."""

    code = -32000


class UnknownToolError(ToolDispatchError):
    code = -32601


class InvalidToolArgumentsError(ToolDispatchError):
    code = -32602


def list_tool_specs(tools: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic MCP tool definitions from a MemPalace registry."""
    return [
        {
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
        }
        for name, tool in tools.items()
    ]


def prepare_tool_call(
    tools: Mapping[str, Mapping[str, Any]],
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve a tool and normalize only arguments declared by its schema."""
    if tool_name not in tools:
        raise UnknownToolError(f"Unknown tool: {tool_name}")
    if arguments is not None and not isinstance(arguments, Mapping):
        raise InvalidToolArgumentsError("Tool arguments must be a JSON object")

    tool = tools[tool_name]
    handler = tool["handler"]
    schema_props = tool["input_schema"].get("properties", {})
    tool_args = dict(arguments or {})

    try:
        signature = inspect.signature(handler)
        accepts_var_keyword = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    except (ValueError, TypeError):
        accepts_var_keyword = False

    if not accepts_var_keyword:
        tool_args = {key: value for key, value in tool_args.items() if key in schema_props}

    for key, value in list(tool_args.items()):
        declared_type = schema_props.get(key, {}).get("type")
        try:
            if declared_type == "integer" and not isinstance(value, int):
                tool_args[key] = int(value)
            elif declared_type == "number" and not isinstance(value, (int, float)):
                tool_args[key] = float(value)
        except (ValueError, TypeError) as exc:
            raise InvalidToolArgumentsError(f"Invalid value for parameter '{key}'") from exc

    # Retain compatibility with older clients that sent this transport hint.
    tool_args.pop("wait_for_previous", None)
    return handler, tool_args


def dispatch_tool(
    tools: Mapping[str, Mapping[str, Any]],
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> Any:
    """Invoke a registered tool independently of stdio or HTTP framing."""
    handler, tool_args = prepare_tool_call(tools, tool_name, arguments)
    return handler(**tool_args)
