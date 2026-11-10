"""Local embeddings through Ollama, so indexing and evaluation cost nothing.

nomic-embed-text is trained with task prefixes: documents and queries are
embedded differently ("search_document: " vs "search_query: "). Leaving the
prefixes out still works but measurably hurts retrieval, so they are applied
here rather than left to the caller.
"""

from __future__ import annotations

import os

import httpx

DEFAULT_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("DOCSRAG_EMBED_MODEL", "nomic-embed-text")
DIM = 768  # nomic-embed-text


class OllamaEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, url: str = DEFAULT_URL,
                 batch: int = 32, timeout: float = 120.0):
        self.model, self.url, self.batch = model, url.rstrip("/"), batch
        self._client = httpx.Client(timeout=timeout)
        self._queries: dict[str, list[float]] = {}

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            r = self._client.post(f"{self.url}/api/embed",
                                  json={"model": self.model, "input": texts[i : i + self.batch]})
            if r.status_code == 404:
                raise RuntimeError(f"Ollama has no model {self.model!r}: run `ollama pull {self.model}`")
            r.raise_for_status()
            out += r.json()["embeddings"]
        return out

    def documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"search_document: {t}" for t in texts])

    def query(self, text: str) -> list[float]:
        # Cached: the evaluation asks every question once per retrieval mode.
        if text not in self._queries:
            self._queries[text] = self._embed([f"search_query: {text}"])[0]
        return self._queries[text]
