"""docsrag index | eval | search"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .chunking import STRATEGIES, chunk
from .corpus import ROOT, load_corpus
from .embed import DEFAULT_MODEL, OllamaEmbedder
from .evaluate import load_questions, run_and_write
from .retrieve import MODES, Retriever
from .store import DEFAULT_DSN, PgStore


def cmd_index(args) -> int:
    docs = load_corpus()
    store, emb = PgStore(args.dsn), OllamaEmbedder(args.model)
    store.init()
    for s in args.strategies.split(","):
        chunks = chunk(docs, s)
        store.replace(s, chunks, emb.documents([c.embed_text for c in chunks]))
        print(f"{s}: {len(chunks)} chunks from {len(docs)} documents")
    return 0


def cmd_eval(args) -> int:
    docs = load_corpus()
    questions = load_questions(args.questions, docs)
    store, emb = PgStore(args.dsn), OllamaEmbedder(args.model)
    retrievers = {s: Retriever(store, emb, s) for s in STRATEGIES}
    path, _ = run_and_write(retrievers, questions, args.model, args.out, args.results)
    print(path.read_text())
    return 0


def cmd_search(args) -> int:
    r = Retriever(PgStore(args.dsn), OllamaEmbedder(args.model), args.strategy)
    res = r.search(args.query, mode=args.mode, k=args.k)
    print(f"top similarity {res.top_similarity:.3f}")
    for h in res.hits:
        c = h.chunk
        print(f"\n[{h.score:.3f}] {c.heading or c.source}  ({c.id})\n{c.text[:400]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="docsrag", description=__doc__)
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--model", default=DEFAULT_MODEL)
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("index", help="chunk and embed the corpus into pgvector")
    i.add_argument("--strategies", default=",".join(STRATEGIES))
    i.set_defaults(fn=cmd_index)

    e = sub.add_parser("eval", help="score retrieval and abstention, write report/rag.md")
    e.add_argument("--questions", type=Path, default=ROOT / "rag" / "questions.yaml")
    e.add_argument("--out", type=Path, default=ROOT / "report")
    e.add_argument("--results", type=Path, default=ROOT / "results" / "rag")
    e.set_defaults(fn=cmd_eval)

    s = sub.add_parser("search", help="ask one question")
    s.add_argument("query")
    s.add_argument("--strategy", choices=STRATEGIES, default="heading")
    s.add_argument("--mode", choices=MODES, default="hybrid")
    s.add_argument("-k", type=int, default=3)
    s.set_defaults(fn=cmd_search)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
