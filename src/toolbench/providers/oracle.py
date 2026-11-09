"""A provider that always gives the first accepted answer. No API key needed.

It exists to test the pipeline, not models: a run against it must score 100%
correct, 0% unsafe and 100% valid. Anything less means a case's accepted
answer fails the allowlist, or the scorer is wrong.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .base import Completion, ToolCall

if TYPE_CHECKING:
    from ..cases import Case
    from ..tools import Tool


class OracleProvider:
    def __init__(self, cases: list[Case], latency_s: float = 0.0) -> None:
        self.by_prompt = {c.prompt: c for c in cases}
        self.latency_s = latency_s

    async def complete(self, *, system: str, prompt: str, tools: list[Tool]) -> Completion:
        await asyncio.sleep(self.latency_s)
        answer = self.by_prompt[prompt].accept[0]
        calls = []
        if answer.tool is not None:
            calls.append(ToolCall(answer.tool, {k: v[0] for k, v in answer.args.items()}))
        return Completion(
            calls=calls,
            text="" if calls else "(oracle: no tool call)",
            stop_reason="tool_use" if calls else "end_turn",
            ttft_s=self.latency_s,
            ttfo_s=self.latency_s,
            total_s=self.latency_s,
            input_tokens=0,
            output_tokens=0,
        )
