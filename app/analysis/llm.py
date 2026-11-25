"""Client for the Gemini API free tier (owner decision 2026-11-24, architecture 5.5).

Article text is sent to Google. Safeguards, all enforced here:
- only Google's API host and only models on the free allowlist (`FREE_MODELS`);
- every request is reserved in the database ledger (`Ledger`) BEFORE it is sent, and
  the reservation is finished with the real outcome afterwards (see ledger.py);
- no tools, grounding, caching or other options that can be billed: the payload is
  built here and nothing can be added to it;
- HTTP 429 (quota) stops the run, with no retry;
- "high demand" errors (503, and 500/502/504, read/write timeouts) are EXPECTED on the
  free tier: the same request is tried again after 10, 20, 30, then every 60 seconds,
  until Google answers. Every attempt is reserved in the ledger, so the daily limit stays
  the absolute stop. Each attempt is logged with today's counts;
- network errors (no network, connection refused, dropped or timed out while
  connecting) are retried the same way, but only up to `MAX_TRANSPORT_ERRORS` in a row
  for one request, counted until Google answers: an offline Mac stops instead of
  reserving its daily budget (owner decision D2, 2026-11-25).
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.analysis.budget import (
    WINDOW_S,
    BudgetExceededError,
    Ledger,
    Outcome,
    PaceHandle,
    RequestBudget,
    RequestTooLargeError,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

GEMINI_HOST = "generativelanguage.googleapis.com"
# The only endpoint allowed, exactly (no other path, port, user or query).
GEMINI_BASE_URL = f"https://{GEMINI_HOST}/v1beta/openai"
# Admission waits (cooldown, then per-minute) are rechecked in a loop until both are
# clear; past this many rounds something is wrong, so the request is refused, not sent.
MAX_ADMISSION_ROUNDS = 100
# Google's "high demand" / temporary server errors: worth waiting for, never a bug of ours.
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
# Pauses between attempts; the last one repeats until Google answers.
RETRY_PAUSES_S = (10.0, 20.0, 30.0, 60.0)
# Consecutive network errors (not timeouts, not HTTP answers) for ONE request before it
# is given up (owner decision D2). Lowering it is allowed; raising it is not.
MAX_TRANSPORT_ERRORS = 5
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
        if base_url != GEMINI_BASE_URL:
            msg = f"refusing endpoint {base_url!r}: only {GEMINI_BASE_URL} is allowed"
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
        # Half-open: during a busy streak only ONE request (the probe) is in flight; the
        # others wait for its answer. Set when no probe is out.
        self._probe_done = asyncio.Event()
        self._probe_done.set()
        self._seeded = False  # the per-minute window is seeded from the ledger once

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
        # Google answered (HTTP 200): the per-run slot is used whatever the answer is.
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
            await self.ledger.record(reservation, "failed", None, None, "unusable answer")
            msg = f"unusable answer shape: {type(exc).__name__}"
            raise LLMOutputError(msg, response.text[:300]) from exc
        try:
            if choice.get("finish_reason") == "length":
                msg = f"answer cut off at the {max_tokens}-token output limit"
                raise LLMOutputError(msg, raw)
            parsed = schema.model_validate(json.loads(_extract_json(raw)))
        except LLMOutputError:
            await self.ledger.record(
                reservation, "failed", prompt_tokens, completion_tokens, "cut off"
            )
            raise
        except (json.JSONDecodeError, ValidationError) as exc:
            await self.ledger.record(
                reservation, "failed", prompt_tokens, completion_tokens, "invalid output"
            )
            msg = f"invalid output: {_error_summary(exc)}"
            raise LLMOutputError(msg, raw) from exc
        await self.ledger.record(reservation, "answered", prompt_tokens, completion_tokens)
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
        transport_errors = 0  # consecutive, for this request only
        tokens = estimate_tokens(content)
        while True:
            attempt += 1
            reservation, handle, probe = await self._admit(tokens)
            started = time.monotonic()
            try:
                try:
                    response: httpx.Response | None = await self._client.post(
                        f"{self._base}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                        timeout=900,
                    )
                except httpx.TransportError as exc:
                    # A slow answer is Google's overload: waited out like a 503, and it
                    # does not reset the network-error count (only an answer does).
                    # Network errors are waited for, but only a few times in a row.
                    response = None
                    if _is_network_error(exc):
                        transport_errors += 1
                        if transport_errors >= MAX_TRANSPORT_ERRORS:
                            await self._give_up_offline(reservation, transport_errors, exc)
                except asyncio.CancelledError:
                    # Ctrl-C or a stopped worker: the request may have reached Google.
                    await self._finish(reservation, "failed", None, None, error="cancelled")
                    raise
                duration = time.monotonic() - started
                if response is not None:
                    transport_errors = 0  # Google answered: the network works
                if response is not None and response.status_code == httpx.codes.OK:
                    self._busy_streak = 0
                    self.budget.settle_run_slot(answered=True)  # any HTTP 200 uses the slot
                    self.budget.correct(handle, _input_tokens(response))
                    return response, duration, reservation
                status = "timeout/offline" if response is None else f"HTTP {response.status_code}"
                busy = response is None or response.status_code in TRANSIENT_STATUSES
                await self._finish(
                    reservation, "refused_busy" if busy else "failed", None, None, error=status
                )
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
            finally:
                if probe:
                    self._probe_done.set()  # the next waiting worker may probe
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

    async def _admit(self, tokens: int) -> tuple[int, PaceHandle, bool]:
        """Wait out the cooldown and the per-minute limits, check every limit, reserve.

        Returns the ledger reservation id, the request's pacing handle, and whether this
        request is the single probe of a busy streak. Under one lock: admitted one at a
        time, so two requests can never both take the last slot of a minute, run or day.
        """
        try:
            self.budget.check_size(tokens)  # never sendable: refused before any wait
        except RequestTooLargeError as exc:
            raise LLMRequestTooLargeError(str(exc)) from exc
        async with self._admission:
            if not self._seeded:  # other processes' last minute counts too
                self.budget.seed(await self.ledger.recent(self.model, WINDOW_S))
                self._seeded = True
            served = 0.0  # the cooldown end already waited for (a longer one needs a wait)
            for _ in range(MAX_ADMISSION_ROUNDS):
                if self._busy_streak > 0 and not self._probe_done.is_set():
                    await self._probe_done.wait()  # half-open: one probe at a time
                    continue
                remaining = self._cooldown_until - self.budget.monotonic()
                if remaining > 0 and self._cooldown_until > served:
                    served = self._cooldown_until
                    await self._sleep(remaining)
                    continue
                wait = self.budget.wait_seconds(tokens)
                if wait > 0:
                    await self._sleep(wait)
                    continue  # the cooldown may have been extended meanwhile
                break
            else:  # fail closed: never send without both checks clear
                msg = f"admission still blocked after {MAX_ADMISSION_ROUNDS} waits; not sent"
                raise LLMUnavailableError(msg)
            try:
                self.budget.open_run_slot()
            except BudgetExceededError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            try:
                reservation = await self.ledger.reserve(
                    self.model, self.budget.quota_day(), self.budget.per_day, self.run_id, tokens
                )
            except BudgetExceededError as exc:
                self.budget.settle_run_slot(answered=False)
                raise LLMUnavailableError(str(exc)) from exc
            except BaseException:
                self.budget.settle_run_slot(answered=False)  # nothing was sent
                raise
            probe = self._busy_streak > 0
            if probe:
                self._probe_done.clear()
            return reservation, self.budget.pace(tokens), probe

    async def _give_up_offline(
        self, reservation: int, count: int, exc: httpx.TransportError
    ) -> NoReturn:
        """Too many network errors in a row: record the last attempt and stop (D2)."""
        kind = type(exc).__name__
        await self._finish(reservation, "failed", None, None, error=f"network error ({kind})")
        msg = (
            f"{self.model}: {count} network errors in a row ({kind}); giving up. "
            "Check the connection."
        )
        raise LLMUnavailableError(msg) from exc

    async def _finish(  # the outcome's fields
        self,
        reservation: int,
        outcome: Outcome,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        error: str | None = None,
    ) -> None:
        """Record how a request that Google did not answer ended; release its slot."""
        self.budget.settle_run_slot(answered=False)
        await self.ledger.record(reservation, outcome, input_tokens, output_tokens, error)


def _is_network_error(exc: httpx.TransportError) -> bool:
    """No working connection to Google (capped, D2), as opposed to a slow answer.

    Connect and pool timeouts mean the connection was never made (no route, captive
    Wi-Fi), so they count with refused or dropped connections; read and write timeouts
    happen once connected and are Google's overload, waited out like a 503.
    """
    if isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout):
        return True
    return not isinstance(exc, httpx.TimeoutException)


def _error_summary(exc: json.JSONDecodeError | ValidationError) -> str:
    """Why the output was invalid, without any of the output itself.

    The model's answer can echo article text, which must never reach logs or the
    database, so a ValidationError is reduced to error locations and types only.
    """
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}"
            for err in exc.errors(include_input=False, include_url=False, include_context=False)
        ]
        return f"ValidationError: {exc.error_count()} error(s): {'; '.join(parts)}"[:300]
    # Position only (line, column), never the text.
    return f"JSONDecodeError at line {exc.lineno} column {exc.colno}"


def _input_tokens(response: httpx.Response) -> int | None:
    try:
        usage = response.json().get("usage") or {}
    except ValueError:
        return None
    value = usage.get("prompt_tokens")
    return value if isinstance(value, int) else None
