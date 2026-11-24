"""Analysis schema v1: the contract between the prompt, the model and the database.

Pydantic is the source of truth. Its JSON Schema is sent to the model in the prompt,
decoding, and the same model validates the response. Design rules (docs/analysis.md):
- Every important statement carries `evidence` (paragraph number plus a verbatim quote),
  which code verifies later. The model's own confidence is not trusted.
- Facts, attributed statements, allegations, opinions, predictions and inferences stay
  distinct.
- Unknown means null or an empty list. Nothing is invented to fill a field.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "7"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    paragraph: int = Field(description="Number of the paragraph, e.g. 3 for [P3].", ge=1)
    quote: str = Field(
        description="Short verbatim quote copied exactly from that paragraph (max ~25 words).",
        max_length=300,
    )


class Overview(_Strict):
    headline: str = Field(description="Neutral one-line description of the main event.")
    report_type: Literal[
        "new_event", "update", "reaction", "investigation", "analysis", "background", "opinion"
    ]
    event_status: Literal["completed", "ongoing", "planned", "uncertain"]


class What(_Strict):
    main_event: str
    secondary_events: list[str] = Field(default_factory=list, max_length=8)


class Actor(_Strict):
    name_as_written: str = Field(description="Exactly as written in the article.")
    full_name: str = Field(
        description="The fullest form of the name that appears IN THE ARTICLE "
        "(e.g. 'Nasser Bourita' if the article also writes 'Bourita')."
    )
    type: Literal[
        "person",
        "government_body",
        "political_party",
        "company",
        "ngo",
        "international_org",
        "court",
        "media",
        "group",
        "country",
        "other",
    ]
    role: str = Field(description="Role in THIS event, e.g. 'announced the decision'.")
    involvement: Literal["central", "participant", "quoted", "mentioned"]
    evidence: Evidence


# ISO date with honest precision: "2026", "2026-11" or "2026-11-23".
IsoDate = Annotated[str, Field(pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")]


class EventDate(_Strict):
    value: IsoDate | None = Field(
        description="Date of the MAIN event (YYYY-MM-DD, YYYY-MM or YYYY), or null if the "
        "article does not say. Never copy the publication date without textual support."
    )
    evidence: Evidence | None = Field(
        description="Quote containing the date words. Required whenever value is set."
    )

    @model_validator(mode="after")
    def _dated_needs_evidence(self) -> "EventDate":
        if self.value is not None and self.evidence is None:
            msg = "a dated event needs evidence"
            raise ValueError(msg)
        return self

    @property
    def precision(self) -> Literal["day", "month", "year", "unknown"]:
        """Derived in code from the value's form, never asked of the model (it disagreed)."""
        return {4: "year", 7: "month", 10: "day"}.get(len(self.value or ""), "unknown")  # type: ignore[return-value]  # dict of Literals


class TimelineEntry(_Strict):
    date: IsoDate | None
    event: str = Field(description="A development reported in the article (not publication).")
    evidence: Evidence


class When(_Strict):
    event_date: EventDate
    timeline: list[TimelineEntry] = Field(
        default_factory=list,
        max_length=12,
        description="Every dated event the article mentions, including background, oldest first.",
    )

    @field_validator("event_date", mode="before")
    @classmethod
    def _null_means_no_date(cls, value: object) -> object:
        # Gemma sometimes writes `"event_date": null` for "no date" (seen 2026-11-24).
        # It means exactly {"value": null, "evidence": null}: accepted, nothing is lost.
        return {"value": None, "evidence": None} if value is None else value


class Place(_Strict):
    """A geographic place exactly as named in the article, and its role.

    No normalized name, country or hierarchy from the model: the gazetteer resolves those
    (docs/analysis.md section 5). The name is checked to appear verbatim in the article.
    """

    name_as_written: str = Field(
        description="A proper place name exactly as written (e.g. 'Agadir', 'Sidi Ayad'). "
        "Never a description such as 'the region' or 'buffer zone'."
    )
    role: Literal["event_location", "actor_location", "related_event", "mentioned"]
    evidence: Evidence


ClaimKind = Literal["fact", "allegation", "opinion", "prediction"]


class Claim(_Strict):
    text: str = Field(description="The claim, in your own words.")
    kind: ClaimKind = Field(
        description="fact: presented as established. allegation: an accusation not "
        "established. opinion: a judgment or assessment. prediction: about the future."
    )
    attributed_to: str | None = Field(
        description="The named person or organization who said it, or null when the article "
        "itself states it. Never an abstract phrase."
    )
    evidence: Evidence


class Cause(_Strict):
    text: str
    kind: Literal["stated", "attributed", "interpretation"]
    attributed_to: str | None
    evidence: Evidence


class Impact(_Strict):
    area: Literal[
        "citizens",
        "government",
        "economy",
        "public_services",
        "international",
        "environment",
        "security",
        "other",
    ]
    text: str
    certainty: Literal["confirmed", "expected", "possible"]
    evidence: Evidence


class Tone(_Strict):
    label: Literal[
        "neutral", "positive", "negative", "critical", "concerned", "urgent", "analytical"
    ]
    explanation: str


class ArticleAnalysis(_Strict):
    overview: Overview
    what: What
    who: list[Actor] = Field(max_length=30)
    when: When
    where: list[Place] = Field(max_length=20)
    claims: list[Claim] = Field(max_length=15, description="The 15 most important at most.")
    why: list[Cause] = Field(default_factory=list, max_length=10)
    how: str | None = Field(description="How it happened, if the article says; else null.")
    impact: list[Impact] = Field(default_factory=list, max_length=12)
    context: list[str] = Field(
        default_factory=list, max_length=8, description="Background given BY THE ARTICLE."
    )
    open_questions: list[str] = Field(
        default_factory=list, max_length=3, description="The 3 most important unanswered ones."
    )
    topics: list[str] = Field(max_length=8)
    tone: Tone
