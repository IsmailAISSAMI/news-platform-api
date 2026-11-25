"""Cleanup of a validated analysis, applied after the evidence checks.

Prompt rules reduce errors but never eliminate them, so code fixes what can be fixed
safely. Every correction is recorded as a `Correction`: where it applied, the original
value, the corrected value (None when removed), the reason, and whether the rule is
deterministic (exact) or heuristic (pattern-based, may be wrong). Callers keep the raw
analysis alongside the cleaned one, so nothing is lost.

- duplicate actors and places are merged (same name, case-insensitive)  [deterministic]
- a name that does not appear in the article is dropped                  [deterministic]
- an actor's `full_name` not in the article falls back to the name as written
                                                                          [deterministic]
- a place name with an added generic word ("Taourirt province" when the article lists
  "Taourirt, ... provinces") is kept as written ("Taourirt")             [heuristic]
- the publisher's own name is never an actor or a place                  [heuristic]
- "publication of the article" entries are dropped from the timeline     [heuristic]
- a timeline entry dated with the article's publication date whose quote holds no date
  words is dropped (the model filled the date in from the byline)         [heuristic]
- an event date whose evidence contains no date words becomes unknown    [heuristic]
- near-duplicate actors ("the investigating judge" and "investigating judge at the
  Tangier Court of Appeal") are merged, only when unambiguous: a short name contained
  in two different longer names ("Huang": "Jensen Huang", "Alice Huang") is kept
                                                                          [heuristic]

Some problems cannot be fixed safely and are FLAGGED for human review instead, never
rewritten: a claim whose quote speaks of the future while the claim reads as done;
a name listed both as an actor and as a place (either side may be the error); a claim
resting on a paragraph that cites anonymous sources ("sources said") but carrying no
attribution; a cause too short to explain anything or restating the main event.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.analysis.evidence import Issue, core_place_name, normalize, semantic_issues
from app.analysis.schema import Actor, ArticleAnalysis, EventDate, Place

Method = Literal["deterministic", "heuristic"]


@dataclass(frozen=True)
class Correction:
    path: str
    original: str
    corrected: str | None  # None: the item was removed
    reason: str
    method: Method

    def message(self) -> str:
        change = "removed" if self.corrected is None else f"changed to {self.corrected!r}"
        return f"{self.path}: {self.original!r} {change} ({self.reason}; {self.method})"


@dataclass(frozen=True)
class ReviewFlag:
    """Something code suspects but cannot fix safely: left unchanged, shown to a human."""

    path: str
    text: str
    reason: str


@dataclass
class CleanupReport:
    items: list[Correction] = field(default_factory=list)
    flags: list[ReviewFlag] = field(default_factory=list)

    def add(  # one record per correction, all fields required
        self, path: str, original: str, corrected: str | None, reason: str, method: Method
    ) -> None:
        self.items.append(Correction(path, original, corrected, reason, method))

    @property
    def corrections(self) -> list[str]:
        """Human-readable form, for reports and review files."""
        return [item.message() for item in self.items]


def clean(
    analysis: ArticleAnalysis,
    paragraphs: list[str],
    *,
    publisher_names: tuple[str, ...],
    published: date | None = None,
) -> tuple[ArticleAnalysis, CleanupReport]:
    report = CleanupReport()
    issues = semantic_issues(analysis, paragraphs, published)
    publisher = {normalize(name) for name in publisher_names}

    where = _clean_places(analysis.where, issues, publisher, report)
    places = {normalize(p.name_as_written) for p in analysis.where}
    who = _clean_actors(analysis.who, issues, publisher, places, report)

    for index, claim in enumerate(analysis.claims):
        if issues.get(f"$.claims[{index}]") is Issue.POSSIBLE_TENSE_MISMATCH:
            report.flags.append(
                ReviewFlag(
                    f"$.claims[{index}]",
                    claim.text,
                    f"quote speaks of the future ({claim.evidence.quote!r}) but the claim "
                    "reads as done",
                )
            )
        path = f"$.claims[{index}].attributed_to"
        if issues.get(path) is Issue.ANONYMOUS_SOURCE_UNATTRIBUTED:
            report.flags.append(
                ReviewFlag(path, claim.text, "claim from an anonymous source has no attribution")
            )

    for index, cause in enumerate(analysis.why):
        if issues.get(f"$.why[{index}]") is Issue.UNINFORMATIVE_CAUSE:
            report.flags.append(ReviewFlag(f"$.why[{index}]", cause.text, "uninformative cause"))

    event_date = analysis.when.event_date
    if issues.get("$.when.event_date") is Issue.EVENT_DATE_WITHOUT_DATE_WORDS:
        report.add(
            "$.when.event_date",
            str(event_date.value),
            None,
            "evidence quote contains no date words",
            "heuristic",
        )
        event_date = EventDate(value=None, evidence=None)

    timeline = []
    for index, entry in enumerate(analysis.when.timeline):
        if issues.get(f"$.when.timeline[{index}]") is Issue.TIMELINE_PUBLICATION_ENTRY:
            report.add(
                f"$.when.timeline[{index}]",
                entry.event,
                None,
                "the article's own publication is not an event",
                "heuristic",
            )
            continue
        if issues.get(f"$.when.timeline[{index}]") is Issue.TIMELINE_PUBLICATION_DATE:
            report.add(
                f"$.when.timeline[{index}]",
                f"{entry.date}: {entry.event}",
                None,
                "dated with the publication date, no date in the quote",
                "heuristic",
            )
            continue
        timeline.append(entry)

    cleaned = analysis.model_copy(
        update={
            "who": who,
            "where": where,
            "when": analysis.when.model_copy(
                update={"event_date": event_date, "timeline": timeline}
            ),
        }
    )
    return cleaned, report


def upcoming(analysis: ArticleAnalysis, published: date) -> list[str]:
    """What happens next, derived (never asked of the model separately).

    Prediction claims, plus timeline entries dated after the publication date. A
    month-only date in the publication month is ambiguous and is not counted.
    """
    items = [c.text for c in analysis.claims if c.kind == "prediction"]
    items += [
        f"{t.date}: {t.event}"
        for t in analysis.when.timeline
        if t.date is not None and t.date > published.isoformat()
    ]
    return items


def _is_publisher(name: str, publisher: set[str]) -> bool:
    normalized = normalize(name)
    return any(p in normalized or normalized in p for p in publisher)


_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")


def _actor_key(name: str) -> str:
    """Comparison key: normalized, without a leading article ("The judge" == "judge")."""
    return _LEADING_ARTICLE.sub("", normalize(name))


def _contains_words(longer: str, shorter: str) -> bool:
    return longer != shorter and re.search(rf"\b{re.escape(shorter)}\b", longer) is not None


def _unambiguous_duplicate(name: str, kept: list[str], all_names: list[str]) -> str | None:
    """The kept name `name` duplicates, if exactly one and the shorter is unambiguous."""
    for other in kept:
        if name == other:
            return other
        shorter, longer = sorted((name, other), key=len)
        if not _contains_words(longer, shorter):
            continue
        partners = {n for n in all_names if _contains_words(n, shorter)}
        if len(partners) == 1:
            return other
    return None


def _clean_actors(  # the cleanup context, passed explicitly
    actors: list[Actor],
    issues: dict[str, Issue],
    publisher: set[str],
    places: set[str],
    report: CleanupReport,
) -> list[Actor]:
    kept: list[Actor] = []
    kept_names: list[str] = []
    all_names = sorted({_actor_key(a.full_name) for a in actors})
    for index, original in enumerate(actors):
        actor = original
        path = f"$.who[{index}]"
        if _is_publisher(actor.name_as_written, publisher):
            report.add(path, actor.name_as_written, None, "the publisher itself", "heuristic")
            continue
        if normalize(actor.name_as_written) in places or normalize(actor.full_name) in places:
            # Either side can be the error (an airport listed as an actor, or a court
            # listed as a place), so code cannot decide: flag, never delete.
            report.flags.append(
                ReviewFlag(path, actor.full_name, "listed both as an actor and as a place")
            )
        if issues.get(f"{path}.name_as_written") is Issue.NAME_NOT_IN_ARTICLE:
            report.add(
                path, actor.name_as_written, None, "name not in the article", "deterministic"
            )
            continue
        if issues.get(f"{path}.full_name") is Issue.NAME_NOT_IN_ARTICLE:
            report.add(
                f"{path}.full_name",
                actor.full_name,
                actor.name_as_written,
                "expanded name not in the article",
                "deterministic",
            )
            actor = actor.model_copy(update={"full_name": actor.name_as_written})
        key = _actor_key(actor.full_name)
        duplicate_of = _unambiguous_duplicate(key, kept_names, all_names)
        if duplicate_of is not None:
            exact = duplicate_of == key
            report.add(
                path,
                actor.full_name,
                None,
                "duplicate actor" if exact else f"near-duplicate of {duplicate_of!r}",
                "deterministic" if exact else "heuristic",
            )
            continue
        kept_names.append(key)
        kept.append(actor)
    return kept


def _clean_places(
    places: list[Place],
    issues: dict[str, Issue],
    publisher: set[str],
    report: CleanupReport,
) -> list[Place]:
    kept: list[Place] = []
    seen: set[str] = set()
    for index, original in enumerate(places):
        place = original
        path = f"$.where[{index}]"
        if _is_publisher(place.name_as_written, publisher):
            report.add(path, place.name_as_written, None, "the publisher itself", "heuristic")
            continue
        issue = issues.get(f"{path}.name_as_written")
        if issue is Issue.NAME_NOT_IN_ARTICLE:
            report.add(
                path, place.name_as_written, None, "name not in the article", "deterministic"
            )
            continue
        if issue is Issue.PLACE_NAME_WITH_ADDED_SUFFIX:
            core = core_place_name(place.name_as_written)
            report.add(
                path,
                place.name_as_written,
                core,
                "generic word added to a name the article lists",
                "heuristic",
            )
            place = place.model_copy(update={"name_as_written": core})
        key = normalize(place.name_as_written)
        if key in seen:
            report.add(path, place.name_as_written, None, "duplicate place", "deterministic")
            continue
        seen.add(key)
        kept.append(place)
    return kept
