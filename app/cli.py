"""`news` command-line interface (run from the project root).

    uv run news ingest                 # collect everything published since the last run
    uv run news ingest --since-days 7  # first run only: how far back to bootstrap

Exit codes: 0 ok · 1 some feeds failed · 2 environment not ready · 3 already running ·
130 interrupted (progress is kept; the next run resumes).
"""

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
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
from app.models.enums import RunStatus

EXIT_OK = 0
EXIT_FEED_FAILURES = 1
EXIT_NOT_READY = 2
EXIT_ALREADY_RUNNING = 3
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news", description="News platform operations.")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser(
        "ingest", help="Collect articles published since the last completed run."
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
    ingest.add_argument("-v", "--verbose", action="store_true", help="Show progress logs.")
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        msg = "must be >= 1"
        raise argparse.ArgumentTypeError(msg)
    return number


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_cli_logging(verbose=args.verbose)
    options = IngestOptions(
        bootstrap_days=args.since_days,
        max_pages=args.max_pages,
        include_news_sitemap=not args.no_sitemap,
    )
    try:
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
    return EXIT_OK if report.status is RunStatus.SUCCEEDED else EXIT_FEED_FAILURES


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
