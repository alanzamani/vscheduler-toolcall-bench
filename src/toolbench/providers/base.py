"""The interface every provider adapter implements.

Adapters do no retrying of their own (SDK retries are switched off) so the
runner sees every attempt and can count it. An adapter's only job is to make
one streamed request, time it, and translate its errors into two kinds:
RetryableError for rate limits, overload and network trouble, and anything
else for failures that a retry would not fix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..tools import Tool


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    raw: str = ""   # arguments exactly as the model produced them


@dataclass
class Completion:
    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    stop_reason: str | None = None
    # Two first-token times, because reasoning models stream thinking before
    # anything visible: ttft is the first streamed output of any kind, ttfo
    # the first answer text or tool-call argument. Both are None when the
    # stream produced nothing.
    ttft_s: float | None = None
    ttfo_s: float | None = None
    total_s: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None   # as billed, so reasoning tokens included


class RetryableError(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Provider(Protocol):
    async def complete(self, *, system: str, prompt: str, tools: list[Tool]) -> Completion: ...


def parse_arguments(raw: str) -> dict:
    """Decode a tool call's JSON arguments.

    Malformed JSON is the model's mistake, not ours, so it is kept as an
    argument the allowlist will reject instead of raising.
    """
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__invalid_json__": raw}
    return args if isinstance(args, dict) else {"__invalid_json__": raw}


def retry_after_seconds(headers) -> float | None:
    try:
        value = headers.get("retry-after") if headers is not None else None
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
