"""`news` command-line interface (run from the project root).

    uv run news ingest                 # collect everything published since the last run
    uv run news ingest --since-days 7  # first run only: how far back to bootstrap
    uv run news backup                 # pg_dump into ~/Backups/news (keeps 14)
    uv run news restore-test           # prove the newest backup restores

Exit codes: 0 ok · 1 failed (feeds, backup or restore check) · 2 environment not ready ·
3 already running · 130 interrupted (progress is kept; the next run resumes).
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from app.analysis.budget import CEILINGS, GOOGLE_LIMITS, RequestBudget
from app.analysis.compare import render as render_comparison
from app.analysis.compare import score_run
from app.analysis.lab import run_lab
from app.analysis.llm import GeminiClient, LLMUnavailableError
from app.analysis.pipeline import (
    MAX_ATTEMPTS,
    AlreadyAnalyzingError,
    AnalyzeOptions,
    AnalyzeReport,
    run_analyze,
)
from app.core.config import Settings, get_settings
from app.core.logging import configure_cli_logging
from app.db.session import create_engine, create_session_factory
from app.ingestion.fetch import FetcherConfig, PoliteFetcher, create_http_client
from app.ingestion.readiness import (
    NotReadyError,
    alembic_head,
    ensure_migrated,
    wait_for_database,
)
from app.ingestion.runner import AlreadyRunningError, IngestOptions, RunReport, run_ingest
from app.models import ArticleBody
from app.models.enums import RunStatus
from app.ops.backup import (
    BackupError,
    PgTools,
    create_backup,
    freshness_warning,
    list_backups,
    verify_backup,
)

EXIT_OK = 0
EXIT_FEED_FAILURES = 1
EXIT_NOT_READY = 2
EXIT_ALREADY_RUNNING = 3
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news", description="News platform operations.")
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="Show progress logs.")

    ingest = commands.add_parser(
        "ingest",
        parents=[common],
        help="Collect articles published since the last completed run.",
    )
    ingest.add_argument(
        "--since-days",
        type=_positive_int,
        default=30,
        help="First run of a feed only: how many days back to collect (default 30).",
    )
    ingest.add_argument(
        "--max-pages",
        type=_positive_int,
        default=500,
        help="Safety cap on RSS pages per feed (default 500, ~42 min at 5 s/page).",
    )
    ingest.add_argument(
        "--no-sitemap", action="store_true", help="Skip the news sitemap (last ~48 h)."
    )
    ingest.add_argument(
        "--rewalk-days",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Re-read the last N days, even articles already stored (e.g. to fetch "
        "article text for older articles). Slower; never skips anything.",
    )
    lab = commands.add_parser(
        "lab",
        parents=[common],
        help="Try the AI analysis on a few articles (results go to ~/news-eval/lab).",
    )
    lab.add_argument("ids", nargs="+", type=_positive_int, help="Article ids.")
    lab.add_argument("--label", default="", help="Suffix for the output folder name.")
    analyze = commands.add_parser(
        "analyze",
        parents=[common],
        help="Analyse stored articles with Gemma 4 31B (parallel, resumable, within limits).",
    )
    analyze.add_argument(
        "--limit", type=_positive_int, default=None, metavar="N", help="At most N articles."
    )
    analyze.add_argument(
        "--concurrency",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Articles in flight at once (default: GEMINI_CONCURRENCY).",
    )
    analyze.add_argument(
        "--retry-failed",
        action="store_true",
        help=f"Also retry articles whose answers were unusable {MAX_ATTEMPTS} times.",
    )
    commands.add_parser(
        "gemini-usage",
        parents=[common],
        help="Show Gemini API use for the last 7 days against our limits and Google's.",
    )
    compare = commands.add_parser(
        "lab-compare",
        parents=[common],
        help="Score lab runs side by side with the current code checks.",
    )
    compare.add_argument("runs", nargs="+", type=Path, help="Lab run directories.")
    commands.add_parser(
        "backup", parents=[common], help="Dump the database to the backup directory."
    )
    commands.add_parser(
        "restore-test",
        parents=[common],
        help="Restore the newest backup into a scratch database and verify row counts.",
    )
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        msg = "must be >= 1"
        raise argparse.ArgumentTypeError(msg)
    return number


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911  # one return per command
    args = build_parser().parse_args(argv)
    configure_cli_logging(verbose=args.verbose)
    try:
        if args.command == "gemini-usage":
            return gemini_usage()
        if args.command == "analyze":
            logging.getLogger("app.analysis").setLevel(logging.INFO)  # live progress
            return asyncio.run(
                analyze(args.limit, args.concurrency, retry_failed=args.retry_failed)
            )
        if args.command == "lab":
            # Always show live progress: attempts, waits, and passed/failed counts.
            logging.getLogger("app.analysis").setLevel(logging.INFO)
            return asyncio.run(lab(args.ids, args.label))
        if args.command == "lab-compare":
            return asyncio.run(lab_compare(args.runs))
        if args.command == "backup":
            return asyncio.run(backup())
        if args.command == "restore-test":
            return asyncio.run(restore_test())
        options = IngestOptions(
            bootstrap_days=args.since_days,
            max_pages=args.max_pages,
            include_news_sitemap=not args.no_sitemap,
            rewalk_days=args.rewalk_days,
        )
        return asyncio.run(ingest(options))
    except KeyboardInterrupt:
        _err("Interrupted. Work done so far is saved; the next run resumes the gap.")
        return EXIT_INTERRUPTED


async def ingest(options: IngestOptions) -> int:
    settings = get_settings()
    engine = create_engine(settings)
    started = time.monotonic()
    try:
        await wait_for_database(engine)
        async with engine.connect() as connection:
            await ensure_migrated(connection, alembic_head(Path.cwd() / "alembic.ini"))

        session_factory = create_session_factory(engine)
        async with create_http_client(settings) as client, session_factory() as session:
            fetcher = PoliteFetcher(client, FetcherConfig.from_settings(settings))
            report = await run_ingest(session, fetcher, now=datetime.now(UTC), options=options)
    except NotReadyError as exc:
        _err(f"Not ready: {exc}")
        return EXIT_NOT_READY
    except AlreadyRunningError as exc:
        _err(f"Not started: {exc}.")
        return EXIT_ALREADY_RUNNING
    finally:
        await engine.dispose()

    sys.stdout.write(format_report(report, elapsed_s=time.monotonic() - started))
    reminder = freshness_warning(
        settings.backup_dir,
        max_age=timedelta(days=settings.backup_max_age_days),
        now=datetime.now(UTC),
    )
    if reminder:
        _err(f"Reminder: {reminder}")
    return EXIT_OK if report.status is RunStatus.SUCCEEDED else EXIT_FEED_FAILURES


async def lab(ids: list[int], label: str) -> int:
    settings = get_settings()
    if settings.gemini_api_key is None:
        _err("Not ready: GEMINI_API_KEY is not set in .env")
        return EXIT_NOT_READY
    budget = _budget(settings)
    sys.stderr.write(
        f"Gemini budget: at most {budget.per_run} requests this run; "
        f"{budget.used_today()}/{budget.per_day} used today (Pacific time).\n"
    )
    engine = create_engine(settings, read_only=True)
    try:
        async with httpx.AsyncClient() as client:
            llm = GeminiClient(
                client,
                base_url=settings.gemini_base_url,
                model=settings.gemini_model,
                api_key=settings.gemini_api_key,
                budget=budget,
            )
            async with create_session_factory(engine)() as session:
                out, results = await run_lab(session, llm, ids, label=label)
    except LLMUnavailableError as exc:
        _err(f"Stopped: {exc}")
        return EXIT_NOT_READY
    finally:
        await engine.dispose()
    sys.stdout.write((out / "summary.md").read_text() + f"\nFull results: {out}\n")
    return EXIT_OK if all(r.ok for r in results) else EXIT_FEED_FAILURES


def gemini_usage() -> int:
    sys.stdout.write(format_usage(_budget(get_settings())))
    return EXIT_OK


def format_usage(budget: RequestBudget) -> str:
    lines = [
        f"Gemini API usage (quota days, Pacific time; log: {budget.usage_file})",
        "",
        f"{'day':<12}{'sent':>6}{'passed':>8}{'failed':>8}{'busy':>6}"
        f"{'tokens in':>12}{'tokens out':>12}",
    ]
    lines += [
        f"{d.day:<12}{d.requests:>6}{d.requests - d.failed:>8}{d.failed:>8}{d.busy:>6}"
        f"{d.input_tokens:>12}{d.output_tokens:>12}"
        for d in budget.history(7)
    ]
    used = budget.used_today()
    lines += [
        "",
        f"Today: {used}/{budget.per_day} requests ({budget.per_day - used} left).",
        f"Our limits:  {budget.per_run} answered/run, {budget.per_day}/day, "
        f"{budget.per_minute}/min, "
        f"{budget.input_tokens_per_minute} input tokens/min (hard ceilings: "
        f"{CEILINGS['per_day']}/day, {CEILINGS['per_minute']}/min, "
        f"{CEILINGS['input_tokens_per_minute']} tokens/min).",
        f"Google's free tier: {GOOGLE_LIMITS['per_day']}/day, "
        f"{GOOGLE_LIMITS['per_minute']}/min, "
        f"{GOOGLE_LIMITS['input_tokens_per_minute']} input tokens/min.",
        "Free only while the Google project has NO billing account linked.",
    ]
    return "\n".join(lines) + "\n"


async def analyze(limit: int | None, concurrency: int | None, *, retry_failed: bool) -> int:
    settings = get_settings()
    if settings.gemini_api_key is None:
        _err("Not ready: GEMINI_API_KEY is not set in .env")
        return EXIT_NOT_READY
    engine = create_engine(settings)
    try:
        await wait_for_database(engine)
        async with engine.connect() as connection:
            await ensure_migrated(connection, alembic_head(Path.cwd() / "alembic.ini"))
        # A bulk run: the per-run cap is the daily cap (tests keep their 3 via `news lab`).
        budget = _budget(settings, per_run=settings.gemini_max_requests_per_day)
        async with httpx.AsyncClient() as client:
            llm = GeminiClient(
                client,
                base_url=settings.gemini_base_url,
                model=settings.gemini_model,
                api_key=settings.gemini_api_key,
                budget=budget,
            )
            options = AnalyzeOptions(
                concurrency=concurrency or settings.gemini_concurrency,
                limit=limit,
                retry_failed=retry_failed,
            )
            report = await run_analyze(create_session_factory(engine), llm, options)
    except NotReadyError as exc:
        _err(f"Not ready: {exc}")
        return EXIT_NOT_READY
    except AlreadyAnalyzingError as exc:
        _err(f"Not started: {exc}.")
        return EXIT_ALREADY_RUNNING
    finally:
        await engine.dispose()
    sys.stdout.write(format_analyze_report(report, budget))
    return EXIT_OK if report.stop_reason is None else EXIT_FEED_FAILURES


def format_analyze_report(report: AnalyzeReport, budget: RequestBudget) -> str:
    hours, rest = divmod(int(report.elapsed_s), 3600)
    average = (
        f"{sum(report.durations) / len(report.durations):.0f}s per answer"
        if report.durations
        else "no answers"
    )
    lines = [
        f"Analysis run: {report.queued} queued, {hours}h{rest // 60:02d}m, {average}.",
        f"  succeeded {report.succeeded}, failed {report.failed}, "
        f"retried {report.retried}, skipped (too long) {report.skipped}, "
        f"not sent {report.not_sent}",
        f"  still pending: {report.pending_after} (the next run continues from there)",
    ]
    if report.stop_reason:
        lines.append(f"  stopped: {report.stop_reason}")
    lines.append(f"  Gemini {format_usage(budget).splitlines()[-4]}")
    return "\n".join(lines) + "\n"


def _budget(settings: Settings, *, per_run: int | None = None) -> RequestBudget:
    return RequestBudget(
        usage_file=settings.gemini_usage_file,
        per_run=per_run or settings.gemini_max_requests_per_run,
        per_day=settings.gemini_max_requests_per_day,
        per_minute=settings.gemini_max_requests_per_minute,
        input_tokens_per_minute=settings.gemini_max_input_tokens_per_minute,
    )


async def lab_compare(runs: list[Path]) -> int:
    missing = [str(run) for run in runs if not run.is_dir()]
    if missing:
        _err(f"Not a lab run directory: {', '.join(missing)}")
        return EXIT_NOT_READY
    settings = get_settings()
    engine = create_engine(settings, read_only=True)
    ids = {
        json.loads(path.read_text())["article_id"]
        for run in runs
        for path in run.glob("article-*.json")
    }
    try:
        async with create_session_factory(engine)() as session:
            rows = await session.execute(
                select(ArticleBody.article_id, ArticleBody.body_text).where(
                    ArticleBody.article_id.in_(ids)
                )
            )
            paragraphs = {
                article_id: [p for p in text.split("\n\n") if p.strip()]
                for article_id, text in rows.all()
            }
    finally:
        await engine.dispose()
    sys.stdout.write(render_comparison([score_run(run, paragraphs) for run in runs]))
    return EXIT_OK


async def backup() -> int:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        await wait_for_database(engine)
        created = await create_backup(
            engine,
            PgTools.from_settings(settings),
            settings.backup_dir,
            keep=settings.backup_keep,
            now=datetime.now(UTC),
        )
    except NotReadyError as exc:
        _err(f"Not ready: {exc}")
        return EXIT_NOT_READY
    except AlreadyRunningError as exc:
        _err(f"Not started: {exc}.")
        return EXIT_ALREADY_RUNNING
    except BackupError as exc:
        _err(f"Backup failed: {exc}")
        return EXIT_FEED_FAILURES
    finally:
        await engine.dispose()

    rows = sum(created.manifest.row_counts.values())
    sys.stdout.write(
        f"Backup written: {created.dump} ({created.manifest.size_bytes / 1024:.0f} KiB, "
        f"{rows} rows in {len(created.manifest.row_counts)} tables)\n"
        "Not verified yet: run `uv run news restore-test`.\n"
    )
    return EXIT_OK


async def restore_test() -> int:
    settings = get_settings()
    backups = list_backups(settings.backup_dir)
    if not backups:
        _err(f"No backup in {settings.backup_dir}. Run `uv run news backup` first.")
        return EXIT_NOT_READY
    newest = backups[0]
    try:
        result = await verify_backup(
            newest, PgTools.from_settings(settings), settings, now=datetime.now(UTC)
        )
    except BackupError as exc:
        _err(f"Restore check failed: {exc}")
        return EXIT_FEED_FAILURES

    if not result.ok:
        for table, (expected, actual) in sorted(result.mismatches.items()):
            _err(f"  {table}: expected {expected} rows, restored {actual}")
        _err(f"Restore check FAILED for {newest.dump.name}: do not rely on this backup.")
        return EXIT_FEED_FAILURES
    sys.stdout.write(
        f"Verified: {newest.dump.name} restores completely "
        f"({len(newest.manifest.row_counts)} tables, row counts match).\n"
    )
    return EXIT_OK


def format_report(report: RunReport, *, elapsed_s: float) -> str:
    lines = [f"Ingestion run #{report.run_id}: {report.status} in {elapsed_s:.1f}s"]
    for feed in report.feeds:
        label = f"  {feed.source:<14} {feed.kind:<14}"
        if feed.skipped:
            lines.append(f"{label} skipped: {feed.skipped}")
        elif feed.error:
            lines.append(f"{label} FAILED ({feed.state}): {feed.error}")
        elif feed.result is not None:
            r = feed.result
            lines.append(
                f"{label} +{r.items_new} new, {r.items_updated} updated, "
                f"{r.items_skipped} skipped, {r.pages_fetched} page(s) "
                f"[{r.stop_reason}]"
            )
            if not r.complete:
                lines.append(f"{'':<32} incomplete: run again to continue the gap")
        if feed.uncovered:
            since, until = (moment.date().isoformat() for moment in feed.uncovered)
            lines.append(f"{'':<32} not covered by RSS: {since} to {until} (archive backlog)")
    lines.append(
        f"Total: +{report.total('items_new')} new, {report.total('items_updated')} updated, "
        f"{report.total('pages_fetched')} page(s)"
    )
    return "\n".join(lines) + "\n"


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
