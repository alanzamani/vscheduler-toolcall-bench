"""Configuration for the vscheduler MCP server.

Everything is driven by environment variables so the dev and prod compose
files can point the same image at different vscheduler hosts and different
credentials, exactly like the other services in the stack.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_COMMAND_SPEC = Path(__file__).with_name("commands.yaml")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SSHSettings:
    """How the MCP container reaches the vscheduler container.

    The default user is `ubuntu` because that is the account the vscheduler
    image actually creates (docker/Dockerfile.dev). The default key is the
    MCP-specific keypair that docker/setup.sh generates and Dockerfile.dev
    installs into ubuntu's authorized_keys.
    """

    host: str = field(default_factory=lambda: os.getenv("VSCHEDULER_HOST", "vscheduler"))
    port: int = field(default_factory=lambda: int(os.getenv("VSCHEDULER_SSH_PORT", "22")))
    user: str = field(default_factory=lambda: os.getenv("VSCHEDULER_SSH_USER", "ubuntu"))
    key_path: str = field(
        default_factory=lambda: os.getenv("VSCHEDULER_SSH_KEY", "/home/mcp/.ssh/id_mcp")
    )
    known_hosts: str = field(
        default_factory=lambda: os.getenv("VSCHEDULER_KNOWN_HOSTS", "/home/mcp/.ssh/known_hosts")
    )
    # Dev convenience only. Leave false in prod so host keys are verified.
    insecure_skip_host_key_check: bool = field(
        default_factory=lambda: _env_flag("VSCHEDULER_SSH_INSECURE")
    )
    connect_timeout: int = field(
        default_factory=lambda: int(os.getenv("VSCHEDULER_SSH_CONNECT_TIMEOUT", "10"))
    )


@dataclass(frozen=True)
class ServerSettings:
    host: str = field(default_factory=lambda: os.getenv("MCP_BIND_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("MCP_BIND_PORT", "8930")))
    transport: str = field(
        default_factory=lambda: os.getenv("MCP_TRANSPORT", "streamable-http")
    )
    # Hard ceiling on any single command, regardless of per-command timeout.
    max_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("MCP_MAX_TIMEOUT", "180"))
    )
    max_output_chars: int = field(
        default_factory=lambda: int(os.getenv("MCP_MAX_OUTPUT_CHARS", "20000"))
    )
    # When true, tools that change scheduler state are not registered at all.
    read_only: bool = field(default_factory=lambda: _env_flag("MCP_READ_ONLY"))
    # When true, the post-run output check in commands.yaml `expect:` blocks is
    # skipped. That check costs one extra SSH round trip per call, so this is
    # the escape hatch for a deployment that would rather not pay it.
    skip_expectations: bool = field(
        default_factory=lambda: _env_flag("MCP_SKIP_EXPECTATIONS")
    )
    command_spec_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("MCP_COMMAND_SPEC", str(DEFAULT_COMMAND_SPEC))
        )
    )


@dataclass(frozen=True)
class CommandSpec:
    """A single allowlisted vscheduler command."""

    name: str
    description: str
    binary: str
    mutating: bool = False
    timeout: int = 60
    # Always appended to the argv, after the caller-supplied arguments.
    # In practice this is ["-v"]: without it the vscheduler tools print
    # nothing at all, because every print is gated on the verbose flag.
    fixed_args: list[str] = field(default_factory=list)
    # Arguments the caller may supply, each with a validation pattern.
    # Anything not described here is rejected before execution.
    arguments: list[dict] = field(default_factory=list)
    # Optional post-run output check: files the command must have freshly
    # written for the run to count as successful. See _validate_expect and
    # the `expect:` block in commands.yaml. Empty means no check.
    expect: dict = field(default_factory=dict)

    @property
    def argument_names(self) -> list[str]:
        return [a["name"] for a in self.arguments]


_ALLOWED_ARG_KEYS = {
    "name",
    "description",
    "required",
    "flag",
    "positional",
    "choices",
    "pattern",
    "split",
}


_ALLOWED_EXPECT_KEYS = {"dir", "cases", "min_bytes", "fresh_within"}
_ALLOWED_CASE_KEYS = {"when", "files"}
_WHEN_STATES = {"present", "absent"}


def _validate_expect(name: str, expect: dict, argument_names: set[str]) -> None:
    """Fail at startup on a malformed or unmatchable `expect:` block.

    A `when:` clause naming an argument that does not exist would simply never
    match, leaving a dead expectation that neither fires nor complains -- the
    silent failure this whole mechanism exists to catch. So a typo here is a
    hard startup error, not a shrug.
    """
    unknown = set(expect) - _ALLOWED_EXPECT_KEYS
    if unknown:
        raise ValueError(
            f"Command '{name}': unknown expect key(s) {sorted(unknown)}. "
            f"Allowed: {sorted(_ALLOWED_EXPECT_KEYS)}"
        )
    if not expect.get("dir"):
        raise ValueError(f"Command '{name}': expect block needs a 'dir'")

    cases = expect.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"Command '{name}': expect block needs a non-empty 'cases'")

    for i, case in enumerate(cases):
        unknown = set(case) - _ALLOWED_CASE_KEYS
        if unknown:
            raise ValueError(
                f"Command '{name}', expect case {i}: unknown key(s) "
                f"{sorted(unknown)}. Allowed: {sorted(_ALLOWED_CASE_KEYS)}"
            )
        for arg_name, state in (case.get("when") or {}).items():
            if arg_name not in argument_names:
                raise ValueError(
                    f"Command '{name}', expect case {i}: 'when' names "
                    f"'{arg_name}', which is not a declared argument "
                    f"({sorted(argument_names)}). This case could never match."
                )
            if state not in _WHEN_STATES:
                raise ValueError(
                    f"Command '{name}', expect case {i}: 'when.{arg_name}' is "
                    f"'{state}', must be one of {sorted(_WHEN_STATES)}"
                )
        files = case.get("files")
        if files is not None and not isinstance(files, list):
            raise ValueError(
                f"Command '{name}', expect case {i}: 'files' must be a list"
            )
        for f in files or []:
            if not isinstance(f, str) or f.startswith("/") or ".." in f:
                raise ValueError(
                    f"Command '{name}', expect case {i}: file '{f}' must be a "
                    f"relative path under 'dir', with no '..' segments"
                )


def load_command_specs(path: Path) -> dict[str, CommandSpec]:
    """Load the command allowlist from YAML.

    The allowlist is the security boundary of this server: the executor will
    only ever run a binary that appears here, with arguments that match the
    declared patterns. A malformed spec is a hard failure at startup rather
    than a surprise at call time.
    """
    raw = yaml.safe_load(path.read_text())
    if not raw or "commands" not in raw:
        raise ValueError(f"No 'commands' key found in {path}")

    specs: dict[str, CommandSpec] = {}
    for name, body in (raw["commands"] or {}).items():
        if "binary" not in body:
            raise ValueError(f"Command '{name}' in {path} has no 'binary'")

        arguments = body.get("arguments") or []
        for arg in arguments:
            if "name" not in arg:
                raise ValueError(f"Command '{name}' has an argument with no 'name'")
            unknown = set(arg) - _ALLOWED_ARG_KEYS
            if unknown:
                raise ValueError(
                    f"Command '{name}', argument '{arg['name']}': unknown key(s) "
                    f"{sorted(unknown)}. Allowed: {sorted(_ALLOWED_ARG_KEYS)}"
                )
            if not arg.get("positional") and "flag" not in arg:
                raise ValueError(
                    f"Command '{name}', argument '{arg['name']}': needs either "
                    f"'flag' or 'positional: true'"
                )

        expect = body.get("expect") or {}
        if expect:
            _validate_expect(name, expect, {a["name"] for a in arguments})

        specs[name] = CommandSpec(
            name=name,
            description=(body.get("description") or "").strip(),
            binary=body["binary"],
            mutating=bool(body.get("mutating", False)),
            timeout=int(body.get("timeout", 60)),
            fixed_args=list(body.get("fixed_args") or []),
            arguments=arguments,
            expect=expect,
        )

    if not specs:
        raise ValueError(f"{path} declares no commands")
    return specs
