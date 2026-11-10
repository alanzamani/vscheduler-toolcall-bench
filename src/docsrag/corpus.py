"""The documents the RAG track searches.

Two kinds of source:

- Markdown copied from the vScheduler deployment repository into
  `rag/corpus/` (deployment, MCP server, Ansible, admin install, release
  notes). Copied, not linked, so a published result can always be traced to
  the exact text it was scored on.
- A CLI reference rendered from the MCP server's `commands.yaml`, the same
  allowlist the tool-calling benchmark uses. It is rendered at load time, so
  it can never drift from the allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from vscheduler_mcp.config import DEFAULT_COMMAND_SPEC

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = ROOT / "rag" / "corpus"
CLI_REFERENCE = "cli-reference.md"


@dataclass(frozen=True)
class Doc:
    source: str  # file name, used as the citation
    text: str


def render_commands(path: Path = DEFAULT_COMMAND_SPEC) -> str:
    """Render the allowlist as a Markdown CLI reference, one section per command."""
    raw = path.read_text()
    spec = yaml.safe_load(raw)["commands"]
    out = ["# vScheduler CLI reference (MCP tools)", ""]
    for name, c in spec.items():
        out += [f"## {name}", "", " ".join(c["description"].split()), ""]
        out.append("Changes state: " + ("yes" if c.get("mutating") else "no (read-only)") + ".")
        for a in c.get("arguments", []):
            desc = " ".join(a.get("description", "").split())
            req = "required" if a.get("required") else "optional"
            flag = a.get("flag", "positional")
            line = f"- `{a['name']}` ({flag}, {req}): {desc}"
            if "choices" in a:
                line += " Allowed values: " + ", ".join(a["choices"]) + "."
            out.append(line)
        out.append("")
    # The commands deliberately left out are documented only in a trailing
    # comment block; that rationale is worth retrieving too.
    tail = raw.split("Deliberately NOT exposed", 1)
    if len(tail) == 2:
        lines = [ln.lstrip("#").strip() for ln in tail[1].splitlines()]
        body = " ".join(ln for ln in lines if ln and not set(ln) <= {"-"})
        out += ["## Commands not exposed as tools", "", "Deliberately NOT exposed" + body, ""]
    return "\n".join(out)


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> list[Doc]:
    docs = [Doc(p.name, p.read_text()) for p in sorted(corpus_dir.glob("*.md"))]
    docs.append(Doc(CLI_REFERENCE, render_commands()))
    return docs
