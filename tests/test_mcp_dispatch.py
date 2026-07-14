"""Pure tests for the transport-neutral MCP tool dispatcher."""

import pytest

from mempalace.mcp_dispatch import (
    InvalidToolArgumentsError,
    UnknownToolError,
    dispatch_tool,
    list_tool_specs,
)


def _tools(handler):
    return {
        "sample": {
            "description": "Sample tool",
            "input_schema": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                },
            },
            "handler": handler,
        }
    }


def test_list_tool_specs_is_deterministic_and_omits_handlers():
    tools = _tools(lambda: None)
    assert list_tool_specs(tools) == [
        {
            "name": "sample",
            "description": "Sample tool",
            "inputSchema": tools["sample"]["input_schema"],
        }
    ]


def test_dispatch_allowlists_and_coerces_declared_arguments():
    captured = {}

    def handler(count=0, ratio=0.0):
        captured.update(count=count, ratio=ratio)
        return captured

    result = dispatch_tool(
        _tools(handler),
        "sample",
        {"count": "4", "ratio": "0.5", "internal": "not-forwarded"},
    )
    assert result == {"count": 4, "ratio": 0.5}


def test_dispatch_removes_legacy_transport_hint_for_kwargs_handler():
    def handler(**kwargs):
        return kwargs

    assert dispatch_tool(
        _tools(handler),
        "sample",
        {"count": 1, "wait_for_previous": True},
    ) == {"count": 1}


def test_dispatch_rejects_unknown_tool_and_non_object_arguments():
    tools = _tools(lambda: None)
    with pytest.raises(UnknownToolError, match="Unknown tool"):
        dispatch_tool(tools, "missing", {})
    with pytest.raises(InvalidToolArgumentsError, match="JSON object"):
        dispatch_tool(tools, "sample", ["not", "an", "object"])


def test_dispatch_rejects_uncoercible_number():
    with pytest.raises(InvalidToolArgumentsError, match="count"):
        dispatch_tool(_tools(lambda count=0: count), "sample", {"count": "not-an-int"})
