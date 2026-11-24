"""The `news` CLI: arguments, exit codes and the printed report (no DB, no network)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app import cli
from app.ingestion.readiness import NotReadyError
from app.ingestion.runner import AlreadyRunningError, FeedReport, IngestOptions, RunReport
from app.ingestion.walk import StopReason, WalkResult
from app.models.enums import RunStatus
from app.ops.backup import Backup, BackupError, Manifest, VerifyResult


def test_defaults() -> None:
    args = cli.build_parser().parse_args(["ingest"])

    assert (args.since_days, args.max_pages, args.no_sitemap) == (30, 500, False)


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_non_positive_values_are_rejected(value: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["ingest", "--max-pages", value])


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def sample_report(status: RunStatus = RunStatus.SUCCEEDED) -> RunReport:
    walked = WalkResult(StopReason.CHECKPOINT, pages_fetched=2, items_new=15, items_updated=1)
    capped = WalkResult(StopReason.PAGE_LIMIT, pages_fetched=500, items_new=4990)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    until = datetime(2026, 10, 9, tzinfo=UTC)
    return RunReport(
        run_id=7,
        status=status,
        feeds=[
            FeedReport("hespress-en", "rss", "u1", result=walked, uncovered=(since, until)),
            FeedReport("hespress-en", "news_sitemap", "u2", error="WalkError: HTTP 403"),
            FeedReport("hespress-en", "archive_index", "u3", skipped="deferred"),
            FeedReport("other", "rss", "u4", result=capped),
        ],
    )


def test_format_report() -> None:
    text = cli.format_report(sample_report(), elapsed_s=12.34)

    assert text.startswith("Ingestion run #7: succeeded in 12.3s\n")
    assert "+15 new, 1 updated, 0 skipped, 2 page(s) [checkpoint]" in text
    assert "not covered by RSS: 2026-10-01 to 2026-10-09" in text
    assert "FAILED (None): WalkError: HTTP 403" in text
    assert "skipped: deferred" in text
    assert "incomplete: run again to continue the gap" in text
    assert text.endswith("Total: +5005 new, 1 updated, 502 page(s)\n")


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the database is up and migrated."""

    async def ok(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(cli, "wait_for_database", ok)
    monkeypatch.setattr(cli, "ensure_migrated", ok)
    monkeypatch.setattr(cli, "alembic_head", lambda _path: "head")


def fake_run(outcome: RunReport | BaseException) -> Any:
    async def run(*_: Any, **__: Any) -> RunReport:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run


@pytest.mark.usefixtures("ready")
@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (sample_report(RunStatus.SUCCEEDED), cli.EXIT_OK),
        (sample_report(RunStatus.FAILED), cli.EXIT_FEED_FAILURES),
        (AlreadyRunningError("another run"), cli.EXIT_ALREADY_RUNNING),
    ],
)
async def test_exit_codes(
    monkeypatch: pytest.MonkeyPatch, outcome: RunReport | BaseException, code: int
) -> None:
    monkeypatch.setattr(cli, "run_ingest", fake_run(outcome))

    assert await cli.ingest(IngestOptions()) == code


async def test_not_ready_exits_2_with_guidance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def down(*_: Any, **__: Any) -> None:
        raise NotReadyError("PostgreSQL is not reachable. Is Docker running?")

    monkeypatch.setattr(cli, "wait_for_database", down)

    assert await cli.ingest(IngestOptions()) == cli.EXIT_NOT_READY
    assert "Is Docker running?" in capsys.readouterr().err


@pytest.mark.usefixtures("ready")
async def test_success_prints_the_report_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run_ingest", fake_run(sample_report()))

    await cli.ingest(IngestOptions())

    assert "Ingestion run #7: succeeded" in capsys.readouterr().out


def test_main_maps_ctrl_c_to_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def interrupted(_: IngestOptions) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ingest", interrupted)

    assert cli.main(["ingest"]) == cli.EXIT_INTERRUPTED
    assert "next run resumes" in capsys.readouterr().err


def test_main_passes_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[IngestOptions] = []

    async def capture(options: IngestOptions) -> int:
        seen.append(options)
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "ingest", capture)

    assert cli.main(["ingest", "--since-days", "7", "--max-pages", "20", "--no-sitemap"]) == 0
    assert cli.main(["ingest", "--rewalk-days", "90"]) == 0
    assert seen == [
        IngestOptions(bootstrap_days=7, max_pages=20, include_news_sitemap=False),
        IngestOptions(rewalk_days=90),
    ]


# --- backup / restore-test ---------------------------------------------------------------


def test_backup_and_restore_test_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_backup() -> int:
        calls.append("backup")
        return cli.EXIT_OK

    async def fake_restore() -> int:
        calls.append("restore-test")
        return cli.EXIT_FEED_FAILURES

    monkeypatch.setattr(cli, "backup", fake_backup)
    monkeypatch.setattr(cli, "restore_test", fake_restore)

    assert cli.main(["backup"]) == cli.EXIT_OK
    assert cli.main(["restore-test", "-v"]) == cli.EXIT_FEED_FAILURES
    assert calls == ["backup", "restore-test"]


async def test_restore_test_without_backups_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "list_backups", lambda _dir: [])

    assert await cli.restore_test() == cli.EXIT_NOT_READY
    assert "Run `uv run news backup` first" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotReadyError("db down"), cli.EXIT_NOT_READY),
        (AlreadyRunningError("ingest running"), cli.EXIT_ALREADY_RUNNING),
        (BackupError("pg_dump failed: disk full"), cli.EXIT_FEED_FAILURES),
    ],
)
async def test_backup_errors_map_to_exit_codes(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: int
) -> None:
    async def ok(*_: Any, **__: Any) -> None:
        return None

    async def fail(*_: Any, **__: Any) -> None:
        raise error

    monkeypatch.setattr(cli, "wait_for_database", ok)
    monkeypatch.setattr(cli, "create_backup", fail)

    assert await cli.backup() == code


async def test_backup_success_reports_and_asks_for_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dump = tmp_path / "news-x.dump"
    manifest = Manifest(dump.name, datetime.now(UTC), 2048, "h", "rev", {"articles": 5})

    async def ok(*_: Any, **__: Any) -> None:
        return None

    async def created(*_: Any, **__: Any) -> Backup:
        return Backup(dump, manifest)

    monkeypatch.setattr(cli, "wait_for_database", ok)
    monkeypatch.setattr(cli, "create_backup", created)

    assert await cli.backup() == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Backup written" in out
    assert "Not verified yet" in out


@pytest.mark.parametrize(
    ("outcome", "code", "message"),
    [
        ("ok", cli.EXIT_OK, "Verified"),
        ("mismatch", cli.EXIT_FEED_FAILURES, "do not rely on this backup"),
        ("error", cli.EXIT_FEED_FAILURES, "Restore check failed"),
    ],
)
async def test_restore_test_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    code: int,
    message: str,
) -> None:
    backup = Backup(
        tmp_path / "news-x.dump", Manifest("news-x.dump", datetime.now(UTC), 1, "h", "r", {"a": 1})
    )

    async def verify(*_: Any, **__: Any) -> VerifyResult:
        if outcome == "error":
            raise BackupError("pg_restore failed")
        result = VerifyResult(backup)
        if outcome == "mismatch":
            result.mismatches["a"] = (1, 0)
        return result

    monkeypatch.setattr(cli, "list_backups", lambda _dir: [backup])
    monkeypatch.setattr(cli, "verify_backup", verify)

    assert await cli.restore_test() == code
    captured = capsys.readouterr()
    assert message in captured.out + captured.err


@pytest.mark.usefixtures("ready")
async def test_ingest_reminds_about_stale_backups(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run_ingest", fake_run(sample_report()))
    monkeypatch.setattr(cli, "freshness_warning", lambda *_a, **_k: "No backup yet.")

    await cli.ingest(IngestOptions())

    assert "Reminder: No backup yet." in capsys.readouterr().err


def test_gemini_usage_shows_our_limits_and_googles(tmp_path: Path) -> None:
    from app.analysis.budget import RequestBudget  # noqa: PLC0415  # local to this test

    budget = RequestBudget(
        usage_file=tmp_path / "usage.json",
        per_run=3,
        per_day=1000,
        per_minute=10,
        input_tokens_per_minute=8000,
    )
    budget.reserve(10)
    budget.record_result(ok=True, input_tokens=4300, output_tokens=2600)

    text = cli.format_usage(budget)

    assert "Today: 1/1000 requests (999 left)." in text
    assert "4300" in text
    assert "Google's free tier: 14400/day, 30/min, 16000 input tokens/min." in text
    assert "NO billing account" in text
