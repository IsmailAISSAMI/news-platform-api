"""Client for the Gemini API free tier (owner decision 2026-11-24, architecture 5.5).

Article text is sent to Google. Safeguards, all enforced here:
- only Google's API host and only models on the free allowlist (`FREE_MODELS`);
- every request goes through `RequestBudget` (hard limits, usage log), counted before
  it is sent;
- no tools, grounding, caching or other options that can be billed: the payload is
  built here and nothing can be added to it;
- HTTP 429 (quota) stops the run, with no retry.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.analysis.budget import (
    BudgetExceededError,
    RequestBudget,
    RequestTooLargeError,
    estimate_tokens,
)

GEMINI_HOST = "generativelanguage.googleapis.com"
# Models allowed to run: free-tier only, as listed on the owner's AI Studio page.
FREE_MODELS = frozenset({"gemma-4-31b-it"})


class LLMUnavailableError(Exception):
    """The request was refused (by our limits or by Google) or could not be made."""


class LLMRequestTooLargeError(Exception):
    """The article is too long for the free tier's per-minute token limit: skipped."""


class LLMOutputError(Exception):
    """The model answered, but not with valid output for the schema."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class Completion[T: BaseModel]:
    parsed: T
    raw: str
    duration_s: float
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def tokens_per_second(self) -> float | None:
        if not self.completion_tokens or self.duration_s <= 0:
            return None
        return self.completion_tokens / self.duration_s


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
# Gemma 4 sometimes writes its reasoning first, in <thought>...</thought> (seen
# 2026-11-24); that text contains braces, so it must go before the JSON is found.
_THOUGHT = re.compile(r"<(thought|think)>.*?</\1>", re.DOTALL)


def _extract_json(text: str) -> str:
    """The JSON object in a free-text answer (after any reasoning, inside any fence)."""
    stripped = _FENCE.sub("", _THOUGHT.sub("", text).strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if start != -1 and end > start else stripped


class GeminiClient:
    """Gemma on the Gemini API's OpenAI-compatible endpoint.

    Gemma on this API rejects the `response_format` JSON schema and the system role, so
    the rules and the schema go in the user message. The answer is still validated
    against the Pydantic schema: invalid output is rejected, never repaired.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr,
        budget: RequestBudget,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or parts.hostname != GEMINI_HOST:
            msg = f"refusing endpoint {base_url!r}: only https://{GEMINI_HOST} is allowed"
            raise ValueError(msg)
        if model not in FREE_MODELS:
            msg = f"refusing model {model!r}: not on the free-tier allowlist {sorted(FREE_MODELS)}"
            raise ValueError(msg)
        self._client = client
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self.budget = budget
        self.model = model

    async def structured[T: BaseModel](
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        max_tokens: int = 8000,
    ) -> Completion[T]:
        schema_text = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        content = (
            f"{system}\n\nAnswer with ONE JSON object only, no other text, matching this "
            f"JSON schema:\n{schema_text}\n\n{user}"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        await self._take_budget(content)

        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self._base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                timeout=900,
            )
        except httpx.HTTPError as exc:
            await self._record(ok=False)
            msg = f"{self.model} request failed: {type(exc).__name__}"
            raise LLMUnavailableError(msg) from exc
        duration = time.monotonic() - started
        if response.status_code != httpx.codes.OK:
            await self._record(ok=False)
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                msg = f"{self.model}: Google's quota reached (HTTP 429); stopping, no retry"
            else:  # Google's error text never contains the key; it is cut short anyway
                msg = f"{self.model} returned HTTP {response.status_code}: {response.text[:300]}"
            raise LLMUnavailableError(msg)

        body = response.json()
        raw = body["choices"][0]["message"].get("content") or ""
        usage = body.get("usage") or {}
        prompt_tokens, completion_tokens = (
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        try:
            parsed = schema.model_validate(json.loads(_extract_json(raw)))
        except (json.JSONDecodeError, ValidationError) as exc:
            await self._record(
                ok=False, input_tokens=prompt_tokens, output_tokens=completion_tokens
            )
            msg = f"invalid output: {type(exc).__name__}: {str(exc)[:300]}"
            raise LLMOutputError(msg, raw) from exc
        await self._record(ok=True, input_tokens=prompt_tokens, output_tokens=completion_tokens)
        return Completion(
            parsed=parsed,
            raw=raw,
            duration_s=duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def _take_budget(self, text: str) -> None:
        """Wait for the per-minute limits, then count the request (or refuse it)."""
        tokens = estimate_tokens(text)
        for _ in range(self.budget.per_minute + 1):  # each wait frees the oldest slot
            wait = self.budget.wait_seconds(tokens)
            if wait <= 0:
                break
            await asyncio.sleep(wait)
        try:
            await asyncio.to_thread(self.budget.reserve, tokens)
        except RequestTooLargeError as exc:
            raise LLMRequestTooLargeError(str(exc)) from exc
        except BudgetExceededError as exc:
            raise LLMUnavailableError(str(exc)) from exc

    async def _record(
        self, *, ok: bool, input_tokens: int | None = None, output_tokens: int | None = None
    ) -> None:
        await asyncio.to_thread(
            self.budget.record_result,
            ok=ok,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
