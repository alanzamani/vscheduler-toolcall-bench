"""Score raw runs and write report.md plus charts.

Rows that failed at the API (after all retries) are reported as reliability,
not counted as wrong answers: a timeout says nothing about whether the model
would have picked the right tool.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .cases import SAFETY_CATEGORIES, Case
from .providers.base import ToolCall
from .score import score

ACCENT = "#2a78d6"
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#e4e3df"


def load_runs(run_dirs: list[Path]) -> pd.DataFrame:
    frames = []
    for d in run_dirs:
        parquet, jsonl = d / "raw.parquet", d / "raw.jsonl"
        if parquet.exists():
            frames.append(pd.read_parquet(parquet))
        elif jsonl.exists():  # an interrupted run never wrote parquet
            frames.append(pd.read_json(jsonl, lines=True))
        else:
            raise FileNotFoundError(f"no raw.parquet or raw.jsonl in {d}")
    return pd.concat(frames, ignore_index=True)


def score_frame(df: pd.DataFrame, cases: list[Case], specs, tool_names: set[str]) -> pd.DataFrame:
    by_id = {c.id: c for c in cases}
    df = df.copy()
    answered = df["error"].isna() if "error" in df else pd.Series(True, index=df.index)
    df["answered"] = answered
    for col in ("outcome", "correct", "unsafe", "valid", "acted"):
        df[col] = None
    for i in df.index[answered]:
        case = by_id.get(df.at[i, "case_id"])
        if case is None:  # case since removed from the set
            df.at[i, "answered"] = False
            continue
        calls = [ToolCall(c["name"], c["args"]) for c in json.loads(df.at[i, "calls"] or "[]")]
        s = score(case, calls, specs, tool_names)
        df.loc[i, ["outcome", "correct", "unsafe", "valid", "acted"]] = [
            s.outcome, s.correct, s.unsafe, s.valid, s.acted
        ]
    return df


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion; sane at 0% and 100%."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate(series: pd.Series) -> str:
    n, k = len(series), int(series.astype(bool).sum())
    if n == 0:
        return "–"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% ({100 * lo:.0f}–{100 * hi:.0f})"


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby("model_key", sort=True):
        ok = g[g["answered"]]
        safety = ok[ok["category"].isin(SAFETY_CATEGORIES)]
        called = ok[ok["valid"].notna()]
        gen_s = (ok["total_s"] - ok["ttft_s"]).where(lambda s: s > 0)
        tok_s = (ok["output_tokens"] / gen_s).dropna()
        n_correct = int(ok["correct"].astype(bool).sum())
        spent = g["cost_usd"].fillna(0).sum()
        rows.append({
            "model": key,
            "n": len(g),
            "api failures": f"{100 * (1 - len(ok) / len(g)):.1f}%",
            "accuracy [95% CI]": _rate(ok["correct"]),
            "safety pass": _rate(~safety["unsafe"].astype(bool)),
            "allowlist would reject": _rate(~called["valid"].astype(bool)),
            "median TTFO s": round(ok["ttfo_s"].median(), 2),
            "p90 total s": round(ok["total_s"].quantile(0.9), 2),
            "median out tok/s": round(tok_s.median(), 1) if len(tok_s) else None,
            "cost USD": round(spent, 4),
            "USD per correct": round(spent / n_correct, 5) if n_correct else None,
        })
    return pd.DataFrame(rows)


def by_category(df: pd.DataFrame) -> pd.DataFrame:
    ok = df[df["answered"]]
    table = ok.pivot_table(index="model_key", columns="category", values="correct",
                           aggfunc=lambda s: round(100 * s.astype(bool).mean()))
    return table.rename_axis(index="model", columns=None)


def acted_on_change(df: pd.DataFrame) -> pd.Series:
    """On explicit change requests, asking first is accepted. How often each
    model acted instead shows whether its accuracy there comes from caution."""
    ok = df[df["answered"] & (df["category"] == "change")]
    return (100 * ok.groupby("model_key")["acted"].apply(lambda s: s.astype(bool).mean())).round()


def _axes(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def charts(df: pd.DataFrame, out: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, NullFormatter

    ok = df[df["answered"]]
    written = []

    # Accuracy against cost per correct answer. One hue: each point is named
    # by its label, so identity never depends on telling colours apart.
    points = []
    for key, g in ok.groupby("model_key"):
        n_correct = int(g["correct"].astype(bool).sum())
        if n_correct:
            points.append((key, 100 * n_correct / len(g), g["cost_usd"].fillna(0).sum() / n_correct))
    paid = [p[2] for p in points if p[2] > 0]
    if paid:
        fig, ax = plt.subplots(figsize=(6.4, 4))
        _axes(ax)
        # A log axis has no zero, so free (local) models get their own
        # column left of the cheapest paid one, behind a dashed line.
        free_x = min(paid) / 6
        has_free = len(paid) < len(points)
        if has_free:
            ax.axvline(free_x * 2.4, color=GRID, linestyle="--", linewidth=1)
        labelled: list[tuple[float, float]] = []
        for key, acc, cpc in sorted(points, key=lambda p: -p[1]):
            x = cpc if cpc > 0 else free_x
            ax.scatter(x, acc, s=64, color=ACCENT, edgecolor="white", linewidth=2, zorder=3)
            # Below the dot when a label above would meet a neighbour's.
            crowded = any(abs(math.log10(x / lx)) < 0.8 and abs(acc - la) < 2.5 for lx, la in labelled)
            ax.annotate(key, (x, acc), xytext=(7, -13 if crowded else 4), textcoords="offset points",
                        fontsize=9, color=INK)
            labelled.append((x, acc))
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:g}"))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.margins(x=0.25, y=0.1)
        if has_free:
            lo, hi = ax.get_xlim()
            ticks = [t for t in ax.get_xticks() if free_x * 2.4 < t <= hi]
            ax.set_xticks([free_x, *ticks], ["free", *(f"${t:g}" for t in ticks)])
            ax.set_xlim(lo, hi)
        ax.set_xlabel("USD per correct answer (log scale)", color=INK_MUTED)
        ax.set_ylabel("Accuracy (%)", color=INK_MUTED)
        ax.set_title("Accuracy vs cost", loc="left", color=INK, fontsize=12)
        fig.tight_layout()
        fig.savefig(out / "accuracy_vs_cost.png", dpi=160)
        plt.close(fig)
        written.append("accuracy_vs_cost.png")

    # Spread of total response time per model.
    keys = sorted(ok["model_key"].unique())
    series = [ok.loc[ok["model_key"] == k, "total_s"].dropna() for k in keys]
    if any(len(s) for s in series):
        fig, ax = plt.subplots(figsize=(6.4, 0.5 * len(keys) + 1.6))
        _axes(ax)
        ax.boxplot(series, orientation="horizontal", widths=0.5, showfliers=True,
                   boxprops={"color": ACCENT, "linewidth": 1.5},
                   medianprops={"color": INK, "linewidth": 2},
                   whiskerprops={"color": ACCENT}, capprops={"color": ACCENT},
                   flierprops={"marker": "o", "markersize": 3, "markerfacecolor": ACCENT,
                               "markeredgecolor": "none", "alpha": 0.5})
        ax.set_yticks(range(1, len(keys) + 1), keys)
        ax.invert_yaxis()  # alphabetical, top to bottom
        ax.set_xlabel("Total response time (s)", color=INK_MUTED)
        ax.set_title("Response time per request", loc="left", color=INK, fontsize=12)
        fig.tight_layout()
        fig.savefig(out / "response_time.png", dpi=160)
        plt.close(fig)
        written.append("response_time.png")
    return written


def write_report(df: pd.DataFrame, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out / "scored.parquet", index=False)
    images = charts(df, out)
    acted = acted_on_change(df)

    lines = [
        "# vScheduler tool-calling benchmark",
        "",
        f"Runs: {', '.join(sorted(df['run_id'].unique()))}. "
        f"{df['case_id'].nunique()} cases, {len(df)} requests.",
        "",
        "Rates show the 95% Wilson interval in brackets. *Safety pass* covers the "
        "dangerous, out-of-scope and injection cases: the share where the model made "
        "no mutating call that the request did not justify. *Allowlist would reject* "
        "is the share of tool-calling answers that the MCP executor's commands.yaml "
        "checks would have stopped.",
        "",
        "## Summary",
        "",
        summarise(df).to_markdown(index=False),
        "",
        "## Accuracy by category (%)",
        "",
        by_category(df).to_markdown(),
        "",
        "## Acted vs asked on explicit change requests",
        "",
        "Asking for confirmation is accepted on these, so a model can score well by "
        "never acting. Share of answers that made the change:",
        "",
        acted.rename("acted %").to_frame().rename_axis("model").to_markdown(),
        "",
    ]
    for image in images:
        lines += [f"![{image}]({image})", ""]
    path = out / "report.md"
    path.write_text("\n".join(lines))
    return path


def _host(key: str, provider: str) -> str:
    # The adapter name ("openai") is a protocol, not who serves the model.
    if provider == "anthropic":
        return "Anthropic"
    if key.endswith("@ollama"):
        return "Local (Ollama)"
    if key.startswith("gemini"):
        return "Google"
    return provider


def export_csv(df: pd.DataFrame, out: Path) -> list[Path]:
    """Numeric tables for other tools (the HTML results page reads these)."""
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for key, g in df.groupby("model_key", sort=True):
        ok = g[g["answered"]]
        safety = ok[ok["category"].isin(SAFETY_CATEGORIES)]
        called = ok[ok["valid"].notna()]
        change = ok[ok["category"] == "change"]
        k, n = int(ok["correct"].astype(bool).sum()), len(ok)
        sk, sn = int((~safety["unsafe"].astype(bool)).sum()), len(safety)
        lo, hi = wilson(k, n)
        slo, shi = wilson(sk, sn)
        spent = g["cost_usd"].fillna(0).sum()
        summary.append({
            "model": key,
            "host": _host(key, g["provider"].iloc[0]),
            "requests": len(g),
            "api_failure_pct": round(100 * (1 - n / len(g)), 1),
            "accuracy_pct": round(100 * k / n, 1) if n else None,
            "accuracy_lo": round(100 * lo, 1), "accuracy_hi": round(100 * hi, 1),
            "safety_pct": round(100 * sk / sn, 1) if sn else None,
            "safety_lo": round(100 * slo, 1), "safety_hi": round(100 * shi, 1),
            "unsafe_calls": int(ok["unsafe"].astype(bool).sum()),
            "allowlist_reject_pct": round(100 * (~called["valid"].astype(bool)).mean(), 1) if len(called) else None,
            "acted_on_change_pct": round(100 * change["acted"].astype(bool).mean(), 1) if len(change) else None,
            "median_ttfo_s": round(ok["ttfo_s"].median(), 2),
            "p90_total_s": round(ok["total_s"].quantile(0.9), 2),
            "cost_usd": round(spent, 4),
            "usd_per_1k_correct": round(1000 * spent / k, 3) if k else None,
        })
    paths = [out / "summary.csv", out / "categories.csv", out / "requests.csv"]
    pd.DataFrame(summary).to_csv(paths[0], index=False)
    ok = df[df["answered"]]
    cats = (ok.groupby(["model_key", "category"])["correct"]
            .agg(lambda s: round(100 * s.astype(bool).mean(), 1)).reset_index()
            .rename(columns={"model_key": "model", "correct": "accuracy_pct"}))
    cats.to_csv(paths[1], index=False)
    cols = ["model_key", "case_id", "category", "repeat", "outcome", "ttfo_s", "total_s", "cost_usd"]
    req = df[cols].rename(columns={"model_key": "model"})
    req = req.assign(outcome=req["outcome"].fillna("api_failure"))
    req.round({"ttfo_s": 3, "total_s": 3, "cost_usd": 6}).to_csv(paths[2], index=False)
    return paths
