"""`news analyze`: analyse every stored article with Gemma 4 31B, in parallel, resumably.

Design (owner rules, docs/architecture.md 5.5):
- Throughput: several articles in flight at once (`concurrency`); the shared client
  admits requests one at a time under the budget, so parallelism never passes a limit
  (2/3 of Google's per-minute limits, 1,000 requests per day).
- Resumable: each article's result is committed as soon as it is known. A crash, a
  Ctrl+C, a closed laptop or the daily limit loses at most the requests in flight; the
  next run picks up whatever is not done yet. Nothing is analysed twice for the same
  analyzer version (model + prompt + schema).
- Safety net for unusable answers (cut off, invalid JSON, schema errors): the article
  is retried later in the same run, up to `MAX_ATTEMPTS` in total, every attempt
  stored with its raw answer. After that it waits for `--retry-failed`.
- Never breaks on Google's "busy" errors: the client waits them out (shared cooldown).
  Only a limit, a quota error or a hard error stops sending; the report is still
  written and the remaining articles stay pending.
- One run at a time (Postgres advisory lock), newest articles first.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import Row, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.cleanup import clean
from app.analysis.evidence import semantic_issues, verify
from app.analysis.llm import (
    GeminiClient,
    LLMOutputError,
    LLMRequestTooLargeError,
    LLMUnavailableError,
)
from app.analysis.prompt import PROMPT_VERSION, SYSTEM_PROMPT, prepare
from app.analysis.schema import SCHEMA_VERSION
from app.analysis.schema import ArticleAnalysis as AnalysisSchema
from app.models import Article, ArticleAnalysis, ArticleBody, Source

logger = logging.getLogger(__name__)

ANALYZE_LOCK_KEY = 0x6E65_7761  # "newa": one `news analyze` at a time
MAX_ATTEMPTS = 3  # unusable answers per article before waiting for --retry-failed


class AlreadyAnalyzingError(Exception):
    """Another `news analyze` holds the lock."""


def analyzer_version(model: str) -> str:
    return f"{model}|p{PROMPT_VERSION}|s{SCHEMA_VERSION}"


@dataclass(frozen=True)
class AnalyzeOptions:
    concurrency: int = 8
    limit: int | None = None  # at most this many articles this run
    retry_failed: bool = False  # also retry articles that used all their attempts


@dataclass
class AnalyzeReport:
    queued: int = 0
    succeeded: int = 0
    failed: int = 0  # used all attempts this run
    retried: int = 0  # unusable answers sent back to the queue
    skipped: int = 0  # too long for the free tier
    not_sent: int = 0  # still queued when the run stopped
    stop_reason: str | None = None
    elapsed_s: float = 0.0
    pending_after: int = 0
    durations: list[float] = field(default_factory=list)


async def pending_article_ids(
    session: AsyncSession, version: str, *, retry_failed: bool = False, limit: int | None = None
) -> list[int]:
    """Articles with text and no usable analysis for `version`, newest first."""
    done = ArticleAnalysis.status == "succeeded"
    exhausted = and_(ArticleAnalysis.status == "failed", ArticleAnalysis.attempts >= MAX_ATTEMPTS)
    blocked = or_(done, ArticleAnalysis.status == "skipped")
    if not retry_failed:
        blocked = or_(blocked, exhausted)
    query = (
        select(Article.id)
        .join(ArticleBody, ArticleBody.article_id == Article.id)
        .outerjoin(
            ArticleAnalysis,
            and_(
                ArticleAnalysis.article_id == Article.id,
                ArticleAnalysis.analyzer_version == version,
            ),
        )
        .where(or_(ArticleAnalysis.id.is_(None), ~blocked))
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


async def run_analyze(
    session_factory: async_sessionmaker[AsyncSession],
    llm: GeminiClient,
    options: AnalyzeOptions,
) -> AnalyzeReport:
    async with session_factory() as lock_session:
        if not await lock_session.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ANALYZE_LOCK_KEY}
        ):
            msg = "another `news analyze` is already running"
            raise AlreadyAnalyzingError(msg)
        try:
            return await _run_locked(session_factory, llm, options, lock_session)
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ANALYZE_LOCK_KEY}
            )
            await lock_session.commit()


async def _run_locked(
    session_factory: async_sessionmaker[AsyncSession],
    llm: GeminiClient,
    options: AnalyzeOptions,
    session: AsyncSession,
) -> AnalyzeReport:
    version = analyzer_version(llm.model)
    ids = await pending_article_ids(
        session, version, retry_failed=options.retry_failed, limit=options.limit
    )
    report = AnalyzeReport(queued=len(ids))
    queue: asyncio.Queue[int] = asyncio.Queue()
    for article_id in ids:
        queue.put_nowait(article_id)
    logger.info(
        "%d articles to analyse with %s, %d in parallel · %s",
        len(ids), llm.model, options.concurrency, await llm.usage_line(),
    )  # fmt: skip

    run = _Run(session_factory, llm, version, report, queue, time.monotonic())
    workers = [asyncio.create_task(run.worker()) for _ in range(max(1, options.concurrency))]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker in workers:
            worker.cancel()
        report.not_sent = queue.qsize()
        report.elapsed_s = time.monotonic() - run.started
        report.pending_after = len(
            await pending_article_ids(session, version, retry_failed=options.retry_failed)
        )
    return report


@dataclass
class _Run:
    session_factory: async_sessionmaker[AsyncSession]
    llm: GeminiClient
    version: str
    report: AnalyzeReport
    queue: asyncio.Queue[int]
    started: float
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def worker(self) -> None:
        while not self.stop.is_set():
            try:
                article_id = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await self.analyse(article_id)

    async def analyse(self, article_id: int) -> None:
        async with self.session_factory() as session:
            row = await _load(session, article_id, self.version)
            if row is None:  # the article lost its text since the run started
                return
            article, body, source, previous = row
            attempts = (previous.attempts if previous else 0) + 1
            prepared = prepare(
                article.title, article.published_at.date(), source.name, body.body_text
            )
            values: dict[str, Any] = {"attempts": attempts, "error": None}
            try:
                completion = await self.llm.structured(
                    AnalysisSchema, system=SYSTEM_PROMPT, user=prepared.render()
                )
            except LLMRequestTooLargeError as exc:
                values |= {"status": "skipped", "error": str(exc)}
                self.report.skipped += 1
                outcome = f"skipped #{article_id}: too long for the free tier"
            except LLMOutputError as exc:
                values |= {"status": "failed", "error": str(exc), "raw_output": exc.raw}
                if attempts < MAX_ATTEMPTS:
                    self.queue.put_nowait(article_id)  # safety net: try again later
                    self.report.retried += 1
                    outcome = f"✗ #{article_id} unusable answer ({exc}); retry {attempts + 1}"
                else:
                    self.report.failed += 1
                    outcome = f"✗ #{article_id} failed {attempts} times; left for --retry-failed"
            except LLMUnavailableError as exc:
                # A limit, Google's quota or a hard error: stop sending, keep the article.
                self.queue.put_nowait(article_id)
                self.report.stop_reason = str(exc)
                self.stop.set()
                logger.warning("Stopping: %s", exc)
                return
            else:
                analysis = completion.parsed
                paragraphs = prepared.paragraphs
                cleaned, cleanup = clean(
                    analysis, paragraphs, publisher_names=(source.name, source.name.split()[0])
                )
                values |= {
                    "status": "succeeded",
                    "raw_output": completion.raw,
                    "analysis": analysis.model_dump(mode="json"),
                    "cleaned": cleaned.model_dump(mode="json"),
                    "corrections": [asdict(c) for c in cleanup.items],
                    "review_flags": [asdict(f) for f in cleanup.flags],
                    "evidence": {
                        **{k: v.value for k, v in verify(analysis, paragraphs).statuses.items()},
                        **{
                            f"issue:{k}": v.value
                            for k, v in semantic_issues(analysis, paragraphs).items()
                        },
                    },
                    "input_tokens": completion.prompt_tokens,
                    "output_tokens": completion.completion_tokens,
                    "duration_s": round(completion.duration_s, 1),
                }
                self.report.succeeded += 1
                self.report.durations.append(completion.duration_s)
                outcome = f"✓ #{article_id} in {completion.duration_s:.0f}s"
            await _save(session, article_id, self.version, self.llm.model, values)
        logger.info("%s · %s · %s", outcome, self.progress(), await self.llm.usage_line())

    def progress(self) -> str:
        r = self.report
        done = r.succeeded + r.failed + r.skipped
        hours = (time.monotonic() - self.started) / 3600
        rate = r.succeeded / hours if hours > 0 else 0.0
        left = self.queue.qsize()
        eta = f"ETA {left / rate:.1f}h" if rate > 0 else "ETA -"
        return f"[{done}/{r.queued}] {rate:.0f} articles/h, {left} queued, {eta}"


async def _load(
    session: AsyncSession, article_id: int, version: str
) -> Row[Article, ArticleBody, Source, ArticleAnalysis | None] | None:
    return (
        await session.execute(
            select(Article, ArticleBody, Source, ArticleAnalysis)
            .join(ArticleBody, ArticleBody.article_id == Article.id)
            .join(Source, Source.id == Article.source_id)
            .outerjoin(
                ArticleAnalysis,
                and_(
                    ArticleAnalysis.article_id == Article.id,
                    ArticleAnalysis.analyzer_version == version,
                ),
            )
            .where(Article.id == article_id)
        )
    ).first()


async def _save(
    session: AsyncSession, article_id: int, version: str, model: str, values: dict[str, Any]
) -> None:
    """Insert or update this article's analysis for `version`, and commit at once."""
    statement = insert(ArticleAnalysis).values(
        article_id=article_id, analyzer_version=version, model=model, **values
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ArticleAnalysis.article_id, ArticleAnalysis.analyzer_version],
            set_={**values, "updated_at": func.now()},
        )
    )
    await session.commit()
