"""Contract: whatever the real briefing builder stores, the API schemas accept (IA-001).

The originality filter nulls a copied scalar field outside any list. Those nulls must not
make `BriefingOut` reject the briefing, or the API would silently drop it. Synthetic data
only.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.analysis.briefing import make_briefing
from app.schemas.analysis import BriefingDetail, BriefingOut, BriefingSource

ARTICLE = [
    "The regional council of the synthetic province approved a new water plan for twelve "
    "rural communes on Monday after a long and heated public debate among its members.",
    "Officials described the decision as a cautious but necessary step for the coming dry "
    "season and promised quarterly progress reports to every affected village assembly.",
]
COPY_1 = "approved a new water plan for twelve rural communes on Monday"
COPY_2 = "a cautious but necessary step for the coming dry season"


def analysis() -> dict[str, Any]:
    return {
        "overview": {
            "headline": f"The council {COPY_1}",
            "report_type": f"Officials called it {COPY_2}",
            "event_status": f"It was {COPY_1}",
        },
        "what": {"main_event": f"Members {COPY_1}", "secondary_events": []},
        "who": [],
        "when": {"event_date": {"value": None}, "timeline": []},
        "where": [],
        "claims": [],
        "how": None,
        "topics": ["water"],
        "tone": {"label": f"Called {COPY_2}", "explanation": f"Officials said {COPY_2}"},
    }


# Every scalar field outside a list that the originality filter can null.
NULLABLE_SCALARS = [
    ("overview", "headline"),
    ("overview", "report_type"),
    ("overview", "event_status"),
    ("what", "main_event"),
    ("tone", "label"),
    ("tone", "explanation"),
]


def test_copied_scalars_are_nulled_by_the_real_builder() -> None:
    briefing = make_briefing(analysis(), [], paragraphs=ARTICLE)
    for section, field in NULLABLE_SCALARS:
        assert briefing[section][field] is None, (section, field)


def test_a_briefing_with_copied_scalars_validates_through_the_api_schemas() -> None:
    briefing = make_briefing(analysis(), [], paragraphs=ARTICLE)
    out = BriefingOut.model_validate(briefing)
    assert out.overview.headline is None
    assert out.what.main_event is None
    assert out.tone is not None and out.tone.explanation is None
    detail = BriefingDetail(
        article_id=1,
        title="Synthetic title",
        source_url="https://example.test/a",
        published_at=datetime(2026, 11, 23, tzinfo=UTC),
        source=BriefingSource(slug="s", name="Synthetic", site_url="https://example.test"),
        analyzer_version="v",
        status="succeeded",
        review_required=False,
        updated_at=datetime(2026, 11, 23, tzinfo=UTC),
        briefing=out,
    )
    dumped = detail.model_dump(mode="json")
    for paragraph in ARTICLE:
        assert paragraph not in str(dumped)


@pytest.mark.parametrize(("section", "field"), NULLABLE_SCALARS)
def test_each_nullable_scalar_alone_validates(section: str, field: str) -> None:
    data = make_briefing(analysis(), [], paragraphs=None)
    data[section][field] = None
    BriefingOut.model_validate(data)
