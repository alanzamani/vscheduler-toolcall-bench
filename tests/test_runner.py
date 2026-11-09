import asyncio
import json

import pandas as pd

from toolbench.providers.base import Completion, RetryableError, ToolCall
from toolbench.providers.oracle import OracleProvider
from toolbench.report import load_runs, score_frame, summarise, write_report
from toolbench.runner import ModelConfig, RunSettings, call_with_retries, run


class Flaky:
    """Fails with the given exceptions in order, then answers."""

    def __init__(self, *failures, completion=None):
        self.failures = list(failures)
        self.calls = 0
        self.completion = completion or Completion(calls=[ToolCall("vinfo", {})], total_s=0.1,
                                                   input_tokens=1000, output_tokens=100)

    async def complete(self, **_):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.completion


class Slow:
    async def complete(self, **_):
        await asyncio.sleep(10)


async def no_sleep(_):
    pass


async def test_retries_rate_limits_then_succeeds():
    p = Flaky(RetryableError("429"), RetryableError("529", retry_after=2.0))
    r = await call_with_retries(p, system="", prompt="", tools=[], settings=RunSettings(), sleep=no_sleep)
    assert r.completion is not None and r.attempts == 3 and r.error is None
    assert r.backoff_s >= 2.0   # honoured Retry-After


async def test_gives_up_after_max_attempts():
    p = Flaky(*[RetryableError("429")] * 10)
    r = await call_with_retries(p, system="", prompt="", tools=[],
                                settings=RunSettings(max_attempts=3), sleep=no_sleep)
    assert r.completion is None and r.attempts == 3 and "429" in r.error and p.calls == 3


async def test_does_not_retry_a_bad_request():
    p = Flaky(ValueError("400 bad request"))
    r = await call_with_retries(p, system="", prompt="", tools=[], settings=RunSettings(), sleep=no_sleep)
    assert r.completion is None and r.attempts == 1 and p.calls == 1


async def test_timeout_is_retried():
    r = await call_with_retries(Slow(), system="", prompt="", tools=[],
                                settings=RunSettings(max_attempts=2, attempt_timeout_s=0.01),
                                sleep=no_sleep)
    assert r.completion is None and r.attempts == 2 and "TimeoutError" in r.error


async def test_spending_cap_skips_remaining_requests(tmp_path, cases, tools):
    # 1000 in + 100 out at $1000/M each = $1.10 per call; cap $2 allows two,
    # then the rest are skipped. Concurrency 1 makes that exact.
    model = ModelConfig("pricey", "fake", "fake", price_in=1000, price_out=1000, concurrency=1)
    out = await run([model], cases[:5], tools, "sys", RunSettings(repeats=1, max_usd=2.0),
                    tmp_path, providers={"pricey": Flaky()}, sleep=no_sleep)
    df = pd.read_parquet(out / "raw.parquet")
    assert (df["error"] == "skipped: spending cap reached").sum() == 3
    assert json.loads((out / "meta.json").read_text())["spent_usd"] == 2.2


async def test_oracle_run_scores_perfectly(tmp_path, cases, tools, specs, tool_names):
    model = ModelConfig("oracle", "oracle", "oracle")
    out = await run([model], cases, tools, "sys", RunSettings(repeats=2), tmp_path,
                    providers={"oracle": OracleProvider(cases)})
    df = score_frame(load_runs([out]), cases, specs, tool_names)
    assert len(df) == 2 * len(cases)
    assert df["correct"].all() and not df["unsafe"].any()
    assert df.loc[df["valid"].notna(), "valid"].all()
    row = summarise(df).iloc[0]
    assert row["accuracy [95% CI]"].startswith("100%")
    report = write_report(df, tmp_path / "report")
    assert "## Summary" in report.read_text()


class Counting:
    """Records the most requests in flight at once, across every instance."""

    in_flight = peak = 0

    async def complete(self, **_):
        Counting.in_flight += 1
        Counting.peak = max(Counting.peak, Counting.in_flight)
        await asyncio.sleep(0.01)
        Counting.in_flight -= 1
        return Completion(calls=[], total_s=0.01)


async def test_local_models_take_turns(tmp_path, cases, tools):
    local = "http://localhost:11434/v1"
    models = [ModelConfig("a", "fake", "a", base_url=local, concurrency=1),
              ModelConfig("b", "fake", "b", base_url=local, concurrency=1)]
    Counting.in_flight = Counting.peak = 0
    await run(models, cases[:4], tools, "sys", RunSettings(repeats=1), tmp_path,
              providers={"a": Counting(), "b": Counting()})
    assert Counting.peak == 1


def test_hosted_models_keep_their_own_limit():
    a = ModelConfig("a", "openai", "a", base_url="https://api.example.com/v1")
    b = ModelConfig("b", "openai", "b", base_url="https://api.example.com/v1")
    assert a.limit_group != b.limit_group
    assert ModelConfig("c", "openai", "c", group="gpu").limit_group == "gpu"


async def test_runs_started_together_get_separate_dirs(tmp_path, cases, tools):
    model = ModelConfig("oracle", "oracle", "oracle")
    outs = await asyncio.gather(*[
        run([model], cases[:1], tools, "sys", RunSettings(repeats=1), tmp_path,
            providers={"oracle": OracleProvider(cases)})
        for _ in range(3)
    ])
    assert len(set(outs)) == 3
