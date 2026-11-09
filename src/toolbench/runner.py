"""Send every case to every model, with retries, timeouts and a spending cap.

One row is written per (model, case, repeat) as soon as it finishes, to
raw.jsonl, so a crash or Ctrl-C keeps everything already paid for. Scoring
happens later in the report, so cases can be corrected and re-scored without
paying for the calls again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import yaml

from . import __version__
from .cases import Case
from .providers.base import Completion, Provider, RetryableError
from .tools import Tool


@dataclass(frozen=True)
class ModelConfig:
    key: str                     # short name used in the report
    provider: str                # anthropic | openai | oracle
    model: str
    price_in: float = 0.0        # USD per million input tokens
    price_out: float = 0.0       # USD per million output tokens
    concurrency: int = 4         # requests in flight for this model
    base_url: str | None = None
    api_key_env: str | None = None
    params: dict = field(default_factory=dict)
    # Models in the same group share one concurrency limit (the smallest of
    # theirs). Models on a local server are grouped by default: they share
    # one machine's memory and GPU, so running two at once measures the
    # contention, not the models.
    group: str | None = None

    @property
    def limit_group(self) -> str:
        if self.group:
            return self.group
        if self.base_url and urlparse(self.base_url).hostname in ("localhost", "127.0.0.1", "::1"):
            return self.base_url
        return self.key

    def cost(self, c: Completion) -> float:
        return ((c.input_tokens or 0) * self.price_in + (c.output_tokens or 0) * self.price_out) / 1e6


@dataclass(frozen=True)
class RunSettings:
    repeats: int = 3
    max_attempts: int = 5
    attempt_timeout_s: float = 120.0
    max_usd: float = 5.0
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    seed: int = 0


def load_models(path: Path) -> list[ModelConfig]:
    raw = yaml.safe_load(path.read_text()) or {}
    models = [ModelConfig(**m) for m in raw.get("models") or []]
    keys = [m.key for m in models]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate model keys in {path}")
    return models


def make_provider(cfg: ModelConfig, cases: list[Case]) -> Provider:
    if cfg.provider == "anthropic":
        from .providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg.model, params=cfg.params)
    if cfg.provider == "openai":
        from .providers.openai_provider import OpenAIProvider

        return OpenAIProvider(
            cfg.model,
            base_url=cfg.base_url,
            api_key_env=cfg.api_key_env or "OPENAI_API_KEY",
            params=cfg.params,
        )
    if cfg.provider == "oracle":
        from .providers.oracle import OracleProvider

        return OracleProvider(cases)
    raise ValueError(f"{cfg.key}: unknown provider '{cfg.provider}'")


class Budget:
    """Stops new requests once estimated spend reaches the cap.

    Requests already in flight still finish, so a run can overshoot by up to
    (concurrency x cost of one call). With these prompts that is cents.
    """

    def __init__(self, cap_usd: float) -> None:
        self.cap = cap_usd
        self.spent = 0.0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.cap

    def charge(self, usd: float) -> None:
        self.spent += usd


@dataclass
class Attempted:
    completion: Completion | None
    attempts: int
    error: str | None
    wall_s: float         # including every failed attempt and backoff
    backoff_s: float


def backoff_delay(attempt: int, settings: RunSettings, retry_after: float | None) -> float:
    """Exponential backoff with jitter, or the server's Retry-After if it sent one."""
    if retry_after is not None:
        return min(retry_after, settings.backoff_cap_s)
    ceiling = min(settings.backoff_cap_s, settings.backoff_base_s * 2 ** (attempt - 1))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


async def call_with_retries(
    provider: Provider,
    *,
    system: str,
    prompt: str,
    tools: list[Tool],
    settings: RunSettings,
    sleep=asyncio.sleep,
) -> Attempted:
    start = time.perf_counter()
    waited = 0.0
    error = None
    for attempt in range(1, settings.max_attempts + 1):
        try:
            completion = await asyncio.wait_for(
                provider.complete(system=system, prompt=prompt, tools=tools),
                timeout=settings.attempt_timeout_s,
            )
            return Attempted(completion, attempt, None, time.perf_counter() - start, waited)
        except (RetryableError, asyncio.TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt == settings.max_attempts:
                break
            delay = backoff_delay(attempt, settings, getattr(exc, "retry_after", None))
            waited += delay
            await sleep(delay)
        except Exception as exc:  # noqa: BLE001 -- recorded on the row, not fatal to the run
            return Attempted(None, attempt, f"{type(exc).__name__}: {exc}",
                             time.perf_counter() - start, waited)
    return Attempted(None, settings.max_attempts, error, time.perf_counter() - start, waited)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


async def run(
    models: list[ModelConfig],
    cases: list[Case],
    tools: list[Tool],
    system: str,
    settings: RunSettings,
    out_root: Path,
    providers: dict[str, Provider] | None = None,
    sleep=asyncio.sleep,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root.mkdir(parents=True, exist_ok=True)
    # Two runs started in the same second (hosted and local models in
    # separate terminals, say) get -2, -3, ... rather than colliding.
    n = 1
    while True:
        run_id = stamp if n == 1 else f"{stamp}-{n}"
        out = out_root / run_id
        try:
            out.mkdir()
            break
        except FileExistsError:
            n += 1
    tools_json = json.dumps([asdict(t) for t in tools], indent=2)
    (out / "tools.json").write_text(tools_json + "\n")
    (out / "system.md").write_text(system)

    providers = providers or {m.key: make_provider(m, cases) for m in models}
    sizes: dict[str, int] = {}
    for m in models:
        sizes[m.limit_group] = min(sizes.get(m.limit_group, m.concurrency), m.concurrency)
    limits = {g: asyncio.Semaphore(n) for g, n in sizes.items()}
    budget = Budget(settings.max_usd)

    # Shuffled so that no model or category is systematically sent first,
    # when rate limits are fresh, or last, when they are not.
    jobs = [(m, c, r) for m in models for c in cases for r in range(settings.repeats)]
    random.Random(settings.seed).shuffle(jobs)

    raw_path = out / "raw.jsonl"
    rows: list[dict] = []
    write_lock = asyncio.Lock()

    async def one(model: ModelConfig, case: Case, repeat: int) -> None:
        async with limits[model.limit_group]:
            row = {
                "run_id": run_id,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "model_key": model.key,
                "provider": model.provider,
                "model": model.model,
                "case_id": case.id,
                "category": case.category,
                "repeat": repeat,
            }
            if budget.exhausted:
                row |= {"attempts": 0, "error": "skipped: spending cap reached"}
            else:
                result = await call_with_retries(
                    providers[model.key], system=system, prompt=case.prompt,
                    tools=tools, settings=settings, sleep=sleep,
                )
                c = result.completion
                cost = model.cost(c) if c else 0.0
                budget.charge(cost)
                row |= {
                    "attempts": result.attempts,
                    "error": result.error,
                    "wall_s": result.wall_s,
                    "backoff_s": result.backoff_s,
                    "cost_usd": cost,
                }
                if c:
                    row |= {
                        "stop_reason": c.stop_reason,
                        "ttft_s": c.ttft_s,
                        "ttfo_s": c.ttfo_s,
                        "total_s": c.total_s,
                        "input_tokens": c.input_tokens,
                        "output_tokens": c.output_tokens,
                        "calls": json.dumps([{"name": t.name, "args": t.args} for t in c.calls]),
                        "text": c.text[:2000],
                    }
        async with write_lock:
            rows.append(row)
            with raw_path.open("a") as fh:
                fh.write(json.dumps(row, default=str) + "\n")

    started = time.time()
    await asyncio.gather(*(one(m, c, r) for m, c, r in jobs))

    pd.DataFrame(rows).to_parquet(out / "raw.parquet", index=False)
    meta = {
        "run_id": run_id,
        "toolbench_version": __version__,
        "duration_s": round(time.time() - started, 1),
        "spent_usd": round(budget.spent, 4),
        "settings": asdict(settings),
        # Never the API keys: only which env var each model reads.
        "models": [asdict(m) for m in models],
        "cases": len(cases),
        "system_prompt_sha": _sha(system),
        "tools_sha": _sha(tools_json),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return out
