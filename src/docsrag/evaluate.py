"""Retrieval evaluation: recall@k, MRR and abstention, per configuration.

A configuration is a chunking strategy x a retrieval mode (2 x 3 = 6). Every
answerable question is asked of every configuration. Everything runs locally
and is deterministic, so a run costs nothing and repeats are pointless.

Abstention is scored separately: can the top cosine similarity tell an
answerable question from one the docs do not cover? The threshold is chosen
by leave-one-out, so no question is scored with a threshold fitted on itself.
With ~30 questions an in-sample threshold would flatter the result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from toolbench.report import wilson

from .chunking import Chunk
from .corpus import Doc
from .retrieve import MODES, Retriever

KS = (1, 3, 5)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass(frozen=True)
class Gold:
    source: str
    contains: str


@dataclass(frozen=True)
class Question:
    id: str
    style: str
    q: str
    gold: tuple[Gold, ...] = field(default=())  # empty: the docs do not answer it

    @property
    def answerable(self) -> bool:
        return bool(self.gold)

    def relevant(self, c: Chunk) -> bool:
        return any(c.source == g.source and _norm(g.contains) in _norm(c.text) for g in self.gold)


def load_questions(path: Path, docs: list[Doc]) -> list[Question]:
    by_source = {d.source: _norm(d.text) for d in docs}
    out = []
    for raw in yaml.safe_load(path.read_text())["questions"]:
        gold = () if raw["answer"] == "none" else tuple(Gold(**g) for g in raw["answer"])
        for g in gold:
            if g.source not in by_source:
                raise ValueError(f"{raw['id']}: unknown source {g.source!r}")
            if _norm(g.contains) not in by_source[g.source]:
                raise ValueError(f"{raw['id']}: {g.contains!r} is not in {g.source}")
        out.append(Question(raw["id"], raw["style"], raw["q"], gold))
    if len({q.id for q in out}) != len(out):
        raise ValueError("duplicate question ids")
    return out


def first_relevant_rank(q: Question, chunks: list[Chunk]) -> int | None:
    for rank, c in enumerate(chunks, start=1):
        if q.relevant(c):
            return rank
    return None


def reachable(q: Question, chunks: dict[str, Chunk]) -> bool:
    """False when chunking split every copy of the gold phrase across chunks."""
    return any(q.relevant(c) for c in chunks.values())


def run(retrievers: dict[str, Retriever], questions: list[Question], depth: int = 10) -> pd.DataFrame:
    """One row per (strategy, mode, question)."""
    rows = []
    for strategy, r in retrievers.items():
        for q in questions:
            for mode in MODES:
                res = r.search(q.q, mode=mode, k=depth)
                hits = [h.chunk for h in res.hits]
                rows.append({
                    "strategy": strategy, "mode": mode, "id": q.id, "style": q.style,
                    "answerable": q.answerable,
                    "rank": first_relevant_rank(q, hits) if q.answerable else None,
                    "reachable": reachable(q, r.chunks) if q.answerable else None,
                    "top_similarity": res.top_similarity,
                    "top_ids": [h.id for h in hits[:5]],
                })
    return pd.DataFrame(rows)


def _pct(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% ({100 * lo:.0f}–{100 * hi:.0f})"


def retrieval_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    ans = df[df["answerable"]]
    for (strategy, mode), g in ans.groupby(["strategy", "mode"], sort=False):
        n = len(g)
        rank = g["rank"]
        row = {"strategy": strategy, "mode": mode, "questions": n}
        for k in KS:
            hit = int((rank <= k).sum())
            row[f"recall@{k}"] = hit / n
            row[f"recall@{k} (95% CI)"] = _pct(hit, n)
        row["MRR@10"] = float((1 / rank).fillna(0).mean())
        para = g[g["style"] == "paraphrase"]
        row["recall@3 paraphrase"] = float((para["rank"] <= 3).mean()) if len(para) else float("nan")
        row["recall@3 direct"] = float((g[g["style"] == "direct"]["rank"] <= 3).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def loo_threshold(sims: list[float], answerable: list[bool]) -> list[bool]:
    """Leave-one-out abstention: for each question, fit a threshold on the
    others (the midpoint that best separates answerable from not, by balanced
    accuracy) and predict whether to abstain on the held-out one."""
    preds = []
    for i in range(len(sims)):
        xs = [(s, a) for j, (s, a) in enumerate(zip(sims, answerable)) if j != i]
        t = best_threshold(xs)
        preds.append(sims[i] < t)
    return preds


def best_threshold(xs: list[tuple[float, bool]]) -> float:
    vals = sorted({s for s, _ in xs})
    cands = [(a + b) / 2 for a, b in zip(vals, vals[1:])] or vals
    pos = [s for s, a in xs if a]
    neg = [s for s, a in xs if not a]

    def bal_acc(t: float) -> float:
        tpr = sum(s >= t for s in pos) / len(pos) if pos else 0  # answered when answerable
        tnr = sum(s < t for s in neg) / len(neg) if neg else 0  # abstained when not
        return (tpr + tnr) / 2

    return max(cands, key=lambda t: (bal_acc(t), -t))


def abstention_table(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    rows, thresholds = [], {}
    # top_similarity does not depend on mode; take one mode's rows per strategy
    for strategy, g in df[df["mode"] == "vector"].groupby("strategy", sort=False):
        sims, ans = g["top_similarity"].tolist(), g["answerable"].tolist()
        abstain = loo_threshold(sims, ans)
        n_ans, n_not = sum(ans), len(ans) - sum(ans)
        answered_ok = sum(1 for a, x in zip(ans, abstain) if a and not x)
        abstained_ok = sum(1 for a, x in zip(ans, abstain) if not a and x)
        thresholds[strategy] = best_threshold(list(zip(sims, ans)))
        rows.append({
            "strategy": strategy,
            "threshold (all questions)": round(thresholds[strategy], 3),
            "abstained when not covered": _pct(abstained_ok, n_not),
            "answered when covered": _pct(answered_ok, n_ans),
            "mean top similarity, covered": round(float(g[g["answerable"]]["top_similarity"].mean()), 3),
            "mean top similarity, not covered": round(float(g[~g["answerable"]]["top_similarity"].mean()), 3),
        })
    return pd.DataFrame(rows), thresholds


def write(df: pd.DataFrame, chunk_counts: dict[str, int], model: str, out: Path,
          results_dir: Path) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rdir = results_dir / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    df.to_json(rdir / "rows.jsonl", orient="records", lines=True)
    ret = retrieval_table(df)
    abst, thresholds = abstention_table(df)
    meta = {"run_id": run_id, "embed_model": model, "chunks": chunk_counts,
            "thresholds": thresholds, "questions": int(df["id"].nunique())}
    (rdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    csv = out / "csv"
    csv.mkdir(parents=True, exist_ok=True)
    ret.to_csv(csv / "rag_retrieval.csv", index=False)
    abst.to_csv(csv / "rag_abstention.csv", index=False)
    (out / "rag_thresholds.json").write_text(json.dumps(thresholds, indent=2) + "\n")

    show = ret[["strategy", "mode", *[f"recall@{k} (95% CI)" for k in KS], "MRR@10",
                "recall@3 direct", "recall@3 paraphrase"]].copy()
    for c in ("MRR@10", "recall@3 direct", "recall@3 paraphrase"):
        show[c] = show[c].map(lambda v: f"{v:.2f}" if c == "MRR@10" else f"{100 * v:.0f}%")
    ans = df[df["answerable"] & (df["mode"] == "vector")]
    unreachable = ans[~ans["reachable"].astype(bool)].groupby("strategy")["id"].apply(list).to_dict()
    misses = (df[df["answerable"] & ~(df["rank"] <= 3)]
              .groupby(["strategy", "mode"])["id"].apply(lambda s: ", ".join(s)).reset_index())

    n_ans = int(df[df["answerable"]]["id"].nunique())
    n_not = int(df[~df["answerable"]]["id"].nunique())
    lines = [
        f"# RAG track: retrieval over the vScheduler docs ({run_id})", "",
        f"{n_ans} answerable questions and {n_not} the docs do not cover; "
        f"embeddings from `{model}` (local, Ollama); exact search in pgvector. "
        "Chunks per strategy: " + ", ".join(f"{k} {v}" for k, v in chunk_counts.items()) + ".",
        "", "## Retrieval (answerable questions)", "",
        show.to_markdown(index=False), "",
        "Recall@k: the share of questions with a chunk containing the answer in the top k. "
        "MRR: mean of 1/rank of the first such chunk (0 if not in the top 10). "
        "Intervals are 95% Wilson intervals over questions.", "",
        "## Abstention (vector top similarity, leave-one-out threshold)", "",
        abst.to_markdown(index=False), "",
        "## Misses (answer not in the top 3)", "",
        misses.rename(columns={"id": "questions"}).to_markdown(index=False) if len(misses) else "None.",
        "",
    ]
    if unreachable:
        lines += ["Split by chunking (the answer phrase is in no single chunk): " +
                  "; ".join(f"{k}: {', '.join(v)}" for k, v in unreachable.items()), ""]
    path = out / "rag.md"
    path.write_text("\n".join(lines))
    return path


def run_and_write(retrievers: dict[str, Retriever], questions: list[Question], model: str,
                  out: Path, results_dir: Path) -> tuple[Path, pd.DataFrame]:
    df = run(retrievers, questions)
    counts = {s: len(r.chunks) for s, r in retrievers.items()}
    return write(df, counts, model, out, results_dir), df

