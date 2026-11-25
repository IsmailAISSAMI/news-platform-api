"""Pacing and per-run limits for the Gemini free tier. No network, no database.

The daily limit is counted in the database (tests/integration/test_ledger.py).
"""

from datetime import UTC, datetime

import pytest

from app.analysis.budget import (
    CEILINGS,
    PACE_PAD_S,
    WINDOW_S,
    BudgetExceededError,
    RequestBudget,
    RequestTooLargeError,
    estimate_tokens,
)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at
        self.mono = 0.0


def budget(clock: Clock, per_run: int = 3) -> RequestBudget:
    return RequestBudget(
        per_run=per_run,
        per_day=5,
        per_minute=2,
        input_tokens_per_minute=1000,
        now=lambda: clock.at,
        monotonic=lambda: clock.mono,
    )


NOON_UTC = datetime(2026, 11, 24, 12, tzinfo=UTC)


def admit(b: RequestBudget, tokens: int = 10) -> None:
    """What the client does for one request that is about to be sent."""
    b.check_size(tokens)
    b.open_run_slot()
    b.pace(tokens)


def test_only_answered_requests_use_the_per_run_limit() -> None:
    b = budget(Clock(NOON_UTC), per_run=2)
    admit(b)
    b.settle_run_slot(answered=False)  # 503: busy, does not use the run
    for _ in range(2):
        admit(b)
        b.settle_run_slot(answered=True)

    with pytest.raises(BudgetExceededError, match="per-run"):
        b.open_run_slot()
    assert b.used_this_run == 2


def test_requests_in_flight_count_against_the_per_run_limit() -> None:
    b = budget(Clock(NOON_UTC), per_run=2)
    admit(b)
    admit(b)  # both still in flight: a third must not start, even if both succeed later

    with pytest.raises(BudgetExceededError, match="per-run"):
        b.open_run_slot()
    b.settle_run_slot(answered=True)
    b.settle_run_slot(answered=False)
    assert (b.used_this_run, b.in_flight_this_run) == (1, 0)


def test_a_request_larger_than_the_token_limit_is_never_sent() -> None:
    b = budget(Clock(NOON_UTC))

    assert b.wait_seconds(5000) == 0  # no point waiting: it can never fit
    with pytest.raises(RequestTooLargeError, match="too large"):
        b.check_size(5000)


def test_per_minute_limits_make_the_caller_wait() -> None:
    clock = Clock(NOON_UTC)
    b = budget(clock)
    b.pace(600)
    assert b.wait_seconds(300) == 0
    assert b.wait_seconds(500) > 0  # 600 + 500 tokens > 1000 per minute

    b.pace(300)
    clock.mono = 30
    assert b.wait_seconds(10) > 0  # 2 requests per minute already used

    clock.mono = 61
    assert b.wait_seconds(10) == 0  # the window has moved on


def test_settings_can_lower_the_limits_but_never_raise_them() -> None:
    with pytest.raises(ValueError, match="per_day=1001"):
        RequestBudget(
            per_run=3,
            per_day=1001,
            per_minute=10,
            input_tokens_per_minute=8000,
        )
    with pytest.raises(ValueError, match="per_minute=21"):
        RequestBudget(
            per_run=3,
            per_day=10,
            per_minute=21,
            input_tokens_per_minute=8000,
        )


def test_the_ceilings_are_the_owner_limits() -> None:
    assert CEILINGS == {
        "per_run": 1000,
        "lab_per_run": 3,
        "per_day": 1000,
        "per_minute": 20,
        "input_tokens_per_minute": 10_000,
        "concurrency": 8,
    }


def test_the_quota_day_is_pacific_not_utc() -> None:
    clock = Clock(datetime(2026, 11, 24, 7, 59, tzinfo=UTC))  # 23:59 on Nov 23 in California
    assert budget(clock).quota_day().isoformat() == "2026-11-23"
    clock.at = datetime(2026, 11, 24, 8, 1, tzinfo=UTC)  # 00:01 Pacific: a new quota day
    assert budget(clock).quota_day().isoformat() == "2026-11-24"


def test_no_local_file_is_involved_in_the_budget() -> None:
    """The quota is in the database: the budget has no file to delete or copy."""
    fields = set(RequestBudget.__dataclass_fields__)
    assert not {"usage_file"} & fields


# --- boundary tests: each fails if its comparison operator is changed -------------------


def test_check_size_accepts_the_limit_and_refuses_one_more() -> None:
    b = budget(Clock(NOON_UTC))
    b.check_size(1000)
    with pytest.raises(RequestTooLargeError):
        b.check_size(1001)


def test_pace_then_wait_at_exactly_the_request_limit() -> None:
    clock = Clock(NOON_UTC)
    b = budget(clock)  # 2 requests per minute
    b.pace(10)
    assert b.wait_seconds(10) == 0  # 1 < 2: a second one may go
    b.pace(10)
    assert b.wait_seconds(10) == WINDOW_S + PACE_PAD_S  # exactly at the limit: wait


def test_tokens_at_exactly_the_token_limit_are_allowed() -> None:
    b = budget(Clock(NOON_UTC))
    b.pace(600)
    assert b.wait_seconds(400) == 0  # 600 + 400 == 1000: allowed
    assert b.wait_seconds(401) > 0


def test_the_window_expires_at_exactly_60_seconds() -> None:
    clock = Clock(NOON_UTC)
    b = budget(clock)
    b.pace(10)
    b.pace(10)
    clock.mono = 59.999
    assert b.wait_seconds(10) > 0
    clock.mono = 60.0
    assert b.wait_seconds(10) == 0
    assert b.window_usage() == (0, 0)


def test_the_wait_is_the_time_left_plus_exactly_half_a_second() -> None:
    clock = Clock(NOON_UTC)
    b = budget(clock)
    b.pace(10)
    b.pace(10)
    clock.mono = 20.0
    assert b.wait_seconds(10) == 40.5
    assert PACE_PAD_S == 0.5


def test_a_correction_changes_the_pacing_of_that_request() -> None:
    clock = Clock(NOON_UTC)
    b = budget(clock)
    first = b.pace(100)
    b_second = b.pace(100)
    clock.mono = 1.0
    b.window_usage()
    b.correct(first, 950)  # Google counted more than estimated for the FIRST request
    assert first.tokens == 950
    assert b_second.tokens == 100  # the other request is untouched
    b.correct(b_second, 50)  # never lowered below the estimate
    assert b_second.tokens == 100


def test_a_correction_makes_the_next_request_wait() -> None:
    clock = Clock(NOON_UTC)
    b = RequestBudget(
        per_run=3, per_day=5, per_minute=5, input_tokens_per_minute=1000,
        monotonic=lambda: clock.mono,
    )  # fmt: skip
    handle = b.pace(100)
    assert b.wait_seconds(500) == 0
    b.correct(handle, 600)
    assert b.wait_seconds(500) > 0  # 600 + 500 > 1000


def test_the_window_is_seeded_with_other_processes_requests() -> None:
    clock = Clock(NOON_UTC)
    clock.mono = 100.0
    b = budget(clock)
    b.seed([(50.0, 10), (10.0, 10)])  # two requests 50 s and 10 s ago
    assert b.window_usage() == (2, 20)
    assert b.wait_seconds(10) == 10.0 + PACE_PAD_S  # the oldest leaves in 10 s


def test_the_estimate_counts_utf8_bytes_over_three() -> None:
    assert estimate_tokens("a" * 300) == 101
    assert estimate_tokens("é" * 300) == 201  # 2 bytes each: a wider margin


def test_the_concurrency_and_lab_ceilings() -> None:
    assert CEILINGS["concurrency"] == 8
    assert CEILINGS["lab_per_run"] == 3
