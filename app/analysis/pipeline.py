"""`news analyze` and `news lab`: analyse articles with Gemma 4 31B, resumably.

One analysis flow, `analyse_article`, is used by both commands, so the lab measures
exactly what `news analyze` stores. The lab only writes files; `analyze` stores rows.

Design (owner rules, docs/architecture.md 5.5):
- Throughput: several articles in flight at once (`concurrency`); the shared client
  admits requests one at a time under the budget, so parallelism never passes a limit
  (2/3 of Google's per-minute limits, 1,000 requests per day).
- Resumable: each article's result is committed as soon as it is known. A crash, a
  Ctrl+C, a closed laptop or the daily limit loses at most the requests in flight; the
  next run picks up whatever is not done yet. Nothing is analysed twice for the same
  analyzer version (model + prompt + schema).
- Safety net for unusable answers (cut off, invalid JSON, schema errors): the article
  is retried later in the same run, up to `MAX_ATTEMPTS` in total. The row keeps only
  the LAST attempt's raw answer; every attempt is summarised in `attempt_history`
  (status, error, finish reason, tokens, time; no raw text), appended, never replaced.
  A cut-off answer is retried with a compact-output
  instruction (the same request would be cut off again). After the last attempt it
  waits for `--retry-failed`.
- Never breaks on Google's "busy" errors: the client waits them out (shared cooldown).
  Only a limit, a quota error or a hard error stops sending; the report is still
  written and the remaining articles stay pending.
- One run at a time (Postgres advisory lock), newest articles first. Every run writes
  an `analysis_runs` row when it starts and its counters when it finishes; a crash
  leaves `finished_at` NULL.

Status rule (`analysis_status`), the only place that decides it:
- `needs_review` when at least one evidence quote of the model's answer is UNVERIFIED:
  its status is fabricated (not in the article), misplaced (only in another paragraph),
  bad_paragraph (cites a paragraph that does not exist and the quote is not in the
  article) or too_short (proves nothing). These paths, into the model's answer, are
  stored sorted in `unverified_paths`, and the statuses in `evidence`. Such an answer is
  kept for a person to check, and it is never trusted as `succeeded`.
- Quarantine works on the CLEANED analysis, the object the briefing is built from:
  cleanup drops and merges items, so raw indexes would point at the wrong item. The
  cleaned analysis is verified again (statuses stored in `evidence` under `cleaned:`)
  and its unverified items are removed from the briefing.
- `succeeded` only when `unverified_paths` is empty.
- Semantic issues (`Issue` values such as a name that is not in the article) are
  review flags only: they never change the status.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy import and_, cast, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.briefing import make_briefing
from app.analysis.budget import BudgetExceededError
from app.analysis.cleanup import CleanupReport, clean
from app.analysis.evidence import EvidenceReport, EvidenceStatus, Issue, semantic_issues, verify
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
from app.models.analysis_run import AnalysisRun

logger = logging.getLogger(__name__)

ANALYZE_LOCK_KEY = 0x6E65_7761  # "newa": one `news analyze` at a time
MAX_ATTEMPTS = 3  # unusable answers per article before waiting for --retry-failed
CUT_OFF_PREFIX = "answer cut off"  # start of the LLMOutputError message for a cut-off answer
COMPACT_INSTRUCTION = (
    "Keep every string short. Output the JSON object only, with no reasoning before it."
)
UNVERIFIED = frozenset(
    {
        EvidenceStatus.FABRICATED,
        EvidenceStatus.MISPLACED,
        EvidenceStatus.BAD_PARAGRAPH,
        EvidenceStatus.TOO_SHORT,
    }
)

OutcomeStatus = Literal["succeeded", "needs_review", "failed", "skipped", "stopped", "missing"]


class AlreadyAnalyzingError(Exception):
    """Another `news analyze` holds the lock."""


def analyzer_version(model: str) -> str:
    return f"{model}|p{PROMPT_VERSION}|s{SCHEMA_VERSION}"


def is_cut_off(error: str | None) -> bool:
    return error is not None and error.startswith(CUT_OFF_PREFIX)


def analysis_status(unverified_paths: list[str]) -> Literal["succeeded", "needs_review"]:
    """The status rule: any unverified quote means a person must look first."""
    return "needs_review" if unverified_paths else "succeeded"


@dataclass
class AnalysisOutcome:
    """The result of one analysis attempt, before anything is stored."""

    status: OutcomeStatus
    outcome: str  # one human-readable line
    title: str = "?"
    url: str | None = None
    published: date | None = None
    error: str | None = None
    raw_output: str | None = None
    duration_s: float = 0.0
    tokens_per_s: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    analysis: AnalysisSchema | None = None  # the model's answer, unchanged
    cleaned: AnalysisSchema | None = None
    cleanup: CleanupReport | None = None
    evidence: EvidenceReport | None = None
    issues: dict[str, Issue] = field(default_factory=dict)
    unverified_paths: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)  # article_analyses columns


@dataclass(frozen=True)
class AnalyzeOptions:
    concurrency: int = 8
    limit: int | None = None  # at most this many articles this run
    retry_failed: bool = False  # also retry articles that used all their attempts


@dataclass
class AnalyzeReport:
    queued: int = 0
    succeeded: int = 0
    needs_review: int = 0  # answered, but with unverified quotes: a person must check
    failed: int = 0  # used all attempts this run
    retried: int = 0  # unusable answers sent back to the queue
    skipped: int = 0  # too long for the free tier
    not_sent: int = 0  # still queued when the run stopped
    stop_reason: str | None = None
    elapsed_s: float = 0.0
    pending_after: int = 0
    run_id: int | None = None
    durations: list[float] = field(default_factory=list)


async def analyse_article(
    session: AsyncSession,
    llm: GeminiClient,
    article_id: int,
    version: str,
    *,
    compact: bool = False,
) -> AnalysisOutcome:
    """Send one article to the model once, verify the answer, and return the result.

    Pure with respect to storage: it reads the article and calls the model, but writes
    nothing. `compact` appends COMPACT_INSTRUCTION to the user message (used for the
    retry after a cut-off answer). `version` is the analyzer version the result belongs to.
    """
    row = (
        await session.execute(
            select(Article, ArticleBody, Source)
            .join(ArticleBody, ArticleBody.article_id == Article.id)
            .join(Source, Source.id == Article.source_id)
            .where(Article.id == article_id)
        )
    ).first()
    if row is None:
        return AnalysisOutcome("missing", f"#{article_id} has no body text")
    article, body, source = row
    prepared = prepare(article.title, article.published_at.date(), source.name, body.body_text)
    user = prepared.render()
    strategy = "compact"
    if compact:
        user += f"\n\n{COMPACT_INSTRUCTION}"
    else:
        strategy = "standard"
    base: dict[str, Any] = {
        "title": article.title,
        "url": article.canonical_url,
        "published": article.published_at.date(),
    }
    try:
        completion = await llm.structured(AnalysisSchema, system=SYSTEM_PROMPT, user=user)
    except LLMRequestTooLargeError as exc:
        return AnalysisOutcome(
            "skipped", f"skipped #{article_id}: too long for the free tier", error=str(exc),
            values={"status": "skipped", "error": str(exc)}, **base,
        )  # fmt: skip
    except (LLMUnavailableError, BudgetExceededError) as exc:  # a limit or a hard stop
        return AnalysisOutcome(
            "stopped", f"stopped at #{article_id}: {exc}", error=str(exc), **base
        )
    except LLMOutputError as exc:
        return AnalysisOutcome(
            "failed", f"#{article_id} unusable answer ({exc}) [{strategy}]", error=str(exc),
            raw_output=exc.raw,
            values={"status": "failed", "error": str(exc), "raw_output": exc.raw}, **base,
        )  # fmt: skip

    analysis = completion.parsed
    paragraphs = prepared.paragraphs
    evidence = verify(analysis, paragraphs)
    issues = semantic_issues(analysis, paragraphs, published=article.published_at.date())
    cleaned, cleanup = clean(
        analysis, paragraphs, publisher_names=(source.name, source.name.split()[0]),
        published=article.published_at.date(),
    )  # fmt: skip
    unverified = sorted(p for p, s in evidence.statuses.items() if s in UNVERIFIED)
    status = analysis_status(unverified)
    cleaned_evidence = verify(cleaned, paragraphs)  # same object as the briefing
    quarantined = sorted(p for p, s in cleaned_evidence.statuses.items() if s in UNVERIFIED)
    briefing = make_briefing(
        cleaned.model_dump(mode="json"), quarantined, paragraphs=paragraphs,
        review_required=status == "needs_review",
    )  # fmt: skip
    duration = round(completion.duration_s, 1)
    values: dict[str, Any] = {
        "status": status,
        "error": None,
        "raw_output": completion.raw,
        "analysis": analysis.model_dump(mode="json"),
        "cleaned": cleaned.model_dump(mode="json"),
        "corrections": [asdict(c) for c in cleanup.items],
        "review_flags": [asdict(f) for f in cleanup.flags],
        "evidence": {
            **{k: v.value for k, v in evidence.statuses.items()},
            **{f"issue:{k}": v.value for k, v in issues.items()},
            **{f"cleaned:{k}": v.value for k, v in cleaned_evidence.statuses.items()},
        },
        "unverified_paths": unverified,
        "briefing": briefing,
        "input_tokens": completion.prompt_tokens,
        "output_tokens": completion.completion_tokens,
        "duration_s": duration,
    }
    if status == "needs_review":
        text_out = (
            f"! #{article_id} needs review: {len(unverified)} unverified quote(s) [{strategy}]"
        )
    else:
        text_out = f"✓ #{article_id} in {completion.duration_s:.0f}s [{strategy}]"
    return AnalysisOutcome(
        status, text_out, duration_s=completion.duration_s,
        tokens_per_s=completion.tokens_per_second,
        input_tokens=completion.prompt_tokens, output_tokens=completion.completion_tokens,
        raw_output=completion.raw, analysis=analysis, cleaned=cleaned, cleanup=cleanup,
        evidence=evidence, issues=issues, unverified_paths=unverified, values=values, **base,
    )  # fmt: skip


async def pending_article_ids(
    session: AsyncSession, version: str, *, retry_failed: bool = False, limit: int | None = None
) -> list[int]:
    """Articles with text and no analysis left to do for `version`, newest first.

    Done: `succeeded`, `needs_review` (never re-sent automatically) and `skipped`.
    `failed` is retried until it reaches MAX_ATTEMPTS, unless `retry_failed` is set.
    """
    done = ArticleAnalysis.status.in_(("succeeded", "needs_review"))
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
    concurrency = max(1, options.concurrency)
    report = AnalyzeReport(queued=len(ids))
    report.run_id = await _start_run(session, llm.model, concurrency, len(ids))
    llm.run_id = report.run_id  # every api_calls row of this run points to it
    queue: asyncio.Queue[int] = asyncio.Queue()
    for article_id in ids:
        queue.put_nowait(article_id)
    logger.info(
        "%d articles to analyse with %s, %d in parallel · %s",
        len(ids), llm.model, concurrency, await llm.usage_line(),
    )  # fmt: skip

    run = _Run(session_factory, llm, version, report, queue, time.monotonic())
    workers = [asyncio.create_task(run.worker()) for _ in range(concurrency)]
    try:
        await asyncio.gather(*workers)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Ctrl+C or a cancelled task: an orderly stop, not a crash. Record the run as
        # interrupted, then let the cancellation through.
        report.stop_reason = "interrupted"
        for worker in workers:
            worker.cancel()
        report.not_sent = queue.qsize()
        report.elapsed_s = time.monotonic() - run.started
        await asyncio.shield(_finish_run(session, report))
        raise
    finally:
        for worker in workers:
            worker.cancel()
        report.not_sent = queue.qsize()
        report.elapsed_s = time.monotonic() - run.started
        report.pending_after = len(
            await pending_article_ids(session, version, retry_failed=options.retry_failed)
        )
    # Reached when the walk ended normally (a limit or a stop included); an interruption
    # finished the row above. A crash skips both, so the row keeps finished_at NULL.
    await _finish_run(session, report)
    return report


async def _start_run(session: AsyncSession, model: str, concurrency: int, queued: int) -> int:
    run = AnalysisRun(command="analyze", model=model, concurrency=concurrency, queued=queued)
    session.add(run)
    await session.flush()
    run_id = run.id
    await session.commit()
    return run_id


async def _finish_run(session: AsyncSession, report: AnalyzeReport) -> None:
    assert report.run_id is not None  # noqa: S101  # set by _start_run before any work
    await session.execute(
        update(AnalysisRun)
        .where(AnalysisRun.id == report.run_id)
        .values(
            finished_at=datetime.now(UTC),
            succeeded=report.succeeded,
            needs_review=report.needs_review,
            failed=report.failed,
            skipped=report.skipped,
            stop_reason=report.stop_reason,
        )
    )
    await session.commit()


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
            previous = await _previous(session, article_id, self.version)
            attempts = (previous.attempts if previous else 0) + 1
            # A cut-off answer is not retried as the same request: it would be cut off again.
            compact = previous is not None and is_cut_off(previous.error)
            result = await analyse_article(
                session, self.llm, article_id, self.version, compact=compact
            )
            if result.status == "missing":  # the article lost its text since the run started
                return
            if result.status == "stopped":
                # A limit, Google's quota or a hard error: stop sending, keep the article.
                self.queue.put_nowait(article_id)
                self.report.stop_reason = result.error
                self.stop.set()
                logger.warning("Stopping: %s", result.error)
                return
            outcome = result.outcome
            if result.status == "skipped":
                self.report.skipped += 1
            elif result.status == "failed":
                if attempts < MAX_ATTEMPTS:
                    self.queue.put_nowait(article_id)  # safety net: try again later
                    self.report.retried += 1
                    outcome += f"; retry {attempts + 1}"
                else:
                    self.report.failed += 1
                    outcome += f"; failed {attempts} times, left for --retry-failed"
            elif result.status == "needs_review":
                self.report.needs_review += 1
                self.report.durations.append(result.duration_s)
            else:
                self.report.succeeded += 1
                self.report.durations.append(result.duration_s)
            await _save(
                session, article_id, self.version, self.llm.model,
                {"attempts": attempts, **result.values,
                 "attempt_history": [attempt_entry(attempts, result)]},
            )  # fmt: skip
        logger.info("%s · %s · %s", outcome, self.progress(), await self.llm.usage_line())

    def progress(self) -> str:
        r = self.report
        done = r.succeeded + r.needs_review + r.failed + r.skipped
        hours = (time.monotonic() - self.started) / 3600
        rate = (r.succeeded + r.needs_review) / hours if hours > 0 else 0.0
        left = self.queue.qsize()
        eta = f"ETA {left / rate:.1f}h" if rate > 0 else "ETA -"
        return f"[{done}/{r.queued}] {rate:.0f} articles/h, {left} queued, {eta}"


async def _previous(session: AsyncSession, article_id: int, version: str) -> ArticleAnalysis | None:
    return await session.scalar(
        select(ArticleAnalysis).where(
            ArticleAnalysis.article_id == article_id,
            ArticleAnalysis.analyzer_version == version,
        )
    )


def attempt_entry(attempt: int, result: AnalysisOutcome) -> dict[str, Any]:
    """One `attempt_history` entry: no raw answer, no article text."""
    if result.status == "failed":
        finish = "length" if is_cut_off(result.error) else "invalid"
    elif result.status == "skipped":
        finish = None  # never sent
    else:
        finish = "stop"
    return {
        "attempt": attempt,
        "status": result.status,
        "error": (result.error or None) and result.error.split(": ", 1)[0][:200],
        "finish_reason": finish,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "at": datetime.now(UTC).isoformat(),
    }


async def _save(
    session: AsyncSession, article_id: int, version: str, model: str, values: dict[str, Any]
) -> None:
    """Insert or update this article's analysis for `version`, and commit at once.

    `values["attempt_history"]` (this attempt's entry) is appended to the stored list;
    earlier entries are kept.
    """
    statement = insert(ArticleAnalysis).values(
        article_id=article_id, analyzer_version=version, model=model, **values
    )
    history = func.coalesce(ArticleAnalysis.attempt_history, cast(literal("[]"), JSONB))
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ArticleAnalysis.article_id, ArticleAnalysis.analyzer_version],
            set_={
                **values,
                "attempt_history": history.op("||")(statement.excluded.attempt_history),
                "updated_at": func.now(),
            },
        )
    )
    await session.commit()
