# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it."""

import sys

import anyio
from horae_sdk.testing import read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_gmail import COMMAND, NAME

TOOL = f"{NAME}.recent"


async def serve_and_call() -> tuple[set[str], str | None]:
    # `COMMAND` without its interpreter: run the module with the test's own Python. The
    # server has no secrets here (the test's environment is not the plugin's), so the call
    # fails before it contacts anything.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:], env={})
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        replayed = read_result(await client.call_tool(TOOL, {}))
    return tools, replayed.error


def test_serves_over_stdio() -> None:
    tools, error = anyio.run(serve_and_call)
    assert TOOL in tools
    assert error is not None
    assert error.startswith("secret_missing: GMAIL_CLIENT_ID")
