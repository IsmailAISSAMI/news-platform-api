"""Code-side verification of the model's evidence. The model's word is never enough.

A quote is verified when, after normalizing whitespace, quotation marks, dashes and case,
it appears inside the cited paragraph. Quotes that only appear in another paragraph are
counted as `misplaced` (still real text, wrong pointer). Quotes found nowhere are
`fabricated`.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.analysis.schema import ArticleAnalysis, Evidence

# Typographic characters folded to ASCII before comparing (written as escapes on purpose).
_QUOTES = str.maketrans(
    {
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u00a0": " ",  # no-break space
    }
)
_SPACE = re.compile(r"\s+")
_EDGE_PUNCT = " .,;:'\"\u2026"  # incl. horizontal ellipsis


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    MISPLACED = "misplaced"  # the quote exists, but in another paragraph
    FABRICATED = "fabricated"  # the quote exists nowhere in the article
    BAD_PARAGRAPH = "bad_paragraph"  # cites a paragraph number that does not exist


# Quotation marks are dropped entirely: models re-quote nested quotes inside JSON (a
# curly double quote in the article often comes back as a single quote). Dropping them
# cannot make an invented sentence pass: every word must still match.
_DROP_QUOTES = str.maketrans("", "", "\"'`")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).translate(_DROP_QUOTES)
    text = text.replace("...", "\u2026")
    return _SPACE.sub(" ", text).strip().casefold()


@dataclass
class EvidenceReport:
    statuses: dict[str, EvidenceStatus] = field(default_factory=dict)  # JSON path -> status

    def count(self, status: EvidenceStatus) -> int:
        return sum(1 for s in self.statuses.values() if s is status)

    @property
    def total(self) -> int:
        return len(self.statuses)

    @property
    def verified_rate(self) -> float:
        return self.count(EvidenceStatus.VERIFIED) / self.total if self.total else 1.0


def check(evidence: Evidence, paragraphs: list[str]) -> EvidenceStatus:
    quote = normalize(evidence.quote).strip(_EDGE_PUNCT)
    if not quote:
        return EvidenceStatus.FABRICATED
    normalized = [normalize(p) for p in paragraphs]
    if not 1 <= evidence.paragraph <= len(paragraphs):
        found = any(quote in p for p in normalized)
        return EvidenceStatus.MISPLACED if found else EvidenceStatus.BAD_PARAGRAPH
    if quote in normalized[evidence.paragraph - 1]:
        return EvidenceStatus.VERIFIED
    if any(quote in p for p in normalized):
        return EvidenceStatus.MISPLACED
    return EvidenceStatus.FABRICATED


def verify(analysis: ArticleAnalysis, paragraphs: list[str]) -> EvidenceReport:
    """Check every Evidence object anywhere in the analysis."""
    report = EvidenceReport()
    for path, evidence in _walk(analysis, "$"):
        report.statuses[path] = check(evidence, paragraphs)
    return report


def _walk(node: Any, path: str) -> list[tuple[str, Evidence]]:
    found: list[tuple[str, Evidence]] = []
    if isinstance(node, Evidence):
        found.append((path, node))
    elif isinstance(node, BaseModel):
        for name in type(node).model_fields:
            found += _walk(getattr(node, name), f"{path}.{name}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _walk(item, f"{path}[{index}]")
    return found


# --- Semantic checks beyond quotes ---------------------------------------------------------

# Date words that can anchor an event date. Generic "year"/"month"/"week" only count with a
# relative qualifier ("last week"), so "four-year-old" or "decades" do not pass.
_DATE_WORDS = re.compile(
    r"\b(\d{4}|\d{1,2}(st|nd|rd|th)?|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|"
    r"july?|aug(ust)?|sep(t|tember)?|oct(ober)?|nov(ember)?|dec(ember)?|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|yesterday|today|tonight|"
    r"(this|last|next|past)\s+(week|month|year|weekend))\b(?!-)",
    re.IGNORECASE,
)
# Only the ARTICLE's own publication: a body publishing a report or opinion is an event.
_PUBLICATION = re.compile(
    r"\b(publication of (the|this) (article|story|report by hespress)|"
    r"(the|this) (article|story) (was |is )?published|article publication)\b",
    re.IGNORECASE,
)
# Strong markers of something not (yet) done. Weak modals (could, would, may, might)
# express possibility and caused false alarms on real articles, so they are excluded.
# Heuristic: used to FLAG for review, never to rewrite.
_FUTURE = re.compile(
    r"\b(will|expected|scheduled|planned|plans? to|set to|due to|is to|are to|"
    r"aims? to|intends? to|preparing|upcoming|proposed|pending)\b",
    re.IGNORECASE,
)


# Generic words a model may append to a name the article writes in a shared list
# ("Taourirt, Khenifra and Ifrane provinces" -> "Taourirt province").
_GENERIC_PLACE_WORDS = re.compile(
    r"\s+(province|provinces|prefecture|region|commune|city|town|village|governorate)$",
    re.IGNORECASE,
)


def core_place_name(name: str) -> str:
    """'Taourirt province' -> 'Taourirt'. Names without a generic suffix are unchanged."""
    return _GENERIC_PLACE_WORDS.sub("", name.strip())


class Issue(StrEnum):
    NAME_NOT_IN_ARTICLE = "name_not_in_article"  # renamed / expanded / invented name
    EVENT_DATE_WITHOUT_DATE_WORDS = "event_date_without_date_words"
    TIMELINE_PUBLICATION_ENTRY = "timeline_publication_entry"  # publication is not an event
    PLACE_NAME_WITH_ADDED_SUFFIX = "place_name_with_added_suffix"  # core name is in article
    # The quote speaks of the future ("will be signed") but the claim reads as done.
    POSSIBLE_TENSE_MISMATCH = "possible_tense_mismatch"


def semantic_issues(analysis: ArticleAnalysis, paragraphs: list[str]) -> dict[str, Issue]:
    """Problems the JSON schema cannot catch, found by checking against the article."""
    article = normalize(" ".join(paragraphs))
    issues: dict[str, Issue] = {}
    for index, actor in enumerate(analysis.who):
        for field_name in ("name_as_written", "full_name"):
            if normalize(getattr(actor, field_name)) not in article:
                issues[f"$.who[{index}].{field_name}"] = Issue.NAME_NOT_IN_ARTICLE
    for index, place in enumerate(analysis.where):
        name = normalize(place.name_as_written)
        if name not in article and normalize(core_place_name(place.name_as_written)) in article:
            issues[f"$.where[{index}].name_as_written"] = Issue.PLACE_NAME_WITH_ADDED_SUFFIX
        elif name not in article:
            issues[f"$.where[{index}].name_as_written"] = Issue.NAME_NOT_IN_ARTICLE
    event_date = analysis.when.event_date
    quote = event_date.evidence.quote if event_date.evidence else ""
    if event_date.value is not None and not _DATE_WORDS.search(quote):
        issues["$.when.event_date"] = Issue.EVENT_DATE_WITHOUT_DATE_WORDS
    for index, claim in enumerate(analysis.claims):
        if _FUTURE.search(claim.evidence.quote) and not _FUTURE.search(claim.text):
            issues[f"$.claims[{index}]"] = Issue.POSSIBLE_TENSE_MISMATCH
    for index, entry in enumerate(analysis.when.timeline):
        if _PUBLICATION.search(entry.event):
            issues[f"$.when.timeline[{index}]"] = Issue.TIMELINE_PUBLICATION_ENTRY
    return issues
