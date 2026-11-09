"""Claude models through the Anthropic SDK (Messages API, streamed)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import anthropic

from .base import Completion, RetryableError, ToolCall, retry_after_seconds

if TYPE_CHECKING:
    from ..tools import Tool


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 8192,
        timeout: float = 120.0,
        eager_input_streaming: bool = True,
        params: dict | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.eager_input_streaming = eager_input_streaming
        # Extra request fields from models.yaml, e.g. output_config.effort.
        self.params = params or {}
        # The runner owns retries so it can count them.
        self.client = anthropic.AsyncAnthropic(max_retries=0, timeout=timeout)

    def _tool_params(self, tools: list[Tool]) -> list[dict]:
        params = []
        for t in tools:
            param = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            if self.eager_input_streaming:
                # Tool arguments stream as they are generated instead of
                # arriving in one block at the end, so ttfo means the same
                # thing here as for providers that always stream arguments.
                param["eager_input_streaming"] = True
            params.append(param)
        return params

    async def complete(self, *, system: str, prompt: str, tools: list[Tool]) -> Completion:
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "tools": self._tool_params(tools),
            **self.params,
        }
        start = time.perf_counter()
        ttft = ttfo = None
        try:
            async with self.client.messages.stream(**request) as stream:
                async for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    now = time.perf_counter() - start
                    if ttft is None:
                        ttft = now
                    if ttfo is None and event.delta.type in ("text_delta", "input_json_delta"):
                        ttfo = now
                message = await stream.get_final_message()
        except ValueError as exc:
            # With eager streaming the SDK raises ValueError on tool-call JSON
            # it cannot parse at all. That is the model's output failing, so
            # it is scored as a broken call rather than retried.
            return Completion(
                calls=[ToolCall("<unparseable>", {"__invalid_json__": str(exc)})],
                ttft_s=ttft,
                ttfo_s=ttfo,
                total_s=time.perf_counter() - start,
            )
        except anthropic.RateLimitError as exc:                 # 429
            raise RetryableError(str(exc), retry_after_seconds(exc.response.headers)) from exc
        except anthropic.InternalServerError as exc:            # 5xx, including 529 overloaded
            raise RetryableError(str(exc), retry_after_seconds(exc.response.headers)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in (408, 409):
                raise RetryableError(str(exc)) from exc
            raise                                               # 400/401/404...: retrying will not help
        except anthropic.APIConnectionError as exc:             # network failure or client timeout
            raise RetryableError(str(exc)) from exc
        total = time.perf_counter() - start

        calls = [
            ToolCall(block.name, block.input if isinstance(block.input, dict) else {},
                     json.dumps(block.input))
            for block in message.content
            if block.type == "tool_use"
        ]
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = message.usage
        input_tokens = (
            usage.input_tokens
            + (getattr(usage, "cache_creation_input_tokens", None) or 0)
            + (getattr(usage, "cache_read_input_tokens", None) or 0)
        )
        return Completion(
            calls=calls,
            text=text,
            stop_reason=message.stop_reason,
            ttft_s=ttft,
            ttfo_s=ttfo,
            total_s=total,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
        )
