"""Client for an OpenAI-compatible chat server: LM Studio (local) or Gemini (remote).

A remote endpoint is refused unless it comes with an API key AND a request budget, so a
remote call can never run without the hard limits of `app/analysis/budget.py`. Owner
decision 2026-11-24 (docs/architecture.md section 5.5): article text may be sent to
Google's Gemini API free tier.
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

from app.analysis.budget import BudgetExceededError, RequestBudget, estimate_tokens

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


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
# Gemma 4 on the Gemini API writes its reasoning first, in <thought>...</thought>
# (seen 2026-11-24); that text contains braces, so it must go before the JSON is found.
_THOUGHT = re.compile(r"<(thought|think)>.*?</\1>", re.DOTALL)


def _extract_json(text: str) -> str:
    """The JSON object in a free-text answer (after any reasoning, inside any fence)."""
    stripped = _FENCE.sub("", _THOUGHT.sub("", text).strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if start != -1 and end > start else stripped


class ChatLLM:
    """One model behind an OpenAI-compatible endpoint.

    `schema_in_prompt`: some hosted models (Gemma on the Gemini API) reject both the
    `response_format` JSON schema and the system role, so the schema and the system
    prompt are put in the user message instead. The answer is still validated against
    the same Pydantic schema, so invalid output is rejected exactly as before.
    """

    def __init__(  # noqa: PLR0913  # endpoint, model and the remote safeguards
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | None = None,
        budget: RequestBudget | None = None,
        schema_in_prompt: bool = False,
    ) -> None:
        host = urlsplit(base_url).hostname or ""
        self.local = host in LOCAL_HOSTS
        if not self.local and (api_key is None or budget is None):
            msg = f"refusing remote LLM endpoint {host!r} without an API key and a budget"
            raise ValueError(msg)
        self._client = client
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self.budget = budget
        self.schema_in_prompt = schema_in_prompt
        self.model = model

    async def ensure_ready(self) -> None:
        """LM Studio only: a remote check would spend a request from the budget."""
        if not self.local:
            return
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
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.schema_in_prompt:
            schema_text = json.dumps(schema.model_json_schema(), separators=(",", ":"))
            payload["messages"] = [
                {
                    "role": "user",
                    "content": f"{system}\n\nAnswer with ONE JSON object only, no other "
                    f"text, matching this JSON schema:\n{schema_text}\n\n{user}",
                }
            ]
        else:
            payload["messages"] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }
        if self.local:
            # Verified with LM Studio + Qwen3.5 (2026-11-23): without this, the thinking
            # template captures the whole JSON answer in `reasoning_content` and `content`
            # comes back empty. `chat_template_kwargs` and "/no_think" had no effect.
            payload["reasoning_effort"] = "none"
        payload |= extra_body or {}

        headers = {}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        if self.budget is not None:
            await self._take_budget(json.dumps(payload["messages"]))

        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self._base}/chat/completions", json=payload, headers=headers, timeout=900
            )
        except httpx.HTTPError as exc:
            msg = f"{self.model} request failed: {type(exc).__name__}"
            raise LLMUnavailableError(msg) from exc
        duration = time.monotonic() - started
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            msg = f"{self.model}: provider quota reached (HTTP 429); stopping, no retry"
            raise LLMUnavailableError(msg)
        if response.status_code != httpx.codes.OK:
            # The response text never contains the key; it is cut short all the same.
            msg = f"{self.model} returned HTTP {response.status_code}: {response.text[:300]}"
            raise LLMUnavailableError(msg)

        body = response.json()
        message = body["choices"][0]["message"]
        raw = message.get("content") or ""
        usage = body.get("usage") or {}
        try:
            parsed = schema.model_validate(
                json.loads(_extract_json(raw) if self.schema_in_prompt else raw)
            )
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

    async def _take_budget(self, text: str) -> None:
        """Wait for the per-minute limits, then count the request (or refuse it)."""
        assert self.budget is not None  # noqa: S101  # checked by the caller
        tokens = estimate_tokens(text)
        for _ in range(self.budget.per_minute + 1):  # each wait frees the oldest slot
            wait = self.budget.wait_seconds(tokens)
            if wait <= 0:
                break
            await asyncio.sleep(wait)
        try:
            await asyncio.to_thread(self.budget.reserve, tokens)
        except BudgetExceededError as exc:
            raise LLMUnavailableError(str(exc)) from exc
