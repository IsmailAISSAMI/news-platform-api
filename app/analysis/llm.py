"""Client for the Gemini API free tier (owner decision 2026-11-24, architecture 5.5).

Article text is sent to Google. Safeguards, all enforced here:
- only Google's API host and only models on the free allowlist (`FREE_MODELS`);
- every request is reserved in the database ledger (`Ledger`) BEFORE it is sent, and
  the reservation is finished with the real outcome afterwards (see ledger.py);
- no tools, grounding, caching or other options that can be billed: the payload is
  built here and nothing can be added to it;
- HTTP 429 (quota) stops the run, with no retry;
- "high demand" errors (503, and 500/502/504, timeouts, no network) are EXPECTED on
  the free tier: the same request is tried again after 10, 20, 30, then every 60
  seconds, until Google answers. Every attempt is reserved in the ledger, so the daily
  limit stays the absolute stop. Each attempt is logged with today's counts.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.analysis.budget import (
    BudgetExceededError,
    Ledger,
    Outcome,
    RequestBudget,
    RequestTooLargeError,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

GEMINI_HOST = "generativelanguage.googleapis.com"
# Google's "high demand" / temporary server errors: worth waiting for, never a bug of ours.
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
# Pauses between attempts; the last one repeats until Google answers.
RETRY_PAUSES_S = (10.0, 20.0, 30.0, 60.0)
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


def _without_titles(schema: Any) -> Any:  # any JSON value
    """The JSON schema minus Pydantic's "title" keys: same meaning, ~6% fewer tokens."""
    if isinstance(schema, dict):
        return {k: _without_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_without_titles(v) for v in schema]
    return schema


class GeminiClient:
    """Gemma on the Gemini API's OpenAI-compatible endpoint.

    Gemma on this API rejects the `response_format` JSON schema and the system role, so
    the rules and the schema go in the user message. The answer is still validated
    against the Pydantic schema: invalid output is rejected, never repaired.
    """

    def __init__(  # noqa: PLR0913  # endpoint, model, key, budget, ledger, retry policy
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr,
        budget: RequestBudget,
        ledger: Ledger,
        run_id: int | None = None,
        retry_pauses_s: tuple[float, ...] = RETRY_PAUSES_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        self.ledger = ledger
        self.model = model
        self.run_id = run_id  # set by `news analyze` once its run row exists
        self._retry_pauses = retry_pauses_s
        self._sleep = sleep
        # Shared by every concurrent request: one lock so admission (pacing + counting)
        # is never raced, and one cooldown so a "busy" answer pauses ALL workers instead
        # of letting each of them spend budget on an overloaded server.
        self._admission = asyncio.Lock()
        self._busy_streak = 0
        self._cooldown_until = 0.0

    async def structured[T: BaseModel](
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        # Free tier limits INPUT tokens only; Gemma's reasoning block plus the JSON can
        # pass 8,000 output tokens (seen 2026-11-24), so allow more.
        max_tokens: int = 16000,
    ) -> Completion[T]:
        schema_text = json.dumps(_without_titles(schema.model_json_schema()), separators=(",", ":"))
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
        response, duration, reservation = await self._send_with_retries(payload, content)
        try:
            body = response.json()
            choice = body["choices"][0]
            raw = choice["message"].get("content") or ""
            usage = body.get("usage") or {}
            prompt_tokens, completion_tokens = (
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            await self._finish(reservation, "failed", None, None)
            msg = f"unusable answer shape: {type(exc).__name__}"
            raise LLMOutputError(msg, response.text[:300]) from exc
        try:
            if choice.get("finish_reason") == "length":
                msg = f"answer cut off at the {max_tokens}-token output limit"
                raise LLMOutputError(msg, raw)
            parsed = schema.model_validate(json.loads(_extract_json(raw)))
        except LLMOutputError:
            await self._finish(reservation, "failed", prompt_tokens, completion_tokens)
            raise
        except (json.JSONDecodeError, ValidationError) as exc:
            await self._finish(reservation, "failed", prompt_tokens, completion_tokens)
            msg = f"invalid output: {type(exc).__name__}: {str(exc)[:300]}"
            raise LLMOutputError(msg, raw) from exc
        await self._finish(reservation, "answered", prompt_tokens, completion_tokens)
        return Completion(
            parsed=parsed,
            raw=raw,
            duration_s=duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def _send_with_retries(
        self, payload: dict[str, Any], content: str
    ) -> tuple[httpx.Response, float, int]:
        """Send until Google answers: busy errors are waited out, never fatal.

        Same model every time. Each attempt is reserved in the ledger, so the daily
        limit stays the absolute stop. Only errors that waiting cannot fix (429 quota,
        a bad request, a wrong key) end the run. Returns the reservation id of the
        attempt that was answered; its outcome is recorded by the caller.
        """
        attempt = 0
        while True:
            attempt += 1
            reservation = await self._admit(estimate_tokens(content))
            started = time.monotonic()
            try:
                response: httpx.Response | None = await self._client.post(
                    f"{self._base}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                    timeout=900,
                )
            except httpx.TransportError:
                # Too slow, offline, or the server dropped the connection mid-answer
                # (RemoteProtocolError): all worth waiting for, never a crash of the run.
                response = None
            duration = time.monotonic() - started
            if response is not None and response.status_code == httpx.codes.OK:
                self._busy_streak = 0
                self.budget.correct_last_estimate(_input_tokens(response))
                return response, duration, reservation
            status = "timeout/offline" if response is None else f"HTTP {response.status_code}"
            busy = response is None or response.status_code in TRANSIENT_STATUSES
            await self._finish(reservation, "refused_busy" if busy else "failed", None, None)
            if not busy:
                assert response is not None  # noqa: S101  # busy covers the None case
                if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                    msg = f"{self.model}: Google's quota reached (HTTP 429); stopping"
                else:  # Google's error text never contains the key; it is cut short anyway
                    msg = f"{self.model} returned {status}: {response.text[:300]}"
                raise LLMUnavailableError(msg)
            self._busy_streak += 1
            pause = self._retry_pauses[min(self._busy_streak, len(self._retry_pauses)) - 1]
            self._cooldown_until = max(self._cooldown_until, self.budget.monotonic() + pause)
            logger.info(
                "Gemini busy (%s), attempt %d; all requests pause %.0fs · %s",
                status, attempt, pause, await self.usage_line(),
            )  # fmt: skip

    async def usage_line(self) -> str:
        """Today's counts from the database, for the live terminal log."""
        [today] = await self.ledger.totals(self.model, 1)
        return (
            f"today: {today.passed} passed, {today.failed} failed ({today.busy} busy), "
            f"{today.pending} pending, {today.requests}/{self.budget.per_day} sent"
        )

    async def _admit(self, tokens: int) -> int:
        """Wait out the cooldown and the per-minute limits, check every limit, reserve.

        Returns the ledger reservation id. Under one lock: admitted one at a time, so two
        requests can never both take the last slot of a minute, the run or the day.
        """
        try:
            self.budget.check_size(tokens)  # never sendable: refused before any wait
        except RequestTooLargeError as exc:
            raise LLMRequestTooLargeError(str(exc)) from exc
        async with self._admission:
            remaining = self._cooldown_until - self.budget.monotonic()
            if remaining > 0:
                await self._sleep(remaining)
            for _ in range(self.budget.per_minute + 1):  # each wait frees the oldest slot
                wait = self.budget.wait_seconds(tokens)
                if wait <= 0:
                    break
                await self._sleep(wait)
            try:
                self.budget.open_run_slot()
            except BudgetExceededError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            try:
                reservation = await self.ledger.reserve(
                    self.model, self.budget.quota_day(), self.budget.per_day, self.run_id
                )
            except BudgetExceededError as exc:
                self.budget.settle_run_slot(answered=False)
                raise LLMUnavailableError(str(exc)) from exc
            except Exception:
                self.budget.settle_run_slot(answered=False)  # nothing was sent
                raise
            self.budget.pace(tokens)
            return reservation

    async def _finish(
        self,
        reservation: int,
        outcome: Outcome,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """Record how a sent request ended, and release its per-run slot."""
        self.budget.settle_run_slot(answered=outcome == "answered")
        await self.ledger.record(reservation, outcome, input_tokens, output_tokens)


def _input_tokens(response: httpx.Response) -> int | None:
    try:
        usage = response.json().get("usage") or {}
    except ValueError:
        return None
    value = usage.get("prompt_tokens")
    return value if isinstance(value, int) else None
