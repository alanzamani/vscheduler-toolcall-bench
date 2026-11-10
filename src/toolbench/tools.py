"""The tool definitions under test, fetched from the vScheduler MCP server.

The benchmark does not describe the tools itself. It starts the real MCP
server over stdio and asks it for its tool list, so the schemas a model sees
here are byte-for-byte the ones a production MCP client would see. Each run
saves the list it used, so a result can always be traced to its schemas.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from vscheduler_mcp.config import DEFAULT_COMMAND_SPEC, CommandSpec, load_command_specs


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict


async def fetch_from_mcp(command_spec: Path | None = None) -> list[Tool]:
    """Start the MCP server over stdio and return its tools, sorted by name."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "MCP_TRANSPORT": "stdio"}
    # search_docs (the RAG track) is not part of the tool surface under test.
    env.pop("VSCHEDULER_DOCS_DSN", None)
    if command_spec is not None:
        env["MCP_COMMAND_SPEC"] = str(command_spec)
    # Listing tools never touches SSH, so no vscheduler stack is needed here.
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "vscheduler_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()

    def schema(t) -> dict:
        # mcp 2.x snake-cases the field; 1.x keeps the wire name.
        return getattr(t, "input_schema", None) or t.inputSchema

    return sorted(
        (Tool(t.name, t.description or "", schema(t)) for t in listed.tools),
        key=lambda t: t.name,
    )


def save(tools: list[Tool], path: Path) -> None:
    path.write_text(json.dumps([asdict(t) for t in tools], indent=2) + "\n")


def load(path: Path) -> list[Tool]:
    return [Tool(**t) for t in json.loads(path.read_text())]


def load_specs(path: Path | None = None) -> dict[str, CommandSpec]:
    """The allowlist the executor enforces, used to score argument validity."""
    return load_command_specs(path or DEFAULT_COMMAND_SPEC)
