"""Prompt/model lab: run the analysis on a few articles and measure it. Not production.

Outputs contain article quotes (private text), so they go OUTSIDE the repository, to
`~/news-eval/lab/<UTC stamp>-<model>/`: one JSON per article plus `summary.md`.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.cleanup import clean, upcoming
from app.analysis.evidence import EvidenceReport, EvidenceStatus, semantic_issues, verify
from app.analysis.llm import LLMOutputError, LocalLLM
from app.analysis.prompt import PROMPT_VERSION, SYSTEM_PROMPT, prepare
from app.analysis.schema import SCHEMA_VERSION, ArticleAnalysis
from app.models import Article, ArticleBody, Source

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
    llm: LocalLLM,
    article_ids: list[int],
    *,
    extra_body: dict[str, Any] | None = None,
    label: str = "",
) -> tuple[Path, list[LabResult]]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = LAB_ROOT / f"{stamp}-{llm.model.replace('/', '_')}{('-' + label) if label else ''}"
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    results: list[LabResult] = []

    for article_id in article_ids:
        row = (
            await session.execute(
                select(Article, ArticleBody, Source)
                .join(ArticleBody, ArticleBody.article_id == Article.id)
                .join(Source, Source.id == Article.source_id)
                .where(Article.id == article_id)
            )
        ).first()
        if row is None:
            results.append(LabResult(article_id, "?", ok=False, error="no article with a body"))
            continue
        article, body, source = row
        prepared = prepare(article.title, article.published_at.date(), source.name, body.body_text)
        logger.info("Analysing #%d (%d paragraphs)", article_id, len(prepared.paragraphs))

        result = LabResult(article_id, article.title, ok=False)
        record: dict[str, Any] = {
            "article_id": article_id,
            "title": article.title,
            "url": article.canonical_url,
            "model": llm.model,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "settings": extra_body or {},
        }
        try:
            completion = await llm.structured(
                ArticleAnalysis, system=SYSTEM_PROMPT, user=prepared.render(),
                extra_body=extra_body,
            )  # fmt: skip
        except LLMOutputError as exc:
            result.error = str(exc)
            record["error"] = str(exc)
            record["raw"] = exc.raw[:20000]
        else:
            analysis = completion.parsed
            report = verify(analysis, prepared.paragraphs)
            issues = semantic_issues(analysis, prepared.paragraphs)
            cleaned, cleanup = clean(
                analysis,
                prepared.paragraphs,
                publisher_names=(source.name, source.name.split()[0]),
            )
            result.issues = len(issues)
            result.ok = True
            result.duration_s = completion.duration_s
            result.tokens_out = completion.completion_tokens
            result.tokens_per_s = completion.tokens_per_second
            result.evidence = report
            result.counts = {
                "who": len(analysis.who),
                "where": len(analysis.where),
                "claims": len(analysis.claims),
                "timeline": len(analysis.when.timeline),
                "open_questions": len(analysis.open_questions),
            }
            record |= {
                "duration_s": round(completion.duration_s, 1),
                "tokens": {"in": completion.prompt_tokens, "out": completion.completion_tokens},
                "reasoning_chars": len(completion.reasoning or ""),
                "evidence": {k: v.value for k, v in report.statuses.items()},
                "issues": {k: v.value for k, v in issues.items()},
                # The model's raw answer is kept unchanged; the cleaned version and every
                # correction (original, corrected, reason, method) sit beside it.
                "analysis": analysis.model_dump(),
                "cleaned_analysis": cleaned.model_dump(),
                "corrections": [asdict(c) for c in cleanup.items],
                "review_flags": [asdict(f) for f in cleanup.flags],
                "upcoming": upcoming(cleaned, article.published_at.date()),
            }
        path = out / f"article-{article_id}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str))
        path.chmod(0o600)
        results.append(result)

    summary = out / "summary.md"
    summary.write_text(render_summary(llm.model, extra_body, results))
    summary.chmod(0o600)
    return out, results


def render_summary(model: str, settings: dict[str, Any] | None, results: list[LabResult]) -> str:
    ok = [r for r in results if r.ok]
    lines = [
        f"# Lab run: {model}",
        f"prompt v{PROMPT_VERSION}, schema v{SCHEMA_VERSION}, settings {settings or {}}",
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
