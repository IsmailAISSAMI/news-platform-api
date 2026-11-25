"""The quote checker: the guard against invented quotes. Synthetic text only."""

from datetime import date

import pytest

from app.analysis.evidence import (
    EvidenceReport,
    EvidenceStatus,
    Issue,
    check,
    core_place_name,
    mentions,
    normalize,
    semantic_issues,
    verify,
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


# --- Precision of the name checks (part C) ------------------------------------------------


@pytest.mark.parametrize(
    ("name", "found"),
    [
        ("Sale", False),  # only inside "wholesale": a substring, not the name
        ("Rabat", True),
        ("Morocco", True),  # the article writes "Morocco's" (apostrophe dropped)
        ("", False),  # an empty name is in no article
        ("  ", False),
        ("Hassan II", True),
    ],
)
def test_mentions_matches_whole_words(name: str, found: bool) -> None:
    article = normalize("Wholesale prices rose in Rabat, Morocco\u2019s capital, under Hassan II.")
    assert mentions(article, name) is found


def test_normalize_drops_invisible_characters_and_folds_dashes() -> None:
    assert normalize("Sidi\u200b Ifni\u00ad \u2010 \u2212 x\ufeff") == normalize("Sidi Ifni - - x")


def test_a_quote_with_a_soft_hyphen_in_the_article_verifies() -> None:
    assert check(ev(1, "the budget vote passed"), ["Then the bud\u00adget vote passed."]) is (
        EvidenceStatus.VERIFIED
    )


@pytest.mark.parametrize("name", ["the region", "The province", "a city", "region"])
def test_core_place_name_keeps_descriptions(name: str) -> None:
    assert core_place_name(name) == name


# --- O1, O2, O3 -----------------------------------------------------------------------------


def base(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "overview": {"headline": "h", "report_type": "update", "event_status": "ongoing"},
        "what": {"main_event": "The council approved the new water plan"},
        "who": [], "where": [], "claims": [], "how": None, "topics": [],
        "when": {"event_date": {"value": None, "evidence": None}},
        "tone": {"label": "neutral", "explanation": "x"},
    }  # fmt: skip
    data.update(overrides)
    return data


def claim(paragraph: int, attributed_to: str | None) -> dict[str, object]:
    return {"text": "The plan costs more", "kind": "fact", "attributed_to": attributed_to,
            "evidence": {"paragraph": paragraph, "quote": "the plan"}}  # fmt: skip


@pytest.mark.parametrize(
    ("paragraph", "attributed_to", "flagged"),
    [
        ("Sources said the plan would cost more.", None, True),
        ("The plan, sources told Hespress, costs more.", None, True),
        ("According to Hespress sources, the plan costs more.", None, True),
        ("According to informed sources, the plan costs more.", None, True),
        ("A source said the plan costs more.", None, True),
        ("Sources close to the council say the plan costs more.", None, True),
        ("Sources said the plan would cost more.", "sources close to the council", False),
        ("Sources said the plan would cost more.", "  ", True),  # blank is no attribution
        ("The council said the plan costs more.", None, False),  # named, no marker
        ("Water resources said to be scarce; the plan costs more.", None, False),
        ("According to the minister, the plan costs more.", None, False),
    ],
)
def test_anonymous_source_claims_need_attribution(
    paragraph: str, attributed_to: str | None, flagged: bool
) -> None:
    data = base(claims=[claim(1, attributed_to)])
    issues = semantic_issues(ArticleAnalysis.model_validate(data), [paragraph])

    assert (issues.get("$.claims[0].attributed_to") is Issue.ANONYMOUS_SOURCE_UNATTRIBUTED) is (
        flagged
    )


def test_only_the_claims_own_paragraph_counts_for_anonymous_sources() -> None:
    paragraphs = ["The council said the plan costs more.", "Sources said talks stalled."]
    data = base(claims=[claim(1, None), claim(9, None)])  # 9: a bad pointer is not flagged

    assert semantic_issues(ArticleAnalysis.model_validate(data), paragraphs) == {}


def entry(day: str, quote: str) -> dict[str, object]:
    return {"date": day, "event": "Council vote", "evidence": {"paragraph": 1, "quote": quote}}


@pytest.mark.parametrize(
    ("day", "quote", "published", "flagged"),
    [
        ("2026-11-23", "the council voted the plan", date(2026, 11, 23), True),
        ("2026-11-23", "the council voted on Monday", date(2026, 11, 23), False),  # dated
        ("2026-11-20", "the council voted the plan", date(2026, 11, 23), False),  # other day
        ("2026-11", "the council voted the plan", date(2026, 11, 23), False),  # month only
        ("2026-11-23", "the council voted the plan", None, False),  # no date given
    ],
)
def test_timeline_entries_dated_with_the_publication_date(
    day: str, quote: str, published: date | None, flagged: bool
) -> None:
    data = base(when={"event_date": {"value": None, "evidence": None},
                      "timeline": [entry(day, quote)]})  # fmt: skip
    issues = semantic_issues(ArticleAnalysis.model_validate(data), [quote], published)

    assert (issues.get("$.when.timeline[0]") is Issue.TIMELINE_PUBLICATION_DATE) is flagged


def cause(text: str) -> dict[str, object]:
    return {"text": text, "kind": "stated", "attributed_to": None,
            "evidence": {"paragraph": 1, "quote": "x"}}  # fmt: skip


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Drought", True),  # too short
        ("the council approved the new water plan", True),  # restates the main event
        ("approved the new water plan", True),  # contained in the main event
        ("The council approved the new water plan after the drought", True),  # contains it
        ("Three dry years emptied the Al Massira reservoir", False),
    ],
)
def test_uninformative_causes(text: str, flagged: bool) -> None:
    issues = semantic_issues(ArticleAnalysis.model_validate(base(why=[cause(text)])), ["x"])

    assert (issues.get("$.why[0]") is Issue.UNINFORMATIVE_CAUSE) is flagged


@pytest.mark.parametrize(
    ("quote", "status"),
    [
        ("the budget", EvidenceStatus.TOO_SHORT),  # 2 tokens, 10 characters
        ("declined to comment", EvidenceStatus.TOO_SHORT),  # 3 tokens, 19 characters
        ("Officials in Rabat declined", EvidenceStatus.VERIFIED),  # 4 tokens
        ("declined to comment.", EvidenceStatus.TOO_SHORT),  # edge punctuation not counted
    ],
)
def test_short_quotes_prove_nothing(quote: str, status: EvidenceStatus) -> None:
    assert check(ev(3, quote), PARAGRAPHS) is status


def test_short_event_date_quotes_are_allowed() -> None:
    assert check(ev(1, "on Monday"), PARAGRAPHS, allow_short=True) is EvidenceStatus.VERIFIED
    analysis = minimal({"value": "2026-11-23", "evidence": {"paragraph": 1, "quote": "on Monday"}})
    assert verify(analysis, PARAGRAPHS).statuses == {
        "$.when.event_date.evidence": EvidenceStatus.VERIFIED
    }


def test_quotes_match_whole_words_only() -> None:
    paragraphs = ["The wholesale market reopened in the morning after the storm."]
    assert check(ev(1, "sale market reopened in the"), paragraphs) is EvidenceStatus.FABRICATED
    assert check(ev(1, "market reopened in the mor"), paragraphs) is EvidenceStatus.FABRICATED
    assert check(ev(1, "market reopened in the morning"), paragraphs) is EvidenceStatus.VERIFIED


def test_verified_rate_is_none_without_quotes() -> None:
    assert EvidenceReport().verified_rate is None
    report = EvidenceReport({"a": EvidenceStatus.VERIFIED, "b": EvidenceStatus.TOO_SHORT})
    assert report.verified_rate == 0.5


@pytest.mark.parametrize(
    ("quote", "dated"),
    [
        ("10 people were injured in the fire", False),  # a bare number is not a date
        ("on 23 November the council met", True),
        ("the 3rd round of talks began", False),
        ("in 2025 the plan was adopted", True),
    ],
)
def test_digits_are_date_words_only_as_years_or_with_a_month(quote: str, dated: bool) -> None:
    analysis = minimal({"value": "2026-11-23", "evidence": {"paragraph": 1, "quote": quote}})
    assert ("$.when.event_date" not in semantic_issues(analysis, [quote])) is dated


def new_claim(text: str, quote: str, attributed_to: str | None = None) -> dict[str, object]:
    return {"text": text, "kind": "fact", "attributed_to": attributed_to,
            "evidence": {"paragraph": 1, "quote": quote}}  # fmt: skip


def test_due_to_is_a_cause_not_the_future() -> None:
    paragraphs = ["The road closed due to heavy rain last night.", "The minister is due to sign."]
    analysis = minimal(
        None,
        claims=[
            new_claim("The road closed.", "The road closed due to heavy rain"),
            {**new_claim("The minister signed.", "The minister is due to sign"),
             "evidence": {"paragraph": 2, "quote": "The minister is due to sign"}},
        ],
    )  # fmt: skip
    issues = semantic_issues(analysis, paragraphs)
    assert "$.claims[0]" not in issues
    assert issues["$.claims[1]"] is Issue.POSSIBLE_TENSE_MISMATCH


@pytest.mark.parametrize(
    ("attributed_to", "flagged"),
    [
        ("Karim Benali", False),  # named in the article
        ("Youssef Amrani", True),  # never named: invented or substituted
        ("Hespress sources", False),  # anonymous-source description
        ("an unnamed official", False),
        (None, False),
    ],
)
def test_attributions_must_be_named_in_the_article(
    attributed_to: str | None, flagged: bool
) -> None:
    paragraphs = ["Karim Benali said the harbour works were finished in spring."]
    analysis = minimal(
        None,
        claims=[new_claim("Works finished.", "the harbour works were finished", attributed_to)],
    )
    issues = semantic_issues(analysis, paragraphs)
    assert (issues.get("$.claims[0].attributed_to") is Issue.ATTRIBUTION_NOT_IN_ARTICLE) is flagged


def minimal(event_date: object, **extra: object) -> ArticleAnalysis:
    data: dict[str, object] = {
        "overview": {"headline": "h", "report_type": "update", "event_status": "ongoing"},
        "what": {"main_event": "x"},
        "who": [], "where": [], "claims": [], "how": None,
        "when": {"event_date": event_date},
        "topics": [], "tone": {"label": "neutral", "explanation": "x"},
    }  # fmt: skip
    data.update(extra)
    return ArticleAnalysis.model_validate(data)
