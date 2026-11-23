"""Deterministic cleanup of a validated analysis, applied after the evidence checks.

Prompt rules reduce errors but never eliminate them, so code fixes what can be fixed
safely and records every correction (nothing is changed silently):

- duplicate actors and places are merged (same name, case-insensitive);
- the publisher's own name is never an actor or a place;
- "publication of the article" entries are dropped from the timeline;
- an event date whose evidence contains no date words becomes unknown;
- an actor's `full_name` that does not appear in the article falls back to the name as
  written;
- a place whose name does not appear in the article is dropped, unless only a generic
  word was added ("Taourirt province" when the article lists "Taourirt, ... provinces"):
  then the name is kept as written ("Taourirt").
"""

from dataclasses import dataclass, field

from app.analysis.evidence import Issue, core_place_name, normalize, semantic_issues
from app.analysis.schema import Actor, ArticleAnalysis, EventDate, Place


@dataclass
class CleanupReport:
    corrections: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.corrections.append(message)


def clean(
    analysis: ArticleAnalysis, paragraphs: list[str], *, publisher_names: tuple[str, ...]
) -> tuple[ArticleAnalysis, CleanupReport]:
    report = CleanupReport()
    issues = semantic_issues(analysis, paragraphs)
    publisher = {normalize(name) for name in publisher_names}

    who = _clean_actors(analysis.who, issues, publisher, report)
    where = _clean_places(analysis.where, issues, publisher, report)

    event_date = analysis.when.event_date
    if issues.get("$.when.event_date") is Issue.EVENT_DATE_WITHOUT_DATE_WORDS:
        report.note(f"event date {event_date.value} dropped: evidence has no date words")
        event_date = EventDate(value=None, evidence=None)

    timeline = []
    for index, entry in enumerate(analysis.when.timeline):
        if issues.get(f"$.when.timeline[{index}]") is Issue.TIMELINE_PUBLICATION_ENTRY:
            report.note(f"timeline entry dropped (publication is not an event): {entry.event}")
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


def _is_publisher(name: str, publisher: set[str]) -> bool:
    normalized = normalize(name)
    return any(p in normalized or normalized in p for p in publisher)


def _clean_actors(
    actors: list[Actor],
    issues: dict[str, Issue],
    publisher: set[str],
    report: CleanupReport,
) -> list[Actor]:
    kept: list[Actor] = []
    seen: set[str] = set()
    for index, original in enumerate(actors):
        actor = original
        if _is_publisher(actor.name_as_written, publisher):
            report.note(f"actor dropped (the publisher itself): {actor.name_as_written}")
            continue
        if issues.get(f"$.who[{index}].name_as_written") is Issue.NAME_NOT_IN_ARTICLE:
            report.note(f"actor dropped (name not in article): {actor.name_as_written}")
            continue
        if issues.get(f"$.who[{index}].full_name") is Issue.NAME_NOT_IN_ARTICLE:
            report.note(
                f"full name {actor.full_name!r} not in article; kept {actor.name_as_written!r}"
            )
            actor = actor.model_copy(update={"full_name": actor.name_as_written})
        key = normalize(actor.full_name)
        if key in seen:
            report.note(f"duplicate actor merged: {actor.full_name}")
            continue
        seen.add(key)
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
        if _is_publisher(place.name_as_written, publisher):
            report.note(f"place dropped (the publisher itself): {place.name_as_written}")
            continue
        issue = issues.get(f"$.where[{index}].name_as_written")
        if issue is Issue.NAME_NOT_IN_ARTICLE:
            report.note(f"place dropped (name not in article): {place.name_as_written}")
            continue
        if issue is Issue.PLACE_NAME_WITH_ADDED_SUFFIX:
            core = core_place_name(place.name_as_written)
            report.note(f"place name {place.name_as_written!r} kept as written: {core!r}")
            place = place.model_copy(update={"name_as_written": core})
        key = normalize(place.name_as_written)
        if key in seen:
            report.note(f"duplicate place merged: {place.name_as_written}")
            continue
        seen.add(key)
        kept.append(place)
    return kept
