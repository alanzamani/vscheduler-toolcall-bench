"""Runs allowlisted vscheduler commands over SSH.

The MCP container holds no vscheduler code. It treats the vscheduler
container as a service and talks to it the same way production does:
SSH, key auth, one command per invocation.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass, field

from .config import CommandSpec, ServerSettings, SSHSettings


class CommandRejected(Exception):
    """Raised before execution when a request fails validation."""


# Signals that a run failed even though it exited 0. vscheduler does some of
# its work in child processes whose exit status the CLI never checks -- see
# the multiprocessing.Process in tools/report.py -- so a child can die with a
# full traceback while the parent still returns 0. Reporting that as a clean
# pass is exactly how a vreport that had stopped producing its charts went on
# looking healthy. Keep these strict: a banner that fires on routine warnings
# is a banner the reader learns to skip.
_FAILURE_SIGNALS = (
    re.compile(r"^Traceback \(most recent call last\):", re.M),
    re.compile(r"^\s*\w*(?:Error|Exception)\b", re.M),
    # multiprocessing prints this header when a child dies
    re.compile(r"^Process\s+\S+-\d+:", re.M),
    re.compile(r"^(?:Segmentation fault|Killed|Aborted)\b", re.M),
)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    # Expected output files that the run failed to produce; see
    # VSchedulerExecutor._check_expectations.
    unmet: list[str] = field(default_factory=list)

    @property
    def hidden_failure(self) -> bool:
        """True when the command claims success but stderr says otherwise."""
        if self.exit_code != 0 or not self.stderr.strip():
            return False
        return any(p.search(self.stderr) for p in _FAILURE_SIGNALS)

    def as_text(self, limit: int) -> str:
        parts = [f"$ {self.command}", f"exit code: {self.exit_code}"]
        if self.timed_out:
            parts.append("NOTE: command timed out and was terminated.")
        if self.hidden_failure:
            # Deliberately above stdout: stdout can run to thousands of lines
            # and get truncated, and a warning printed after it is a warning
            # nobody reads.
            parts.append(
                "WARNING: this command exited 0 but stderr reports a failure, "
                "so it only partly succeeded. Read the stderr section below "
                "and do not report this run as clean. The exit code alone "
                "does not prove the command did what it says."
            )
        if self.unmet:
            # Same placement and reasoning as the warning above: a failure
            # notice printed below a truncated stdout dump is not a notice.
            parts.append(
                "WARNING: this command exited 0 but did not produce the output "
                "it is supposed to produce, so it failed silently:\n"
                + "\n".join(f"  - {u}" for u in self.unmet)
                + "\nDo not report this run as clean."
            )
        if self.stdout.strip():
            parts.append(f"--- stdout ---\n{_truncate(self.stdout, limit)}")
        if self.stderr.strip():
            parts.append(f"--- stderr ---\n{_truncate(self.stderr, limit)}")
        if not self.stdout.strip() and not self.stderr.strip():
            parts.append(
                "(no output)\n"
                "NOTE: several vscheduler commands print nothing without '-v'. "
                "If this is unexpected, check that the command's spec in "
                "commands.yaml still carries fixed_args: [\"-v\"]."
            )
        return "\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def build_argv(spec: CommandSpec, supplied: dict[str, str]) -> list[str]:
    """Validate supplied arguments against the spec and build the argv.

    Every value is checked against the declared choices or regex pattern.
    Unknown argument names are rejected rather than ignored, so a typo
    surfaces as an error instead of silently running a different command.

    Order is deliberate and load-bearing: flags first, then positionals, then
    the fixed args. vscheduler's parser (general/initiate.py) reads a bare
    status word by looking at sys.argv[arg+1], so a positional only registers
    when it follows its flags. Emitting positionals first -- as a conventional
    argv builder would -- silently produces a command that parses to an empty
    status and does the wrong thing.
    """
    declared = {a["name"]: a for a in spec.arguments}

    unknown = set(supplied) - set(declared)
    if unknown:
        raise CommandRejected(
            f"Unknown argument(s) for {spec.name}: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(spec.argument_names) or '(none)'}"
        )

    missing = [
        name
        for name, a in declared.items()
        if a.get("required") and not str(supplied.get(name) or "").strip()
    ]
    if missing:
        raise CommandRejected(
            f"Missing required argument(s) for {spec.name}: {', '.join(missing)}. "
            f"These are required on purpose -- omitting a node makes most "
            f"vscheduler commands act on every node in every partition."
        )

    positional: list[str] = []
    flagged: list[str] = []

    for name, a in declared.items():
        raw = supplied.get(name)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue

        if "choices" in a and value not in a["choices"]:
            raise CommandRejected(
                f"Argument '{name}' must be one of {a['choices']}, got '{value}'."
            )
        if "pattern" in a and not re.fullmatch(a["pattern"], value):
            raise CommandRejected(
                f"Argument '{name}' value '{value}' does not match the allowed "
                f"pattern {a['pattern']}."
            )

        if a.get("positional"):
            positional.append(value)
        elif a.get("split"):
            # e.g. -d takes two dates as separate argv entries
            flagged.append(a["flag"])
            flagged.extend(value.split())
        else:
            flagged.extend([a["flag"], value])

    return [spec.binary, *flagged, *positional, *spec.fixed_args]


def expected_files(spec: CommandSpec, supplied: dict[str, str]) -> list[str] | None:
    """Pick the `expect` case matching this invocation's argument shape.

    Which files a command writes can depend on which arguments it was given:
    vreport's report_pool.py takes a different branch for "no arguments", for
    "user or node" and for "user and node", and those branches write different
    figures -- the last of them writes none at all. A flat per-command file
    list would therefore fire on runs that were perfectly correct.

    Returns the expected file list (possibly empty, meaning this shape writes
    nothing and nothing should be checked), or None when the command declares
    no expectations or no case matches.
    """
    if not spec.expect:
        return None
    for case in spec.expect.get("cases") or []:
        for arg_name, state in (case.get("when") or {}).items():
            present = bool(str(supplied.get(arg_name) or "").strip())
            if present != (state == "present"):
                break
        else:  # every condition held
            return list(case.get("files") or [])
    return None


def _parse_stat(stdout: str) -> tuple[dict[str, tuple[int, int]], int | None]:
    """Parse the probe's output into {path: (size, mtime)} plus remote now.

    Fields are split from the RIGHT, so a path containing spaces survives.
    """
    seen: dict[str, tuple[int, int]] = {}
    now: int | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("__NOW__ "):
            try:
                now = int(line.split(None, 1)[1])
            except (IndexError, ValueError):
                pass
            continue
        parts = line.rsplit(None, 2)
        if len(parts) != 3:
            continue
        name, size, mtime = parts
        try:
            seen[name] = (int(size), int(mtime))
        except ValueError:
            continue
    return seen, now


class VSchedulerExecutor:
    def __init__(self, ssh: SSHSettings, server: ServerSettings) -> None:
        self.ssh = ssh
        self.server = server

    def _ssh_argv(self, remote_command: str) -> list[str]:
        argv = [
            "ssh",
            "-p",
            str(self.ssh.port),
            "-i",
            self.ssh.key_path,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.ssh.connect_timeout}",
        ]
        if self.ssh.insecure_skip_host_key_check:
            argv += [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                # With no persistent known_hosts, ssh emits "Permanently added
                # ... to the list of known hosts" on every single call. That
                # lands in stderr and therefore in every tool result the model
                # reads, so quiet it. Real errors still come through.
                "-o",
                "LogLevel=ERROR",
            ]
        else:
            argv += [
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self.ssh.known_hosts}",
            ]
        argv += [f"{self.ssh.user}@{self.ssh.host}", remote_command]
        return argv

    async def _run_ssh(self, remote_command: str, timeout: int) -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            *self._ssh_argv(remote_command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CommandResult(
                command=remote_command,
                exit_code=-1,
                stdout="",
                stderr=f"Command exceeded its {timeout}s timeout.",
                timed_out=True,
            )
        return CommandResult(
            command=remote_command,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    async def _check_expectations(
        self, spec: CommandSpec, supplied: dict[str, str]
    ) -> list[str]:
        """Verify the command actually wrote the files it promises to write.

        The stderr banner only catches failures that are noisy. This catches
        the quiet kind: generation that stops producing output while leaving
        the previous run's files in place. That is why freshness, not mere
        existence, is the test -- these files are overwritten each run, so a
        stale figure would satisfy an existence check indefinitely.
        """
        files = expected_files(spec, supplied)
        if not files:  # None (no expectations) or [] (this shape writes none)
            return []

        expect = spec.expect
        directory = str(expect["dir"]).strip("/")
        min_bytes = int(expect.get("min_bytes", 1))
        fresh_within = int(expect.get("fresh_within", 300))

        rel_paths = [f"{directory}/{f}" for f in files]
        # $HOME is left unquoted so the remote shell expands it; the path after
        # it is quoted. Both come from this YAML, never from caller input.
        targets = " ".join(f'"$HOME"/{shlex.quote(p)}' for p in rel_paths)
        probe = f"stat -c '%n %s %Y' -- {targets}; echo \"__NOW__ $(date +%s)\""

        result = await self._run_ssh(probe, 30)
        seen, now = _parse_stat(result.stdout)
        if now is None:
            # The probe itself did not work; say so rather than inventing a
            # verdict about the command.
            return [
                "could not verify output files: the check command returned no "
                f"timestamp ({result.stderr.strip()[:200] or 'no stderr'})"
            ]

        problems: list[str] = []
        for rel in rel_paths:
            match = next(
                (v for name, v in seen.items() if name.endswith("/" + rel)), None
            )
            if match is None:
                problems.append(f"~/{rel} was not written at all")
                continue
            size, mtime = match
            age = now - mtime
            if size < min_bytes:
                problems.append(f"~/{rel} is only {size} bytes (expected >= {min_bytes})")
            elif age > fresh_within:
                problems.append(
                    f"~/{rel} is stale: last written {age}s ago, so it is left "
                    f"over from an earlier run rather than produced by this one"
                )
        return problems

    async def run(self, spec: CommandSpec, supplied: dict[str, str]) -> CommandResult:
        argv = build_argv(spec, supplied)
        # shlex.join gives one safely quoted string for the remote shell.
        remote_command = shlex.join(argv)
        timeout = min(spec.timeout, self.server.max_timeout_seconds)
        result = await self._run_ssh(remote_command, timeout)
        # A non-zero exit already reports itself; only the "succeeded" case
        # needs checking against what it claims to have produced.
        if result.exit_code == 0 and not self.server.skip_expectations:
            result.unmet = await self._check_expectations(spec, supplied)
        return result

    async def check_connectivity(self) -> CommandResult:
        """Cheap reachability probe used by the health tool."""
        return await self._run_ssh("true", 15)
