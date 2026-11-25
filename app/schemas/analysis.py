"""Briefing responses: model-written analysis fields, never article text.

Built from `analysis_briefings.briefing` (app/analysis/briefing.py), which has no evidence
quotes and no unverified item. These schemas list the servable fields explicitly; any
other key in the stored JSON is ignored, so nothing new is served by accident.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


# Scalar fields outside any list may be null: the originality filter nulls a copied one
# (app/analysis/briefing.py). A null must not make the whole briefing invalid.
class OverviewOut(_Out):
    headline: str | None
    report_type: str | None
    event_status: str | None


class WhatOut(_Out):
    main_event: str | None
    secondary_events: list[str] = []


class ActorOut(_Out):
    name_as_written: str
    full_name: str
    type: str
    role: str
    involvement: str


class EventDateOut(_Out):
    value: str | None = Field(
        default=None, description="Null when unknown or when its support was not verified."
    )


class TimelineEntryOut(_Out):
    date: str | None = None
    event: str


class WhenOut(_Out):
    event_date: EventDateOut = EventDateOut()
    timeline: list[TimelineEntryOut] = []


class PlaceOut(_Out):
    name_as_written: str
    role: str


class ClaimOut(_Out):
    text: str
    kind: str
    attributed_to: str | None = None


class CauseOut(_Out):
    text: str
    kind: str
    attributed_to: str | None = None


class ImpactOut(_Out):
    area: str
    text: str
    certainty: str


class ToneOut(_Out):
    label: str | None
    explanation: str | None


class BriefingOut(_Out):
    overview: OverviewOut
    what: WhatOut
    who: list[ActorOut] = []
    when: WhenOut = WhenOut()
    where: list[PlaceOut] = []
    claims: list[ClaimOut] = []
    why: list[CauseOut] = []
    how: str | None = None
    impact: list[ImpactOut] = []
    context: list[str] = []
    open_questions: list[str] = []
    topics: list[str] = []
    tone: ToneOut | None = None


class BriefingSource(_Out):
    slug: str
    name: str
    site_url: str


class BriefingDetail(_Out):
    """One article's briefing. Read the article itself at `source_url` (always link out)."""

    article_id: int
    title: str
    source_url: str = Field(description="Link to the original article (always link out).")
    published_at: datetime
    source: BriefingSource
    analyzer_version: str
    status: str = Field(description="`succeeded` or `needs_review`.")
    review_required: bool = Field(
        description="True when some items could not be verified and were removed."
    )
    removed_items: int = Field(default=0, description="How many unverified items were removed.")
    copied_items: int = Field(
        default=0,
        description="How many items were removed for copying article wording (0 on older "
        "briefings).",
    )
    unverified_fields: list[str] = Field(
        default=[],
        description="Paths of fields whose support could not be verified (empty on older "
        "briefings).",
    )
    updated_at: datetime
    briefing: BriefingOut


class BriefingPlace(_Out):
    name_as_written: str
    role: str


class BriefingActor(_Out):
    full_name: str
    type: str
    role: str
    involvement: str


class BriefingSummary(_Out):
    article_id: int
    title: str
    canonical_url: str = Field(description="Link to the original article (always link out).")
    published_at: datetime
    source: BriefingSource = Field(description="The publisher (attribution).")
    headline: str | None
    report_type: str | None
    event_status: str | None
    event_date: str | None
    places: list[BriefingPlace] = Field(description="Up to 3, event locations first.")
    actors: list[BriefingActor] = Field(description="Up to 3, central actors first.")
    review_required: bool
