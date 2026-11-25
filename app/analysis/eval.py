"""Gold-set evaluation: a person labels a sample, code turns the labels into numbers.

`export_gold` writes a stratified sample of analysed articles as Markdown checklists, plus
`labels.csv` (one row per item) for the verdicts. The files contain article text, so they
go under `~/news-eval/gold/`, outside the repo (directory mode 700, files 600), and never
anywhere inside it.

`score_gold` reads the labelled CSV: precision per section and overall, and the worst
items. `consistency` re-runs the current code checks over every stored analysis (verified
quote rate, review flags and corrections per article): no labels needed.
"""

import asyncio
import csv
import io
import logging
import os
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.cleanup import clean
from app.analysis.evidence import EvidenceStatus, semantic_issues, verify
from app.analysis.prompt import prepare
from app.analysis.schema import ArticleAnalysis as AnalysisSchema
from app.models import Article, ArticleAnalysis, ArticleBody, Source

logger = logging.getLogger(__name__)

GOLD_ROOT = Path.home() / "news-eval" / "gold"
REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_SEED = 20261125  # fixed: the same database gives the same sample
SECTIONS = ("who", "where", "when", "claims", "why", "impact")
VERDICTS = ("correct", "wrong", "missing_detail")
CHECKBOXES = "[ ] correct  [ ] wrong  [ ] missing detail"
LABEL_COLUMNS = ("article_id", "path", "section", "item", "verdict", "note")
NO_CATEGORY = "(none)"


@dataclass(frozen=True)
class GoldItem:
    section: str
    path: str
    text: str


def gold_items(cleaned: dict[str, Any]) -> list[GoldItem]:
    """The labellable items of a cleaned analysis (stored JSON), in section order."""
    items: list[GoldItem] = []
    for i, actor in enumerate(cleaned.get("who", [])):
        text = f"{actor['full_name']} ({actor['type']}, {actor['involvement']}): {actor['role']}"
        items.append(GoldItem("who", f"$.who[{i}]", text))
    for i, place in enumerate(cleaned.get("where", [])):
        items.append(
            GoldItem("where", f"$.where[{i}]", f"{place['name_as_written']} ({place['role']})")
        )
    when = cleaned.get("when", {})
    event_date = (when.get("event_date") or {}).get("value")
    if event_date:
        items.append(GoldItem("when", "$.when.event_date", f"event date: {event_date}"))
    for i, entry in enumerate(when.get("timeline", [])):
        items.append(
            GoldItem("when", f"$.when.timeline[{i}]", f"{entry['date']}: {entry['event']}")
        )
    for i, claim in enumerate(cleaned.get("claims", [])):
        source = f" (attributed to {claim['attributed_to']})" if claim.get("attributed_to") else ""
        items.append(
            GoldItem("claims", f"$.claims[{i}]", f"[{claim['kind']}] {claim['text']}{source}")
        )
    for i, cause in enumerate(cleaned.get("why", [])):
        items.append(GoldItem("why", f"$.why[{i}]", f"[{cause['kind']}] {cause['text']}"))
    for i, impact in enumerate(cleaned.get("impact", [])):
        text = f"[{impact['area']}, {impact['certainty']}] {impact['text']}"
        items.append(GoldItem("impact", f"$.impact[{i}]", text))
    return items


def stratified_sample(categories: dict[int, str], n: int, seed: int = GOLD_SEED) -> list[int]:
    """Round-robin over categories (sorted), each shuffled with a fixed seed."""
    rng = random.Random(seed)  # noqa: S311  # sampling, not security
    groups: dict[str, list[int]] = defaultdict(list)
    for article_id in sorted(categories):
        groups[categories[article_id]].append(article_id)
    queues = []
    for name in sorted(groups):
        ids = groups[name]
        rng.shuffle(ids)
        queues.append(ids)
    picked: list[int] = []
    while len(picked) < n and any(queues):
        for queue in queues:
            if queue and len(picked) < n:
                picked.append(queue.pop(0))
    return picked


def _check_outside_repo(out_dir: Path) -> None:
    resolved = out_dir.expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        msg = f"gold files contain article text and must stay outside the repo: {out_dir}"
        raise ValueError(msg)


async def _latest_analyses(session: AsyncSession) -> list[tuple[Article, ArticleAnalysis, str]]:
    """The newest answered analysis per article, with the article and its body."""
    rows = await session.execute(
        select(Article, ArticleAnalysis, ArticleBody.body_text)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .join(ArticleBody, ArticleBody.article_id == Article.id)
        .where(ArticleAnalysis.analysis.is_not(None), ArticleAnalysis.cleaned.is_not(None))
        .order_by(Article.id, ArticleAnalysis.updated_at.desc(), ArticleAnalysis.id.desc())
    )
    latest: dict[int, tuple[Article, ArticleAnalysis, str]] = {}
    for article, analysis, body in rows.all():
        latest.setdefault(article.id, (article, analysis, body))
    return list(latest.values())


async def export_gold(
    session: AsyncSession, n: int = 30, out_dir: Path = GOLD_ROOT, *, seed: int = GOLD_SEED
) -> Path:
    """Write the labelling files for a sample of `n` articles; return their directory."""
    rows = {
        article.id: (article, analysis, body)
        for article, analysis, body in await _latest_analyses(session)
    }
    categories = {
        i: (a.categories[0] if a.categories else NO_CATEGORY) for i, (a, _, _) in rows.items()
    }
    picked = stratified_sample(categories, n, seed)
    files: dict[str, str] = {}
    labels: list[dict[str, str]] = []
    for article_id in picked:
        article, analysis, body = rows[article_id]
        assert analysis.cleaned is not None  # filtered in the query  # noqa: S101
        items = gold_items(analysis.cleaned)
        files[f"article-{article_id}.md"] = _render(article, analysis, body, items)
        labels += [
            {"article_id": str(article_id), "path": it.path, "section": it.section,
             "item": it.text, "verdict": "", "note": ""}
            for it in items
        ]  # fmt: skip
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=LABEL_COLUMNS)
    writer.writeheader()
    writer.writerows(labels)
    files["labels.csv"] = buffer.getvalue()
    target = await asyncio.to_thread(_write_gold, out_dir, files)
    logger.info("gold set exported", extra={"articles": len(picked), "items": len(labels)})
    return target


def _write_gold(out_dir: Path, files: dict[str, str]) -> Path:
    """Refuse the repo, then write every file 600 in a fresh 700 directory."""
    _check_outside_repo(out_dir)
    root = out_dir.expanduser()
    target = root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    target.chmod(0o700)  # mkdir's mode is masked by the umask
    for name, content in files.items():
        fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    return target


def _render(article: Article, analysis: ArticleAnalysis, body: str, items: list[GoldItem]) -> str:
    published = article.published_at.date()
    paragraphs = prepare(article.title, published, "", body).paragraphs
    lines = [
        f"# Article {article.id}: {article.title}",
        "",
        f"- URL: {article.canonical_url}",
        f"- Published: {published.isoformat()}",
        f"- Category: {article.categories[0] if article.categories else NO_CATEGORY}",
        f"- Analyzer: {analysis.analyzer_version} ({analysis.status})",
        "",
        "Mark each item, then copy the verdict (correct, wrong, missing_detail) and any note",
        "into labels.csv.",
        "",
        "## Paragraphs",
        "",
    ]
    lines += [f"[P{n}] {p}\n" for n, p in enumerate(paragraphs, start=1)]
    by_section: dict[str, list[GoldItem]] = defaultdict(list)
    for item in items:
        by_section[item.section].append(item)
    for section in SECTIONS:
        lines += ["", f"## {section}", ""]
        if not by_section[section]:
            lines += ["(none extracted; note anything missing here)", "", "Note:"]
        for item in by_section[section]:
            lines += [f"- `{item.path}` {item.text}", f"  {CHECKBOXES}", "  Note:", ""]
    return "\n".join(lines) + "\n"


# --- Scoring ----------------------------------------------------------------------------


@dataclass
class SectionScore:
    labelled: int = 0
    correct: int = 0
    wrong: int = 0
    missing_detail: int = 0

    @property
    def precision(self) -> float | None:
        """Share of labelled items that are right (incomplete counts as right)."""
        if not self.labelled:
            return None
        return (self.correct + self.missing_detail) / self.labelled


@dataclass
class GoldReport:
    sections: dict[str, SectionScore]
    overall: SectionScore
    unlabelled: int
    worst: list[tuple[str, str, str]] = field(default_factory=list)  # article, path, note

    def render(self) -> str:
        def pct(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.0%}"

        lines = ["Gold-set precision (correct + missing detail) / labelled:"]
        for name, score in self.sections.items():
            lines.append(
                f"  {name:<7} {pct(score.precision):>5}  labelled {score.labelled:>3}  "
                f"wrong {score.wrong:>3}  missing detail {score.missing_detail:>3}"
            )
        lines.append(
            f"  overall {pct(self.overall.precision):>5}  labelled {self.overall.labelled}"
        )
        lines.append(f"  unlabelled rows: {self.unlabelled}")
        if self.worst:
            lines.append("Wrong items:")
            lines += [f"  #{a} {p}  {note}" for a, p, note in self.worst]
        return "\n".join(lines) + "\n"


def section_of(path: str) -> str:
    name = path.removeprefix("$.").split(".")[0].split("[")[0]
    return name if name in SECTIONS else "other"


def score_gold(labels_csv: Path, *, worst: int = 20) -> GoldReport:
    """Precision per section from a labelled CSV. Unknown verdicts are an error."""
    sections = {name: SectionScore() for name in SECTIONS}
    overall = SectionScore()
    unlabelled = 0
    wrong: list[tuple[str, str, str]] = []
    with labels_csv.open(newline="", encoding="utf-8") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            verdict = (row.get("verdict") or "").strip().lower().replace(" ", "_")
            if not verdict:
                unlabelled += 1
                continue
            if verdict not in VERDICTS:
                allowed = ", ".join(VERDICTS)
                msg = f"{labels_csv}:{line}: unknown verdict {row['verdict']!r} (use {allowed})"
                raise ValueError(msg)
            section = section_of(row["path"])
            for score in (sections.setdefault(section, SectionScore()), overall):
                score.labelled += 1
                setattr(score, verdict, getattr(score, verdict) + 1)
            if verdict == "wrong":
                wrong.append((row["article_id"], row["path"], (row.get("note") or "").strip()))
    return GoldReport(sections, overall, unlabelled, wrong[:worst])


@dataclass
class Consistency:
    analyses: int = 0
    invalid: int = 0  # stored JSON no longer valid against the current schema
    quotes: int = 0
    verified: int = 0
    flags: int = 0
    corrections: int = 0
    articles_by_issue: Counter[str] = field(default_factory=Counter)
    articles_by_flag: Counter[str] = field(default_factory=Counter)

    @property
    def verified_rate(self) -> float | None:
        return self.verified / self.quotes if self.quotes else None

    def render(self) -> str:
        per = self.analyses or 1
        rate = "n/a" if self.verified_rate is None else f"{self.verified_rate:.1%}"
        lines = [
            f"Consistency (current code checks over {self.analyses} stored analyses, "
            f"{self.invalid} not valid against the current schema):",
            f"  verified quotes {rate} ({self.verified}/{self.quotes})",
            f"  review flags per article {self.flags / per:.2f}",
            f"  corrections per article {self.corrections / per:.2f}",
            "  articles with each issue:",
        ]
        lines += [f"    {name}: {count}" for name, count in sorted(self.articles_by_issue.items())]
        lines.append("  articles with each review flag:")
        lines += [f"    {name}: {count}" for name, count in sorted(self.articles_by_flag.items())]
        return "\n".join(lines) + "\n"


async def consistency(session: AsyncSession) -> Consistency:
    """Re-run verify, semantic_issues and clean on every newest stored analysis."""
    result = Consistency()
    publishers = dict((await session.execute(select(Source.id, Source.name))).all())
    for article, stored, body in await _latest_analyses(session):
        try:
            analysis = AnalysisSchema.model_validate(stored.analysis)
        except ValidationError:
            result.invalid += 1
            continue
        result.analyses += 1
        published = article.published_at.date()
        paragraphs = prepare(article.title, published, "", body).paragraphs
        evidence = verify(analysis, paragraphs)
        result.quotes += evidence.total
        result.verified += evidence.count(EvidenceStatus.VERIFIED)
        name = publishers.get(article.source_id, "")
        names = (name, name.split()[0]) if name else ()
        _, report = clean(analysis, paragraphs, publisher_names=names, published=published)
        result.flags += len(report.flags)
        result.corrections += len(report.items)
        issues = semantic_issues(analysis, paragraphs, published)
        result.articles_by_issue.update({issue.value for issue in issues.values()})
        result.articles_by_flag.update({flag.reason.split(" (")[0] for flag in report.flags})
    return result
