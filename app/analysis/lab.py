"""Prompt/model lab: run the analysis on a few articles and measure it. Not production.

Uses the same `analyse_article` flow as `news analyze`, so the lab measures what gets
stored, but it writes NO rows: outputs are files only. Outputs contain article quotes
(private text), so they go OUTSIDE the repository, to
`~/news-eval/lab/<UTC stamp>-<model>/`: one JSON per article plus `summary.md`.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import pipeline
from app.analysis.cleanup import upcoming
from app.analysis.evidence import EvidenceReport, EvidenceStatus
from app.analysis.llm import GeminiClient
from app.analysis.pipeline import AnalysisOutcome, analyzer_version
from app.analysis.prompt import PROMPT_VERSION
from app.analysis.schema import SCHEMA_VERSION

logger = logging.getLogger(__name__)

LAB_ROOT = Path.home() / "news-eval" / "lab"


@dataclass
class LabResult:
    article_id: int
    title: str
    ok: bool
    duration_s: float = 0.0
    tokens_out: int | None = None
    tokens_per_s: float | None = None
    error: str | None = None
    evidence: EvidenceReport | None = None
    counts: dict[str, int] = field(default_factory=dict)
    issues: int = 0


async def run_lab(
    session: AsyncSession,
    llm: GeminiClient,
    article_ids: list[int],
    *,
    label: str = "",
) -> tuple[Path, list[LabResult]]:
    """Analyse each article with the same flow as `news analyze`, storing no rows.

    Writes only files under LAB_ROOT. Each article is sent once (no compact retry).
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = LAB_ROOT / f"{stamp}-{llm.model.replace('/', '_')}{('-' + label) if label else ''}"
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    version = analyzer_version(llm.model)
    results: list[LabResult] = []
    stopped: str | None = None  # set when a limit or a hard error ends the run

    for article_id in article_ids:
        if stopped is not None:
            results.append(LabResult(article_id, "?", ok=False, error=f"not sent: {stopped}"))
            continue
        logger.info(
            "[%d/%d] Sending #%d to %s",
            len(results) + 1, len(article_ids), article_id, llm.model,
        )  # fmt: skip
        outcome = await pipeline.analyse_article(session, llm, article_id, version)
        if outcome.status == "stopped":
            stopped = outcome.error
        result = _result(article_id, outcome)
        record = _record(article_id, llm.model, outcome)
        logger.info("%s · %s", outcome.outcome, await llm.usage_line())
        _write_private(
            out / f"article-{article_id}.json",
            json.dumps(record, indent=2, ensure_ascii=False, default=str),
        )
        results.append(result)

    _write_private(out / "summary.md", render_summary(llm.model, results))
    return out, results


def _result(article_id: int, outcome: AnalysisOutcome) -> LabResult:
    ok = outcome.status in ("succeeded", "needs_review")
    result = LabResult(article_id, outcome.title, ok=ok, error=None if ok else outcome.error)
    if ok:
        analysis = outcome.analysis
        assert analysis is not None  # noqa: S101  # an answered article always has one
        result.duration_s = outcome.duration_s
        result.tokens_out = outcome.output_tokens
        result.tokens_per_s = outcome.tokens_per_s
        result.evidence = outcome.evidence
        result.issues = len(outcome.issues)
        result.counts = {
            "who": len(analysis.who),
            "where": len(analysis.where),
            "claims": len(analysis.claims),
            "timeline": len(analysis.when.timeline),
            "open_questions": len(analysis.open_questions),
        }
    return result


def _record(article_id: int, model: str, outcome: AnalysisOutcome) -> dict[str, Any]:
    record: dict[str, Any] = {
        "article_id": article_id,
        "title": outcome.title,
        "url": outcome.url,
        "model": model,
        "status": outcome.status,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    if outcome.error is not None:
        record["error"] = outcome.error
    if outcome.raw_output is not None and outcome.analysis is None:
        record["raw"] = outcome.raw_output  # whole answer: it can be re-parsed offline
    if outcome.analysis is not None and outcome.cleaned is not None and outcome.cleanup:
        record |= {
            "duration_s": round(outcome.duration_s, 1),
            "tokens": {"in": outcome.input_tokens, "out": outcome.output_tokens},
            "evidence": {k: v.value for k, v in outcome.evidence.statuses.items()}
            if outcome.evidence
            else {},
            "unverified_paths": outcome.unverified_paths,
            "issues": {k: v.value for k, v in outcome.issues.items()},
            # The model's raw answer is kept unchanged; the cleaned version and every
            # correction (original, corrected, reason, method) sit beside it.
            "analysis": outcome.analysis.model_dump(),
            "cleaned_analysis": outcome.cleaned.model_dump(),
            "corrections": [asdict(c) for c in outcome.cleanup.items],
            "review_flags": [asdict(f) for f in outcome.cleanup.flags],
            "upcoming": upcoming(outcome.cleaned, outcome.published) if outcome.published else [],
        }
    return record


def _write_private(path: Path, text: str) -> None:
    """Lab files contain article quotes: readable by the owner only."""
    path.write_text(text)
    path.chmod(0o600)


def render_summary(model: str, results: list[LabResult]) -> str:
    ok = [r for r in results if r.ok]
    lines = [
        f"# Lab run: {model}",
        f"prompt v{PROMPT_VERSION}, schema v{SCHEMA_VERSION}",
        "",
        f"Valid output: {len(ok)}/{len(results)}",
    ]
    if ok:
        total = sum(r.evidence.total for r in ok if r.evidence)
        verified = sum(r.evidence.count(EvidenceStatus.VERIFIED) for r in ok if r.evidence)
        fabricated = sum(r.evidence.count(EvidenceStatus.FABRICATED) for r in ok if r.evidence)
        avg_s = sum(r.duration_s for r in ok) / len(ok)
        speeds = [r.tokens_per_s for r in ok if r.tokens_per_s]
        lines += [
            f"Evidence verified: {verified}/{total} ({verified / total:.0%})" if total else "",
            f"Evidence fabricated: {fabricated}",
            f"Semantic issues (names not in article, undated event dates): "
            f"{sum(r.issues for r in ok)}",
            f"Avg time per article: {avg_s:.1f}s"
            + (f" · avg {sum(speeds) / len(speeds):.1f} tokens/s" if speeds else ""),
        ]
    columns = ["id", "ok", "s", "tok out", "evidence", "fabricated", "issues", "who/where/claims",
               "title"]  # fmt: skip
    lines += ["", "| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    for r in results:
        ev = r.evidence
        lines.append(
            f"| {r.article_id} | {'✓' if r.ok else '✗ ' + (r.error or '')[:40]} "
            f"| {r.duration_s:.0f} | {r.tokens_out or '-'} "
            f"| {f'{ev.verified_rate:.0%}' if ev else '-'} "
            f"| {ev.count(EvidenceStatus.FABRICATED) if ev else '-'} | {r.issues} "
            f"| {'/'.join(str(r.counts.get(k, '-')) for k in ('who', 'where', 'claims'))} "
            f"| {r.title[:60]} |"
        )
    return "\n".join(lines) + "\n"
