"""pgvector store: one table holds every chunking strategy side by side.

Search is exact (sequential scan, no HNSW/IVFFlat index). At a few hundred
chunks a scan takes milliseconds, and exact search means the evaluation
measures the embeddings and chunking, not an approximate index's recall.

Keyword search uses Postgres full-text search over the same text. Questions
are turned into an OR query (any stemmed term may match) because the default
AND semantics return nothing for most natural-language questions.
"""

from __future__ import annotations

import os

import psycopg

from .chunking import Chunk
from .embed import DIM

DEFAULT_DSN = os.getenv("VSCHEDULER_DOCS_DSN", "postgresql://rag:rag@localhost:5433/rag")

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS chunks (
    strategy  text NOT NULL,
    id        text NOT NULL,
    source    text NOT NULL,
    heading   text NOT NULL,
    body      text NOT NULL,
    embedding vector({DIM}) NOT NULL,
    tsv       tsvector GENERATED ALWAYS AS
              (to_tsvector('english', heading || ' ' || body)) STORED,
    PRIMARY KEY (strategy, id)
);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv);
"""


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class PgStore:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.conn = psycopg.connect(dsn, autocommit=True)

    def init(self) -> None:
        self.conn.execute(SCHEMA)

    def replace(self, strategy: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Swap one strategy's chunks atomically, so a re-index never half-applies."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE strategy = %s", (strategy,))
            cur.executemany(
                "INSERT INTO chunks (strategy, id, source, heading, body, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                [(strategy, c.id, c.source, c.heading, c.text, _vec(v))
                 for c, v in zip(chunks, vectors, strict=True)],
            )

    def count(self, strategy: str) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM chunks WHERE strategy = %s", (strategy,)).fetchone()[0]

    def chunks(self, strategy: str) -> dict[str, Chunk]:
        rows = self.conn.execute(
            "SELECT id, source, heading, body FROM chunks WHERE strategy = %s", (strategy,))
        return {r[0]: Chunk(*r) for r in rows}

    def vector_search(self, strategy: str, qvec: list[float], k: int) -> list[tuple[str, float]]:
        """Top k by cosine similarity (1 - cosine distance)."""
        rows = self.conn.execute(
            "SELECT id, 1 - (embedding <=> %s::vector) AS sim FROM chunks "
            "WHERE strategy = %s ORDER BY embedding <=> %s::vector LIMIT %s",
            (_vec(qvec), strategy, _vec(qvec), k),
        )
        return [(r[0], float(r[1])) for r in rows]

    def keyword_search(self, strategy: str, query: str, k: int) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            """
            WITH q AS (
              SELECT to_tsquery('english',
                       replace(plainto_tsquery('english', %s)::text, '&', '|')) AS tsq
            )
            SELECT id, ts_rank_cd(tsv, q.tsq) AS rank FROM chunks, q
            WHERE strategy = %s AND q.tsq::text <> '' AND tsv @@ q.tsq
            ORDER BY rank DESC, id LIMIT %s
            """,
            (query, strategy, k),
        )
        return [(r[0], float(r[1])) for r in rows]
