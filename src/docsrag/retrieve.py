"""Retrieval modes, fusion and the abstention rule."""

from __future__ import annotations

from dataclasses import dataclass

from .chunking import Chunk
from .embed import OllamaEmbedder
from .store import PgStore

MODES = ("vector", "keyword", "hybrid")
RRF_K = 60  # the constant from the original reciprocal rank fusion paper
CANDIDATES = 20  # how deep each list goes before fusion


def rrf(*rankings: list[str], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: score = sum of 1 / (k + rank) over the lists.

    Uses ranks only, so a cosine similarity and a ts_rank never have to be put
    on the same scale.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class Result:
    hits: list[Hit]
    top_similarity: float  # best cosine similarity of the query to any chunk

    def abstain(self, threshold: float) -> bool:
        return self.top_similarity < threshold


class Retriever:
    def __init__(self, store: PgStore, embedder: OllamaEmbedder, strategy: str):
        self.store, self.embedder, self.strategy = store, embedder, strategy
        self.chunks = store.chunks(strategy)
        if not self.chunks:
            raise RuntimeError(f"no chunks for strategy {strategy!r}: run `docsrag index` first")

    def search(self, query: str, mode: str = "hybrid", k: int = 5) -> Result:
        # The vector side always runs: its best similarity is the abstention
        # signal, whichever mode ranks the results.
        qvec = self.embedder.query(query)
        vec = self.store.vector_search(self.strategy, qvec, CANDIDATES)
        top_sim = vec[0][1] if vec else 0.0
        if mode == "vector":
            ranked = vec
        elif mode == "keyword":
            ranked = self.store.keyword_search(self.strategy, query, CANDIDATES)
        elif mode == "hybrid":
            kw = self.store.keyword_search(self.strategy, query, CANDIDATES)
            ranked = rrf([c for c, _ in vec], [c for c, _ in kw])
        else:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        return Result([Hit(self.chunks[c], s) for c, s in ranked[:k]], top_sim)
