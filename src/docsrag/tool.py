"""The `search_docs` MCP tool: retrieval over the vScheduler docs, with sources.

Configuration follows the evaluation (report/rag.md): heading-aware chunks,
vector search, five passages. Five rather than three because recall@5 is
noticeably higher than recall@3, and the calling model reads the passages
anyway.

Low-similarity results are not hidden. The evaluation shows the similarity
threshold separates covered from uncovered questions only roughly, so hiding
results below it would also hide real answers. Instead the tool says plainly
that the match is weak and tells the model not to answer from passages that
do not state the answer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .corpus import ROOT
from .retrieve import Result

STRATEGY = os.getenv("DOCSRAG_STRATEGY", "heading")
MODE = os.getenv("DOCSRAG_MODE", "vector")
K = int(os.getenv("DOCSRAG_K", "5"))
FALLBACK_THRESHOLD = 0.69


def threshold(strategy: str = STRATEGY) -> float:
    """The threshold fitted by the last `docsrag eval`, else a fixed default."""
    override = os.getenv("DOCSRAG_THRESHOLD")
    if override:
        return float(override)
    path = ROOT / "report" / "rag_thresholds.json"
    try:
        return float(json.loads(Path(path).read_text())[strategy])
    except (OSError, KeyError, ValueError):
        return FALLBACK_THRESHOLD


def format_result(question: str, res: Result, limit: float, max_chars: int = 1200) -> str:
    weak = res.abstain(limit)
    head = (
        f"WEAK MATCH (best similarity {res.top_similarity:.2f} < {limit:.2f}): the "
        "vScheduler docs probably do not cover this. Tell the user so; do not answer "
        "from these passages unless one states the answer outright."
        if weak else
        f"Passages from the vScheduler docs (best similarity {res.top_similarity:.2f}). "
        "Answer only from them and cite the source in brackets."
    )
    parts = [head, f"Question: {question}"]
    for i, h in enumerate(res.hits, start=1):
        c = h.chunk
        text = c.text if len(c.text) <= max_chars else c.text[:max_chars] + " …"
        parts.append(f"[{i}] {c.heading or c.source}\n{text}")
    return "\n\n".join(parts)


def register(mcp) -> None:
    """Add `search_docs` to a FastMCP/MCPServer instance."""
    from mcp.types import ToolAnnotations

    from .embed import OllamaEmbedder
    from .retrieve import Retriever
    from .store import PgStore

    retriever = Retriever(PgStore(), OllamaEmbedder(), STRATEGY)
    limit = threshold()

    @mcp.tool(
        name="search_docs",
        description=(
            "Search the vScheduler documentation (deployment, MCP server, Ansible, "
            "admin install, release notes, CLI reference) and return the most relevant "
            "passages with their sources. Use it for how-to and why questions about "
            "vScheduler before answering from memory. Read-only."
        ),
        annotations=ToolAnnotations(title="search_docs", read_only_hint=True,
                                    destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def search_docs(question: str) -> str:
        return format_result(question, retriever.search(question, mode=MODE, k=K), limit)
