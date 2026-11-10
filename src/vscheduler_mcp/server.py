"""MCP server exposing vscheduler's CLI as tools.

Tools are generated from the allowlist in commands.yaml, so adding a
vscheduler command to the natural-language surface is a YAML edit rather
than a code change.

Each command becomes a tool with a real, typed signature -- one parameter per
declared argument, `Literal[...]` where the spec declares choices -- so the
model receives a proper JSON schema and the transport rejects a bad call
before it reaches the executor.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Annotated, Any, Literal, Optional

try:  # mcp >= 2.0 renamed FastMCP to MCPServer; the surface we use is identical
    from mcp.server.mcpserver import MCPServer as _Server
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from mcp.types import ToolAnnotations
from pydantic import Field

from .config import CommandSpec, ServerSettings, SSHSettings, load_command_specs
from .executor import CommandRejected, VSchedulerExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("vscheduler_mcp")

settings = ServerSettings()
ssh_settings = SSHSettings()
specs = load_command_specs(settings.command_spec_path)
executor = VSchedulerExecutor(ssh_settings, settings)

# mcp 1.x takes the bind address on the constructor; 2.x takes it on run().
# Probe once here so both generations bind where the compose file says.
_BIND_ON_INIT = "host" in inspect.signature(_Server.__init__).parameters

if _BIND_ON_INIT:
    mcp = _Server(name="vscheduler", host=settings.host, port=settings.port)
else:
    mcp = _Server(name="vscheduler")


def _describe(spec: CommandSpec) -> str:
    lines = [spec.description.strip()]
    if spec.mutating:
        lines.append(
            "This command CHANGES scheduler state. Confirm the exact target "
            "with the user before calling it."
        )
    return "\n\n".join(line for line in lines if line)


def _annotation(arg: dict) -> Any:
    """Map one spec argument to a typed, described annotation."""
    base: Any = Literal[tuple(arg["choices"])] if "choices" in arg else str
    described = Annotated[base, Field(description=arg.get("description", "").strip())]
    return described if arg.get("required") else Optional[described]


def _register(spec: CommandSpec) -> None:
    """Register one allowlisted command as an MCP tool with a typed schema."""

    async def tool(**kwargs: Any) -> str:
        # Drop unset optionals so the executor sees only what was supplied.
        supplied = {k: v for k, v in kwargs.items() if v is not None and v != ""}
        log.info("tool=%s args=%s", spec.name, sorted(supplied))
        try:
            result = await executor.run(spec, supplied)
        except CommandRejected as exc:
            return f"Request rejected before execution: {exc}"
        except FileNotFoundError:
            return (
                "The ssh client is not available inside the MCP container. "
                "Check that openssh-client is installed in Dockerfile.mcp."
            )
        return result.as_text(settings.max_output_chars)

    params = [
        inspect.Parameter(
            arg["name"],
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if arg.get("required") else None,
            annotation=_annotation(arg),
        )
        # required first: a signature cannot put a defaulted parameter before
        # a non-defaulted one
        for arg in sorted(spec.arguments, key=lambda a: not a.get("required"))
    ]

    tool.__name__ = spec.name
    tool.__doc__ = _describe(spec)
    tool.__signature__ = inspect.Signature(params, return_annotation=str)
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}

    # Hints let a client prompt before a destructive call. vkill in
    # particular is irreversible and no longer prompts in the CLI itself.
    annotations = ToolAnnotations(
        title=spec.name,
        read_only_hint=not spec.mutating,
        destructive_hint=spec.mutating,
        idempotent_hint=False,
        open_world_hint=False,
    )
    mcp.tool(
        name=spec.name,
        description=_describe(spec),
        annotations=annotations,
    )(tool)


for _name, _spec in specs.items():
    if _spec.mutating and settings.read_only:
        log.info("skipping mutating command %s (MCP_READ_ONLY is set)", _name)
        continue
    _register(_spec)
    log.info(
        "registered tool %s -> %s %s",
        _name,
        _spec.binary,
        "(mutating)" if _spec.mutating else "(read-only)",
    )


@mcp.tool(
    name="vscheduler_health",
    description=(
        "Check that the MCP server can reach the vscheduler service over SSH. "
        "Call this first when a command fails for an unclear reason."
    ),
)
async def vscheduler_health() -> str:
    result = await executor.check_connectivity()
    target = f"{ssh_settings.user}@{ssh_settings.host}:{ssh_settings.port}"
    if result.exit_code == 0:
        return f"Reachable: SSH to {target} succeeded."
    return (
        f"Not reachable: SSH to {target} failed (exit {result.exit_code}).\n"
        f"{result.stderr.strip()}\n\n"
        f"Common causes: the MCP public key is not in that user's "
        f"authorized_keys, the key at {ssh_settings.key_path} is missing or "
        f"group/world-readable, or the vscheduler container is still starting."
    )


@mcp.tool(
    name="list_vscheduler_commands",
    description=(
        "List the vscheduler commands this server is permitted to run, with "
        "their arguments. Useful when the user asks what is available."
    ),
)
async def list_vscheduler_commands() -> str:
    lines = []
    for name, spec in specs.items():
        if spec.mutating and settings.read_only:
            continue
        kind = "mutating" if spec.mutating else "read-only"
        args = ", ".join(
            f"{a['name']}{'*' if a.get('required') else ''}" for a in spec.arguments
        )
        lines.append(f"{name} ({kind}) -> {spec.binary} [{args or 'no arguments'}]")
        lines.append(f"    {spec.description.strip()}\n")
    if not lines:
        return "No commands are currently allowlisted."
    return "\n".join(lines) + "\n(* = required)"


# Opt-in documentation search (the RAG track, src/docsrag). Off unless a
# pgvector DSN is configured, so the tool list the benchmark scores against
# is unchanged by default.
if os.getenv("VSCHEDULER_DOCS_DSN"):
    from docsrag.tool import register as _register_docs

    _register_docs(mcp)
    log.info("registered tool search_docs (pgvector docs search)")


def main() -> None:
    log.info(
        "starting vscheduler MCP server on %s:%s (transport=%s, read_only=%s, "
        "ssh=%s@%s:%s)",
        settings.host,
        settings.port,
        settings.transport,
        settings.read_only,
        ssh_settings.user,
        ssh_settings.host,
        ssh_settings.port,
    )
    if _BIND_ON_INIT or settings.transport == "stdio":
        mcp.run(transport=settings.transport)
    else:
        mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
