"""Client for a local OpenAI-compatible server (LM Studio).

Article text never leaves the machine: non-local endpoints are refused.
"""

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LLMUnavailableError(Exception):
    """The local model server is not reachable or the model is not available."""


class LLMOutputError(Exception):
    """The model answered, but not with valid output for the schema."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class Completion[T: BaseModel]:
    parsed: T
    raw: str
    reasoning: str | None
    duration_s: float
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def tokens_per_second(self) -> float | None:
        if not self.completion_tokens or self.duration_s <= 0:
            return None
        return self.completion_tokens / self.duration_s


class LocalLLM:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        model: str,
        allow_remote: bool = False,
    ) -> None:
        host = urlsplit(base_url).hostname or ""
        if host not in LOCAL_HOSTS and not allow_remote:
            msg = f"refusing non-local LLM endpoint {host!r}: article text must stay local"
            raise ValueError(msg)
        self._client = client
        self._base = base_url.rstrip("/")
        self.model = model

    async def ensure_ready(self) -> None:
        try:
            response = await self._client.get(f"{self._base}/models", timeout=5)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = (
                f"LM Studio is not reachable at {self._base}. Open LM Studio, go to the "
                "Developer tab and click Start server."
            )
            raise LLMUnavailableError(msg) from exc
        available = {m.get("id") for m in response.json().get("data", [])}
        if self.model not in available:
            msg = f"model {self.model!r} is not available in LM Studio ({sorted(available)})"
            raise LLMUnavailableError(msg)

    async def structured[T: BaseModel](  # noqa: PLR0913  # mirrors the chat API's options
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        max_tokens: int = 8000,
        temperature: float = 0.1,
        extra_body: dict[str, Any] | None = None,
    ) -> Completion[T]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Verified with LM Studio + Qwen3.5 (2026-11-23): without this, the thinking
            # template captures the whole JSON answer in `reasoning_content` and `content`
            # comes back empty. `chat_template_kwargs` and "/no_think" had no effect.
            "reasoning_effort": "none",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
            **(extra_body or {}),
        }
        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self._base}/chat/completions", json=payload, timeout=900
            )
        except httpx.HTTPError as exc:
            msg = f"LM Studio request failed: {type(exc).__name__}"
            raise LLMUnavailableError(msg) from exc
        duration = time.monotonic() - started
        if response.status_code != httpx.codes.OK:
            msg = f"LM Studio returned HTTP {response.status_code}: {response.text[:300]}"
            raise LLMUnavailableError(msg)

        body = response.json()
        message = body["choices"][0]["message"]
        raw = message.get("content") or ""
        usage = body.get("usage") or {}
        try:
            parsed = schema.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            msg = f"invalid output: {type(exc).__name__}: {str(exc)[:300]}"
            raise LLMOutputError(msg, raw) from exc
        return Completion(
            parsed=parsed,
            raw=raw,
            reasoning=message.get("reasoning_content") or message.get("reasoning"),
            duration_s=duration,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
