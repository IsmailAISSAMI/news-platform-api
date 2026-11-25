"""Pacing and per-run limits for the Gemini free tier. No network, no database.

The daily limit is counted in the database (tests/integration/test_ledger.py).
"""

from datetime import UTC, datetime

import pytest

from app.analysis.budget import (
    CEILINGS,
    BudgetExceededError,
    RequestBudget,
    RequestTooLargeError,
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
        "per_day": 1000,
        "per_minute": 20,
        "input_tokens_per_minute": 10_000,
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
