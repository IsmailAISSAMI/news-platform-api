"""Hard request limits for the Gemini free tier. No network: the budget is pure code."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.analysis.budget import BudgetExceededError, RequestBudget


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at
        self.mono = 0.0


def budget(path: Path, clock: Clock, per_run: int = 3) -> RequestBudget:
    return RequestBudget(
        usage_file=path,
        per_run=per_run,
        per_day=5,
        per_minute=2,
        input_tokens_per_minute=1000,
        now=lambda: clock.at,
        monotonic=lambda: clock.mono,
    )


NOON_UTC = datetime(2026, 11, 24, 12, tzinfo=UTC)


def test_a_run_gets_at_most_its_limit_of_answers(tmp_path: Path) -> None:
    b = budget(tmp_path / "usage.json", Clock(NOON_UTC), per_run=2)
    b.reserve(10)
    b.record_result(ok=False, input_tokens=None, output_tokens=None, busy=True)  # 503
    for _ in range(2):
        b.reserve(10)
        b.record_result(ok=True, input_tokens=10, output_tokens=10)

    with pytest.raises(BudgetExceededError, match="per-run"):
        b.reserve(10)
    assert b.used_today() == 3  # the refused request was not counted


def test_the_daily_limit_is_shared_by_separate_runs(tmp_path: Path) -> None:
    clock = Clock(NOON_UTC)
    path = tmp_path / "usage.json"
    first, second = budget(path, clock), budget(path, clock)
    for _ in range(3):
        first.reserve(10)
    second.reserve(10)
    second.reserve(10)

    with pytest.raises(BudgetExceededError, match="daily limit"):
        second.reserve(10)


def test_the_day_resets_at_midnight_pacific_not_utc(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 11, 24, 7, 59, tzinfo=UTC))  # 23:59 on Nov 23 in California
    path = tmp_path / "usage.json"
    b = budget(path, clock, per_run=100)
    for _ in range(5):
        b.reserve(10)
    with pytest.raises(BudgetExceededError):
        b.reserve(10)

    clock.at = datetime(2026, 11, 24, 8, 1, tzinfo=UTC)  # 00:01 Pacific: a new quota day

    b.reserve(10)
    assert b.used_today() == 1


def test_a_request_larger_than_the_token_limit_is_never_sent(tmp_path: Path) -> None:
    b = budget(tmp_path / "usage.json", Clock(NOON_UTC))

    assert b.wait_seconds(5000) == 0  # no point waiting: it can never fit
    with pytest.raises(BudgetExceededError, match="too large"):
        b.reserve(5000)
    assert b.used_today() == 0


def test_per_minute_limits_make_the_caller_wait(tmp_path: Path) -> None:
    clock = Clock(NOON_UTC)
    b = budget(tmp_path / "usage.json", clock)
    b.reserve(600)
    assert b.wait_seconds(300) == 0
    assert b.wait_seconds(500) > 0  # 600 + 500 tokens > 1000 per minute

    b.reserve(300)
    clock.mono = 30
    assert b.wait_seconds(10) > 0  # 2 requests per minute already used

    clock.mono = 61
    assert b.wait_seconds(10) == 0  # the window has moved on


def test_settings_can_lower_the_limits_but_never_raise_them(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="per_day=1001"):
        RequestBudget(
            usage_file=tmp_path / "u.json",
            per_run=3,
            per_day=1001,
            per_minute=10,
            input_tokens_per_minute=8000,
        )
    with pytest.raises(ValueError, match="per_minute=21"):
        RequestBudget(
            usage_file=tmp_path / "u.json",
            per_run=3,
            per_day=10,
            per_minute=21,
            input_tokens_per_minute=8000,
        )


def test_the_log_keeps_requests_failures_and_real_tokens_per_day(tmp_path: Path) -> None:
    clock = Clock(NOON_UTC)
    path = tmp_path / "usage.json"
    b = budget(path, clock)
    b.reserve(10)
    b.record_result(ok=True, input_tokens=4300, output_tokens=2600)
    b.reserve(10)
    b.record_result(ok=False, input_tokens=None, output_tokens=None)

    history = budget(path, clock).history(2)  # a later run reads the same log

    assert [d.day for d in history] == ["2026-11-23", "2026-11-24"]
    assert history[0].requests == 0
    today = history[1]
    assert (today.requests, today.failed, today.input_tokens, today.output_tokens) == (
        2, 1, 4300, 2600,
    )  # fmt: skip
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_first_log_format_is_still_read(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text('{"day": "2026-11-24", "count": 3}')
    b = budget(path, Clock(NOON_UTC))

    assert b.used_today() == 3
    b.reserve(10)
    assert b.used_today() == 4
