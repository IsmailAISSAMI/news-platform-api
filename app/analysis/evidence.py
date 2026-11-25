"""Code-side verification of the model's evidence. The model's word is never enough.

A quote is verified when, after normalizing whitespace, quotation marks, dashes and case,
it appears inside the cited paragraph as whole words (a quote cannot start or end in the
middle of a word). Quotes that only appear in another paragraph are counted as
`misplaced` (still real text, wrong pointer). Quotes found nowhere are `fabricated`.
A quote shorter than MIN_QUOTE_TOKENS tokens and MIN_QUOTE_CHARS characters is
`too_short`: two words are in almost every article and prove nothing. Event-date quotes
are exempt ("on Monday" is a complete date quote).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.analysis.schema import ArticleAnalysis, Claim, Evidence

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
        "\u2010": "-",  # hyphen (NFKC maps the non-breaking hyphen to it)
        "\u2012": "-",  # figure dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        # Invisible characters CMS editors leave in text: removed, or a real quote
        # would not match the paragraph it was copied from.
        "\u00ad": None,  # soft hyphen
        "\u200b": None,  # zero-width space
        "\u200c": None,  # zero-width non-joiner
        "\u200d": None,  # zero-width joiner
        "\u2060": None,  # word joiner
        "\ufeff": None,  # zero-width no-break space (BOM)
    }
)
_SPACE = re.compile(r"\s+")
_EDGE_PUNCT = " .,;:'\"\u2026"  # incl. horizontal ellipsis


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    MISPLACED = "misplaced"  # the quote exists, but in another paragraph
    FABRICATED = "fabricated"  # the quote exists nowhere in the article
    BAD_PARAGRAPH = "bad_paragraph"  # cites a paragraph number that does not exist
    TOO_SHORT = "too_short"  # too short to prove anything


MIN_QUOTE_TOKENS = 4
MIN_QUOTE_CHARS = 20
EVENT_DATE_EVIDENCE = "$.when.event_date.evidence"


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
    def verified_rate(self) -> float | None:
        """Share of verified quotes; None when there is no quote at all (nothing to rate)."""
        return self.count(EvidenceStatus.VERIFIED) / self.total if self.total else None


def _contains(paragraph: str, quote: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(quote)}(?!\w)", paragraph) is not None


def check(
    evidence: Evidence, paragraphs: list[str], *, allow_short: bool = False
) -> EvidenceStatus:
    quote = normalize(evidence.quote).strip(_EDGE_PUNCT)
    if not quote:
        return EvidenceStatus.FABRICATED
    short = len(quote.split()) < MIN_QUOTE_TOKENS and len(quote) < MIN_QUOTE_CHARS
    if short and not allow_short:
        return EvidenceStatus.TOO_SHORT
    normalized = [normalize(p) for p in paragraphs]
    if not 1 <= evidence.paragraph <= len(paragraphs):
        found = any(_contains(p, quote) for p in normalized)
        return EvidenceStatus.MISPLACED if found else EvidenceStatus.BAD_PARAGRAPH
    if _contains(normalized[evidence.paragraph - 1], quote):
        return EvidenceStatus.VERIFIED
    if any(_contains(p, quote) for p in normalized):
        return EvidenceStatus.MISPLACED
    return EvidenceStatus.FABRICATED


def verify(analysis: ArticleAnalysis, paragraphs: list[str]) -> EvidenceReport:
    """Check every Evidence object anywhere in the analysis (paths index into `analysis`)."""
    report = EvidenceReport()
    for path, evidence in _walk(analysis, "$"):
        report.statuses[path] = check(evidence, paragraphs, allow_short=path == EVENT_DATE_EVIDENCE)
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

# Date words that can anchor an event date. A bare number is not a date ("10 people"):
# digits count only as a 4-digit year, and a day number only counts through the month
# name next to it (which matches on its own). Generic "year"/"month"/"week" only count with a
# relative qualifier ("last week"), so "four-year-old" or "decades" do not pass.
_DATE_WORDS = re.compile(
    r"\b(\d{4}|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|"
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
    r"\b(will|expected|scheduled|planned|plans? to|set to|(is|are) due to|is to|are to|"
    r"aims? to|intends? to|preparing|upcoming|proposed|pending)\b",
    re.IGNORECASE,
)


# Generic words a model may append to a name the article writes in a shared list
# ("Taourirt, Khenifra and Ifrane provinces" -> "Taourirt province").
_GENERIC_PLACE_WORDS = re.compile(
    r"\s+(province|provinces|prefecture|region|commune|city|town|village|governorate)$",
    re.IGNORECASE,
)


_NOT_A_NAME = re.compile(r"^(the|a|an|this|that|its|their)?$", re.IGNORECASE)


def core_place_name(name: str) -> str:
    """'Taourirt province' -> 'Taourirt'. Names without a generic suffix are unchanged.

    A description is not a name: "the region" stays "the region" (stripping would leave
    "the", which is in every article and would pass as a place).
    """
    name = name.strip()
    core = _GENERIC_PLACE_WORDS.sub("", name)
    return name if _NOT_A_NAME.match(core.strip()) else core


def mentions(article: str, name: str) -> bool:
    """Whether the normalized `article` contains the name as whole words.

    Plain substring tests let "Sale" pass inside "wholesale" and an empty name pass
    everywhere. A trailing "s" is allowed: normalize() drops apostrophes, so the
    article's "Morocco's" becomes "moroccos".
    """
    needle = normalize(name)
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}s?(?!\w)", article) is not None


# Markers of an anonymous source in the article. A claim resting on such a paragraph
# should carry the attribution ("sources close to the ministry"), never read as a fact.
_ANONYMOUS_SOURCE = re.compile(
    r"\b(sources?\s+(said|say|says|told|tell|reported|indicated|revealed|confirmed)|"
    r"according\s+to\s+(\w+\s+){0,3}sources?|a\s+source\s+(said|told)|"
    r"sources\s+close\s+to)\b",
    re.IGNORECASE,
)
MIN_CAUSE_LENGTH = 25


class Issue(StrEnum):
    NAME_NOT_IN_ARTICLE = "name_not_in_article"  # renamed / expanded / invented name
    EVENT_DATE_WITHOUT_DATE_WORDS = "event_date_without_date_words"
    TIMELINE_PUBLICATION_ENTRY = "timeline_publication_entry"  # publication is not an event
    PLACE_NAME_WITH_ADDED_SUFFIX = "place_name_with_added_suffix"  # core name is in article
    # The quote speaks of the future ("will be signed") but the claim reads as done.
    POSSIBLE_TENSE_MISMATCH = "possible_tense_mismatch"
    # The evidence paragraph cites anonymous sources but the claim has no attribution.
    ANONYMOUS_SOURCE_UNATTRIBUTED = "anonymous_source_unattributed"
    # Dated with the article's publication date while the quote holds no date words.
    TIMELINE_PUBLICATION_DATE = "timeline_publication_date"
    UNINFORMATIVE_CAUSE = "uninformative_cause"  # too short, or restates the main event
    # attributed_to names someone the article never names (anonymous sources exempt).
    ATTRIBUTION_NOT_IN_ARTICLE = "attribution_not_in_article"


def semantic_issues(
    analysis: ArticleAnalysis, paragraphs: list[str], published: date | None = None
) -> dict[str, Issue]:
    """Problems the JSON schema cannot catch, found by checking against the article.

    `published` (the article's publication date) enables the publication-date timeline
    check; without it that check is skipped. One function per section, merged in order.
    """
    article = normalize(" ".join(paragraphs))
    return {
        **_actor_issues(analysis, article),
        **_place_issues(analysis, article),
        **_event_date_issues(analysis),
        **_claim_issues(analysis, article, paragraphs),
        **_timeline_issues(analysis, published),
        **_cause_issues(analysis, article),
    }


def _actor_issues(analysis: ArticleAnalysis, article: str) -> dict[str, Issue]:
    issues: dict[str, Issue] = {}
    for index, actor in enumerate(analysis.who):
        for field_name in ("name_as_written", "full_name"):
            if not mentions(article, getattr(actor, field_name)):
                issues[f"$.who[{index}].{field_name}"] = Issue.NAME_NOT_IN_ARTICLE
    return issues


def _place_issues(analysis: ArticleAnalysis, article: str) -> dict[str, Issue]:
    issues: dict[str, Issue] = {}
    for index, place in enumerate(analysis.where):
        written = place.name_as_written
        if mentions(article, written):
            continue
        suffixed = mentions(article, core_place_name(written))
        issues[f"$.where[{index}].name_as_written"] = (
            Issue.PLACE_NAME_WITH_ADDED_SUFFIX if suffixed else Issue.NAME_NOT_IN_ARTICLE
        )
    return issues


def _event_date_issues(analysis: ArticleAnalysis) -> dict[str, Issue]:
    event_date = analysis.when.event_date
    quote = event_date.evidence.quote if event_date.evidence else ""
    if event_date.value is not None and not _DATE_WORDS.search(quote):
        return {"$.when.event_date": Issue.EVENT_DATE_WITHOUT_DATE_WORDS}
    return {}


def _claim_issues(
    analysis: ArticleAnalysis, article: str, paragraphs: list[str]
) -> dict[str, Issue]:
    issues: dict[str, Issue] = {}
    for index, claim in enumerate(analysis.claims):
        if _FUTURE.search(claim.evidence.quote) and not _FUTURE.search(claim.text):
            issues[f"$.claims[{index}]"] = Issue.POSSIBLE_TENSE_MISMATCH
        if _anonymous_unattributed(claim, paragraphs):
            issues[f"$.claims[{index}].attributed_to"] = Issue.ANONYMOUS_SOURCE_UNATTRIBUTED
        elif _unknown_attribution(article, claim.attributed_to):
            issues[f"$.claims[{index}].attributed_to"] = Issue.ATTRIBUTION_NOT_IN_ARTICLE
    return issues


def _timeline_issues(analysis: ArticleAnalysis, published: date | None) -> dict[str, Issue]:
    issues: dict[str, Issue] = {}
    for index, entry in enumerate(analysis.when.timeline):
        if _PUBLICATION.search(entry.event):
            issues[f"$.when.timeline[{index}]"] = Issue.TIMELINE_PUBLICATION_ENTRY
        elif (
            published is not None
            and entry.date == published.isoformat()
            and not _DATE_WORDS.search(entry.evidence.quote)
        ):
            issues[f"$.when.timeline[{index}]"] = Issue.TIMELINE_PUBLICATION_DATE
    return issues


def _cause_issues(analysis: ArticleAnalysis, article: str) -> dict[str, Issue]:
    issues: dict[str, Issue] = {}
    main_event = normalize(analysis.what.main_event)
    for index, cause in enumerate(analysis.why):
        text = normalize(cause.text)
        restates = bool(main_event) and (text in main_event or main_event in text)
        if len(text) < MIN_CAUSE_LENGTH or restates:
            issues[f"$.why[{index}]"] = Issue.UNINFORMATIVE_CAUSE
        if _unknown_attribution(article, cause.attributed_to):
            issues[f"$.why[{index}].attributed_to"] = Issue.ATTRIBUTION_NOT_IN_ARTICLE
    return issues


# An attribution that describes an unnamed source ("Hespress sources", "an unnamed
# official") need not appear verbatim: the prompt asks for the article's description.
_ANONYMOUS_PHRASE = re.compile(
    r"\b(sources?|unnamed|anonymous|unidentified|officials?|insiders?|observers?|"
    r"witness(es)?|residents?)\b",
    re.IGNORECASE,
)


def _unknown_attribution(article: str, attributed_to: str | None) -> bool:
    if attributed_to is None or not attributed_to.strip():
        return False
    if _ANONYMOUS_PHRASE.search(attributed_to):
        return False
    return not mentions(article, attributed_to)


def _anonymous_unattributed(claim: Claim, paragraphs: list[str]) -> bool:
    """Only the claim's OWN evidence paragraph counts, and any attribution clears it."""
    if claim.attributed_to is not None and claim.attributed_to.strip():
        return False
    if not 1 <= claim.evidence.paragraph <= len(paragraphs):
        return False
    paragraph = unicodedata.normalize("NFKC", paragraphs[claim.evidence.paragraph - 1])
    return _ANONYMOUS_SOURCE.search(paragraph.translate(_QUOTES)) is not None
