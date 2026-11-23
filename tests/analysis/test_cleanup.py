"""Code-side cleanup: every correction is applied deterministically AND recorded."""

from app.analysis.cleanup import clean
from app.analysis.schema import ArticleAnalysis

PARAGRAPHS = [
    "Ambassador Martin visited Agadir on Monday, HESPRESS English reports.",
    "Martin met officials in Agadir. Nearly four decades of cooperation were celebrated.",
]
Q1 = {"paragraph": 1, "quote": "Ambassador Martin visited Agadir on Monday"}
Q2 = {"paragraph": 2, "quote": "Nearly four decades of cooperation"}


def actor(name: str, full: str) -> dict[str, object]:
    return {"name_as_written": name, "full_name": full, "type": "person", "role": "visited",
            "involvement": "central", "evidence": Q1}  # fmt: skip


def place(name: str) -> dict[str, object]:
    return {"name_as_written": name, "role": "event_location", "evidence": Q1}


def analysis(**overrides: object) -> ArticleAnalysis:
    base: dict[str, object] = {
        "overview": {"headline": "h", "summary": "s", "report_type": "new_event",
                     "event_status": "completed"},
        "what": {"main_event": "visit"},
        "who": [actor("Martin", "Martin")],
        "when": {"event_date": {"value": "2026-11-23", "evidence": Q1}},
        "where": [place("Agadir")],
        "claims": [], "how": None, "topics": [],
        "tone": {"label": "neutral", "explanation": "x"},
    }  # fmt: skip
    base.update(overrides)
    return ArticleAnalysis.model_validate(base)


def run(a: ArticleAnalysis) -> tuple[ArticleAnalysis, list[str]]:
    cleaned, report = clean(a, PARAGRAPHS, publisher_names=("Hespress English", "Hespress"))
    return cleaned, report.corrections


def test_a_clean_analysis_is_left_untouched() -> None:
    original = analysis()

    cleaned, corrections = run(original)

    assert cleaned == original
    assert corrections == []


def test_duplicates_and_the_publisher_are_removed() -> None:
    cleaned, corrections = run(
        analysis(
            who=[
                actor("Martin", "Martin"),
                actor("MARTIN", "Martin"),
                actor("HESPRESS English", "HESPRESS English"),
            ],
            where=[place("Agadir"), place("agadir"), place("HESPRESS English")],
        )
    )

    assert [a.name_as_written for a in cleaned.who] == ["Martin"]
    assert [p.name_as_written for p in cleaned.where] == ["Agadir"]
    assert len(corrections) == 4


def test_names_not_in_the_article_are_corrected_or_dropped() -> None:
    cleaned, corrections = run(
        analysis(
            who=[actor("Martin", "Philippe Martin")],  # expanded beyond the article
            where=[place("Agadir"), place("Western Region")],  # invented place name
        )
    )

    assert cleaned.who[0].full_name == "Martin"
    assert [p.name_as_written for p in cleaned.where] == ["Agadir"]
    assert any("not in article" in c for c in corrections)


def test_undated_event_dates_and_publication_entries_are_dropped() -> None:
    cleaned, corrections = run(
        analysis(
            when={
                "event_date": {"value": "2026-11-23", "evidence": Q2},  # "four decades"
                "timeline": [
                    {"date": "2026-11-23", "event": "Publication of the article.", "evidence": Q1},
                    {"date": "2026-11-23", "event": "Visit to Agadir.", "evidence": Q1},
                ],
            }
        )
    )

    assert cleaned.when.event_date.value is None
    assert cleaned.when.event_date.precision == "unknown"
    assert [t.event for t in cleaned.when.timeline] == ["Visit to Agadir."]
    assert len(corrections) == 2


def test_real_events_named_publish_are_kept_and_listed_provinces_survive() -> None:
    """Both were wrongly removed in the first version (found reviewing lab run v6)."""
    paragraphs = [
        "Tests will run in Taourirt, Khenifra and Ifrane provinces on Monday.",
        "The CESE published its opinion on Thursday.",
    ]
    q1 = {"paragraph": 1, "quote": "Taourirt, Khenifra and Ifrane provinces on Monday"}
    q2 = {"paragraph": 2, "quote": "The CESE published its opinion on Thursday"}
    original = analysis(
        who=[],
        where=[{"name_as_written": "Taourirt province", "role": "event_location", "evidence": q1}],
        when={
            "event_date": {"value": "2026-11-23", "evidence": q1},
            "timeline": [
                {"date": "2026-11-19", "event": "The CESE published its opinion.", "evidence": q2}
            ],
        },
    )

    cleaned, report = clean(original, paragraphs, publisher_names=("Hespress",))

    assert [p.name_as_written for p in cleaned.where] == ["Taourirt"]
    assert len(cleaned.when.timeline) == 1
    assert report.corrections == ["place name 'Taourirt province' kept as written: 'Taourirt'"]
