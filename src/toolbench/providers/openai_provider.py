"""OpenAI and any OpenAI-compatible endpoint (Chat Completions, streamed).

Most inference providers (Gemini's compatibility endpoint, Groq, Together,
Fireworks, OpenRouter, a local vLLM) speak this protocol, so one adapter with
a base_url covers them. That is also what makes "same open-weights model on
two providers" a config change rather than new code.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import openai

from .base import Completion, RetryableError, ToolCall, parse_arguments, retry_after_seconds

if TYPE_CHECKING:
    from ..tools import Tool


class OpenAIProvider:
    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 120.0,
        params: dict | None = None,
    ) -> None:
        self.model = model
        # Extra request fields from models.yaml. Token-limit and reasoning
        # parameter names differ between providers, so none are sent unless
        # configured.
        self.params = params or {}
        self.client = openai.AsyncOpenAI(
            # Local servers such as Ollama ignore the key but the SDK wants one.
            api_key=os.environ.get(api_key_env) or ("unused" if base_url else None),
            base_url=base_url,
            max_retries=0,
            timeout=timeout,
        )

    async def complete(self, *, system: str, prompt: str, tools: list[Tool]) -> Completion:
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "tools": [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            **self.params,
        }
        start = time.perf_counter()
        ttft = ttfo = None
        text: list[str] = []
        pending: dict[int, dict[str, str]] = {}   # tool calls arrive in fragments, keyed by index
        stop_reason = None
        usage = None
        try:
            stream = await self.client.chat.completions.create(**request)
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                now = time.perf_counter() - start
                # Not part of the OpenAI schema, but several compatible
                # providers stream reasoning under one of these names.
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                visible = bool(delta.content or delta.tool_calls)
                if ttft is None and (visible or reasoning):
                    ttft = now
                if ttfo is None and visible:
                    ttfo = now
                if delta.content:
                    text.append(delta.content)
                for fragment in delta.tool_calls or []:
                    call = pending.setdefault(fragment.index, {"name": "", "arguments": ""})
                    if fragment.function is not None:
                        if fragment.function.name and not call["name"]:
                            call["name"] = fragment.function.name
                        call["arguments"] += fragment.function.arguments or ""
                if choice.finish_reason:
                    stop_reason = choice.finish_reason
        except openai.RateLimitError as exc:                    # 429
            raise RetryableError(str(exc), retry_after_seconds(exc.response.headers)) from exc
        except openai.InternalServerError as exc:               # 5xx
            raise RetryableError(str(exc), retry_after_seconds(exc.response.headers)) from exc
        except openai.APIStatusError as exc:
            if exc.status_code in (408, 409):
                raise RetryableError(str(exc)) from exc
            raise
        except openai.APIConnectionError as exc:                # includes APITimeoutError
            raise RetryableError(str(exc)) from exc
        total = time.perf_counter() - start

        calls = [
            ToolCall(c["name"], parse_arguments(c["arguments"]), c["arguments"])
            for _, c in sorted(pending.items())
        ]
        return Completion(
            calls=calls,
            text="".join(text),
            stop_reason=stop_reason,
            ttft_s=ttft,
            ttfo_s=ttfo,
            total_s=total,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )
