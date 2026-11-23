"""The quote checker: the guard against invented quotes. Synthetic text only."""

import pytest

from app.analysis.evidence import (
    EvidenceStatus,
    Issue,
    check,
    core_place_name,
    normalize,
    semantic_issues,
)
from app.analysis.schema import ArticleAnalysis, Evidence

PARAGRAPHS = [
    "The ministry announced on Monday that the system will launch in December.",
    "\u201cWe are ready,\u201d said the director \u2013 adding that tests were complete.",
    "Officials in Rabat declined to comment on the budget.",
]


def ev(paragraph: int, quote: str) -> Evidence:
    return Evidence(paragraph=paragraph, quote=quote)


@pytest.mark.parametrize(
    ("evidence", "status"),
    [
        (ev(1, "the system will launch in December"), EvidenceStatus.VERIFIED),
        (ev(1, "THE SYSTEM   WILL launch in december."), EvidenceStatus.VERIFIED),  # case, spaces
        (
            ev(2, '"We are ready," said the director - adding'),
            EvidenceStatus.VERIFIED,
        ),  # quotes, dash
        (ev(3, "the system will launch in December"), EvidenceStatus.MISPLACED),  # wrong paragraph
        (ev(1, "the system will launch in January"), EvidenceStatus.FABRICATED),  # altered fact
        (ev(1, "officials confirmed the budget"), EvidenceStatus.FABRICATED),  # invented
        (ev(9, "Officials in Rabat declined"), EvidenceStatus.MISPLACED),  # out-of-range, real text
        (ev(9, "never written anywhere"), EvidenceStatus.BAD_PARAGRAPH),
        (ev(1, " ... "), EvidenceStatus.FABRICATED),  # empty after trimming
    ],
)
def test_check(evidence: Evidence, status: EvidenceStatus) -> None:
    assert check(evidence, PARAGRAPHS) is status


def test_paraphrase_inside_a_quote_is_not_accepted() -> None:
    assert check(ev(1, "the system launches in December"), PARAGRAPHS) is EvidenceStatus.FABRICATED


def test_normalize_folds_typography() -> None:
    assert normalize("\u201cA\u00a0\u2014 B\u201d\u2026") == normalize('"a - b"...')


def test_requoted_nested_quotes_still_verify() -> None:
    """Seen with Qwen: curly double quotes in the article came back as single quotes."""
    paragraph = ["Trump welcomed the move as a \u201ccorrect and proper decision.\u201d"]

    assert check(ev(1, "welcomed the move as a 'correct and proper decision.'"), paragraph) is (
        EvidenceStatus.VERIFIED
    )
    assert check(ev(1, "welcomed the move as a 'wrong decision'"), paragraph) is (
        EvidenceStatus.FABRICATED
    )


def test_semantic_issues_catch_renamed_names_and_undated_event_dates() -> None:
    paragraphs = ["The Atlas talks resumed in Geneva.", "The minister Bourita attended."]
    quote = {"paragraph": 1, "quote": "The Atlas talks resumed"}
    analysis = ArticleAnalysis.model_validate(
        {
            "overview": {
                "headline": "h",
                "summary": "s",
                "report_type": "update",
                "event_status": "ongoing",
            },
            "what": {"main_event": "talks"},
            "who": [
                {
                    "name_as_written": "Bourita",
                    "full_name": "Nasser Bourita",
                    "type": "person",
                    "role": "attended",
                    "involvement": "participant",
                    "evidence": {"paragraph": 2, "quote": "Bourita attended"},
                }
            ],
            "when": {"event_date": {"value": "2026-11-23", "evidence": quote}},
            "where": [
                {"name_as_written": "Geneva", "role": "event_location", "evidence": quote},
                {"name_as_written": "High Atlas", "role": "mentioned", "evidence": quote},
            ],
            "claims": [],
            "how": None,
            "topics": [],
            "tone": {"label": "neutral", "explanation": "x"},
        }
    )

    assert semantic_issues(analysis, paragraphs) == {
        "$.who[0].full_name": Issue.NAME_NOT_IN_ARTICLE,  # expanded beyond the article
        "$.where[1].name_as_written": Issue.NAME_NOT_IN_ARTICLE,  # substituted name
        "$.when.event_date": Issue.EVENT_DATE_WITHOUT_DATE_WORDS,  # pretend evidence
    }


@pytest.mark.parametrize(
    ("quote", "dated"),
    [
        ("Nvidia on Monday reinstated Huang", True),
        ("issued Nov. 23, the system", True),
        ("in 1987 the body was found", True),
        ("talks held last week in Rabat", True),
        ("Nearly four decades after the death of a four-year-old girl", False),
        ("The withdrawal of the mission could increase the risk", False),
        ("a 10-year plan", False),
    ],
)
def test_event_date_quotes_need_real_date_words(quote: str, dated: bool) -> None:
    paragraphs = [quote]
    analysis = ArticleAnalysis.model_validate(
        {
            "overview": {
                "headline": "h",
                "summary": "s",
                "report_type": "update",
                "event_status": "ongoing",
            },
            "what": {"main_event": "x"},
            "who": [],
            "where": [],
            "claims": [],
            "how": None,
            "when": {
                "event_date": {"value": "2026-11-23", "evidence": {"paragraph": 1, "quote": quote}},
                "timeline": [
                    {
                        "date": "2026-11-23",
                        "event": "Publication of the article.",
                        "evidence": {"paragraph": 1, "quote": quote},
                    }
                ],
            },
            "topics": [],
            "tone": {"label": "neutral", "explanation": "x"},
        }
    )

    issues = semantic_issues(analysis, paragraphs)

    assert ("$.when.event_date" not in issues) is dated
    assert issues["$.when.timeline[0]"] is Issue.TIMELINE_PUBLICATION_ENTRY


@pytest.mark.parametrize(
    ("name", "core"),
    [("Taourirt province", "Taourirt"), ("Agadir", "Agadir"), ("Casablanca city", "Casablanca"),
     ("Oriental region", "Oriental"), ("Province of Kenitra", "Province of Kenitra")],
)  # fmt: skip
def test_core_place_name(name: str, core: str) -> None:
    assert core_place_name(name) == core
