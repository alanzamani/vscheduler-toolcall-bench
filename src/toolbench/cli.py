"""toolbench tools | run | report"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import tools as tools_mod
from .cases import load_cases
from .report import export_csv, load_runs, score_frame, tables, write_report, write_site
from .runner import RunSettings, load_models, run

ROOT = Path(__file__).resolve().parents[2]


def _tools(path: Path | None) -> list[tools_mod.Tool]:
    if path:
        return tools_mod.load(path)
    return asyncio.run(tools_mod.fetch_from_mcp())


def cmd_tools(args) -> int:
    tools = _tools(None)
    if args.out:
        tools_mod.save(tools, args.out)
        print(f"wrote {len(tools)} tools to {args.out}")
    else:
        for t in tools:
            print(t.name)
    return 0


def cmd_run(args) -> int:
    tools = _tools(args.tools)
    specs = tools_mod.load_specs()
    cases = load_cases(args.cases, {t.name for t in tools}, specs)
    if args.only_cases:
        wanted = set(args.only_cases.split(","))
        cases = [c for c in cases if c.id in wanted or c.category in wanted]
    models = load_models(args.models)
    if args.only:
        wanted = set(args.only.split(","))
        models = [m for m in models if m.key in wanted]
    if not models or not cases:
        print("nothing to run: no models or no cases selected", file=sys.stderr)
        return 2
    settings = RunSettings(repeats=args.repeats, max_usd=args.max_usd, seed=args.seed,
                           max_attempts=args.max_attempts)
    system = args.system.read_text()
    print(f"{len(models)} model(s) x {len(cases)} case(s) x {settings.repeats} repeat(s), "
          f"cap ${settings.max_usd:.2f}")
    out = asyncio.run(run(models, cases, tools, system, settings, args.out))
    print(f"results in {out}")
    return 0


def cmd_report(args) -> int:
    df = load_runs(args.runs)
    if args.exclude:
        df = df[~df["model_key"].isin(args.exclude.split(","))]
    tools = tools_mod.load(args.runs[0] / "tools.json")
    specs = tools_mod.load_specs()
    cases = load_cases(args.cases, {t.name for t in tools}, specs)
    scored = score_frame(df, cases, specs, {t.name for t in tools})
    path = write_report(scored, args.out)
    t = tables(scored)
    export_csv(t, args.out / "csv")
    site = write_site(t, [r.name for r in args.runs], args.site or args.out / "index.html")
    print(f"report in {path}, CSVs in {args.out / 'csv'}, results page in {site}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolbench", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("tools", help="list the tools the MCP server exposes")
    t.add_argument("--out", type=Path, help="save them as JSON")
    t.set_defaults(fn=cmd_tools)

    r = sub.add_parser("run", help="send the cases to the models")
    r.add_argument("--models", type=Path, default=ROOT / "models.yaml")
    r.add_argument("--cases", type=Path, default=ROOT / "cases")
    r.add_argument("--system", type=Path, default=ROOT / "prompts" / "system.md")
    r.add_argument("--tools", type=Path, help="tools JSON instead of asking the MCP server")
    r.add_argument("--only", help="comma-separated model keys")
    r.add_argument("--only-cases", help="comma-separated case ids or categories")
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--max-attempts", type=int, default=5,
                   help="raise for free tiers with per-minute quotas (backoff caps at 60 s)")
    r.add_argument("--max-usd", type=float, default=5.0)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--out", type=Path, default=ROOT / "results")
    r.set_defaults(fn=cmd_run)

    rep = sub.add_parser("report", help="score runs and write report.md")
    rep.add_argument("runs", type=Path, nargs="+", help="one or more results/<run_id> dirs")
    rep.add_argument("--cases", type=Path, default=ROOT / "cases")
    rep.add_argument("--out", type=Path, default=ROOT / "report")
    rep.add_argument("--exclude", help="comma-separated model keys to leave out")
    rep.add_argument("--site", type=Path,
                     help="where to write the HTML results page (default: <out>/index.html); "
                          "docs/index.html is what GitHub Pages serves")
    rep.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
