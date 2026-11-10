"""Two chunking strategies, compared head to head in the evaluation.

- `fixed`: windows of N words with overlap. The common baseline: it ignores
  document structure, so a window can start mid-section and carry no hint of
  what it is about.
- `heading`: one chunk per Markdown section, split further at paragraph
  boundaries when a section is long. Each chunk is embedded with its heading
  path ("mcp-server.md > Tools"), so a short table still says what it is a
  table of.

Headings inside fenced code blocks (`# comment` in a shell snippet) are not
section breaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .corpus import Doc

STRATEGIES = ("fixed", "heading")
_HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    id: str  # "<source>#<n>", stable for a given corpus and strategy
    source: str
    heading: str  # heading path, empty for fixed windows
    text: str

    @property
    def embed_text(self) -> str:
        """What gets embedded and keyword-indexed: heading path plus body."""
        return f"{self.heading}\n\n{self.text}" if self.heading else self.text


def fixed(doc: Doc, size: int = 200, overlap: int = 40) -> list[Chunk]:
    if not 0 <= overlap < size:
        raise ValueError("need 0 <= overlap < size")
    words = doc.text.split()
    chunks, start = [], 0
    while start < len(words):
        body = " ".join(words[start : start + size])
        chunks.append(Chunk(f"{doc.source}#{len(chunks)}", doc.source, "", body))
        if start + size >= len(words):
            break
        start += size - overlap
    return chunks


def _sections(text: str) -> list[tuple[list[str], str]]:
    """Split Markdown into (heading path, body) pairs, ignoring code fences."""
    path: list[str] = []
    sections: list[tuple[list[str], str]] = []
    body: list[str] = []
    in_fence = False

    def flush():
        if "".join(body).strip():
            sections.append((list(path), "\n".join(body).strip()))
        body.clear()

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip("* ").strip()
            path[:] = path[: level - 1] + [title]
        else:
            body.append(line)
    flush()
    return sections


def _paragraphs(body: str) -> list[str]:
    """Blank-line-separated blocks; a fenced code block stays in one piece."""
    blocks, cur, in_fence = [], [], False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def heading(doc: Doc, max_words: int = 250) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path, body in _sections(doc.text):
        head = " > ".join([doc.source, *path])
        parts, cur, n = [], [], 0
        for para in _paragraphs(body):
            w = len(para.split())
            if cur and n + w > max_words:
                parts.append("\n\n".join(cur))
                cur, n = [], 0
            cur.append(para)
            n += w
        if cur:
            parts.append("\n\n".join(cur))
        for p in parts:
            chunks.append(Chunk(f"{doc.source}#{len(chunks)}", doc.source, head, p))
    return chunks


def chunk(docs: list[Doc], strategy: str) -> list[Chunk]:
    if strategy == "fixed":
        return [c for d in docs for c in fixed(d)]
    if strategy == "heading":
        return [c for d in docs for c in heading(d)]
    raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
