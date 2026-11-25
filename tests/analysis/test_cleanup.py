"""Code-side cleanup: every correction is applied deterministically AND recorded."""

from datetime import date

from app.analysis.cleanup import Correction, clean, upcoming
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
        "overview": {"headline": "h", "report_type": "new_event",
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
    assert any("not in the article" in c for c in corrections)


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
    assert report.items == [
        Correction(
            path="$.where[0]",
            original="Taourirt province",
            corrected="Taourirt",
            reason="generic word added to a name the article lists",
            method="heuristic",
        )
    ]


def test_every_correction_keeps_the_original_and_its_method() -> None:
    _, corrections_report = clean(
        analysis(where=[place("Agadir"), place("agadir")]),
        PARAGRAPHS,
        publisher_names=("Hespress",),
    )

    [item] = corrections_report.items
    assert (item.original, item.corrected, item.method) == ("agadir", None, "deterministic")


def test_upcoming_is_derived_from_predictions_and_future_timeline_dates() -> None:
    q = {"paragraph": 1, "quote": "Ambassador Martin visited Agadir on Monday"}
    a = analysis(
        claims=[
            {"text": "Referral expected soon", "kind": "prediction", "attributed_to": None,
             "evidence": q},
            {"text": "A visit happened", "kind": "fact", "attributed_to": None, "evidence": q},
        ],
        when={
            "event_date": {"value": "2026-11-23", "evidence": q},
            "timeline": [
                {"date": "2026-11-20", "event": "Earlier step", "evidence": q},
                {"date": "2026-11-24", "event": "Tests scheduled", "evidence": q},
                {"date": "2026-12", "event": "Service starts", "evidence": q},
                {"date": "2026-11", "event": "Same month, ambiguous", "evidence": q},
            ],
        },
    )  # fmt: skip

    assert upcoming(a, date(2026, 11, 23)) == [
        "Referral expected soon",
        "2026-11-24: Tests scheduled",
        "2026-12: Service starts",
    ]


# --- v8 rules: tense flag, places as actors, near-duplicates -----------------------------

V8_PARAGRAPHS = [
    "A key part of the visit will be the signing of a loan agreement on Monday.",
    "A separate deal was signed in Rabat on Nov. 3.",
    "The investigating judge at the Tangier Court of Appeal questioned the mayor. "
    "The investigating judge is expected to refer the case. He was arrested at "
    "Tangier Ibn Battouta Airport.",
    "Jensen Huang returned. Alice Huang denied it. Huang thanked staff.",
]
FUTURE_Q = {"paragraph": 1, "quote": "will be the signing of a loan agreement"}
PAST_Q = {"paragraph": 2, "quote": "A separate deal was signed in Rabat"}
JUDGE_Q = {"paragraph": 3, "quote": "The investigating judge is expected to refer the case"}
HUANG_Q = {"paragraph": 4, "quote": "Jensen Huang returned"}


def claim(text: str, quote: dict[str, object], kind: str = "fact") -> dict[str, object]:
    return {"text": text, "kind": kind, "attributed_to": None, "evidence": quote}


def person(name: str, quote: dict[str, object]) -> dict[str, object]:
    return {"name_as_written": name, "full_name": name, "type": "person", "role": "r",
            "involvement": "central", "evidence": quote}  # fmt: skip


def v8(**overrides: object) -> tuple[ArticleAnalysis, list[str], list[str]]:
    base: dict[str, object] = {
        "who": [], "where": [], "claims": [],
        "when": {"event_date": {"value": None, "evidence": None}},
    }  # fmt: skip
    base.update(overrides)
    cleaned, report = clean(analysis(**base), V8_PARAGRAPHS, publisher_names=("Hespress",))
    return cleaned, [i.reason for i in report.items], [f.text for f in report.flags]


def test_future_quote_with_a_past_claim_is_flagged_not_rewritten() -> None:
    cleaned, _, flags = v8(
        claims=[
            claim("The loan agreement was signed.", FUTURE_Q),  # wrong tense: flagged
            claim("The loan agreement will be signed.", FUTURE_Q),  # right tense
            claim("A separate deal was signed in Rabat.", PAST_Q),  # past quote, past claim
        ]
    )

    assert flags == ["The loan agreement was signed."]
    assert len(cleaned.claims) == 3  # flagged, never silently changed or deleted


def test_a_name_both_actor_and_place_is_flagged_not_deleted() -> None:
    airport = {"paragraph": 3, "quote": "arrested at Tangier Ibn Battouta Airport"}
    cleaned, reasons, flags = v8(
        who=[person("the investigating judge", JUDGE_Q),
             person("Tangier Ibn Battouta Airport", airport)],
        where=[{"name_as_written": "Tangier Ibn Battouta Airport", "role": "related_event",
                "evidence": airport}],
    )  # fmt: skip

    assert len(cleaned.who) == 2  # kept: code cannot tell which side is wrong
    assert reasons == []
    assert flags == ["Tangier Ibn Battouta Airport"]


def test_near_duplicate_actors_are_merged() -> None:
    cleaned, reasons, _ = v8(
        who=[person("investigating judge at the Tangier Court of Appeal", JUDGE_Q),
             person("The investigating judge", JUDGE_Q)],
    )  # fmt: skip

    assert len(cleaned.who) == 1
    assert reasons[0].startswith("near-duplicate of")


def test_an_ambiguous_short_name_is_never_merged() -> None:
    """'Huang' could be Jensen Huang or Alice Huang: keep all three."""
    cleaned, reasons, _ = v8(
        who=[person("Huang", HUANG_Q), person("Jensen Huang", HUANG_Q),
             person("Alice Huang", HUANG_Q)],
    )  # fmt: skip

    assert [a.full_name for a in cleaned.who] == ["Huang", "Jensen Huang", "Alice Huang"]
    assert reasons == []


def test_an_unambiguous_short_name_is_merged() -> None:
    cleaned, _, _ = v8(who=[person("Jensen Huang", HUANG_Q), person("Huang", HUANG_Q)])

    assert [a.full_name for a in cleaned.who] == ["Jensen Huang"]


def test_weak_modals_do_not_raise_tense_flags() -> None:
    """'could'/'would' express possibility: they caused false alarms on real articles."""
    paragraphs = ["Exemptions do not automatically raise exports, which could fall."]
    quote = {"paragraph": 1, "quote": "Exemptions do not automatically raise exports"}
    full = {"paragraph": 1, "quote": "do not automatically raise exports, which could fall"}
    _, report = clean(
        analysis(
            who=[],
            where=[],
            when={"event_date": {"value": None, "evidence": None}},
            claims=[
                claim("Exemptions do not raise exports.", quote),
                claim("Exemptions do not raise exports.", full),
            ],
        ),
        paragraphs,
        publisher_names=("Hespress",),
    )

    assert report.flags == []


# --- O1, O2, O3: flagged or removed, always recorded ---------------------------------------


def test_anonymous_source_claim_without_attribution_is_flagged_not_changed() -> None:
    paragraphs = ["Sources said Martin visited Agadir on Monday.", PARAGRAPHS[1]]
    quote = {"paragraph": 1, "quote": "Martin visited Agadir on Monday"}
    claims = [
        {"text": "Martin visited", "kind": "fact", "attributed_to": None, "evidence": quote},
        {"text": "Martin visited", "kind": "fact", "attributed_to": "sources", "evidence": quote},
    ]
    original = analysis(claims=claims, who=[], where=[],
                        when={"event_date": {"value": None, "evidence": None}})  # fmt: skip

    cleaned, report = clean(original, paragraphs, publisher_names=("Hespress",))

    assert cleaned == original
    assert [(f.path, f.reason) for f in report.flags] == [
        ("$.claims[0].attributed_to", "claim from an anonymous source has no attribution")
    ]


def test_timeline_entry_with_the_publication_date_is_dropped_only_when_told_the_date() -> None:
    timeline = [
        {"date": "2026-11-23", "event": "Visit", "evidence": Q2},  # no date words
        {"date": "2026-11-23", "event": "Visit", "evidence": Q1},  # "on Monday"
    ]
    original = analysis(when={"event_date": {"value": "2026-11-23", "evidence": Q1},
                              "timeline": timeline})  # fmt: skip

    unchanged, _ = clean(original, PARAGRAPHS, publisher_names=("Hespress",))
    cleaned, report = clean(
        original, PARAGRAPHS, publisher_names=("Hespress",), published=date(2026, 11, 23)
    )

    assert unchanged == original  # default: behaviour as before
    assert len(cleaned.when.timeline) == 1
    assert cleaned.when.timeline[0].evidence.quote == Q1["quote"]
    assert [(c.path, c.corrected, c.reason, c.method) for c in report.items] == [
        ("$.when.timeline[0]", None, "dated with the publication date, no date in the quote",
         "heuristic")
    ]  # fmt: skip


def test_uninformative_causes_are_flagged() -> None:
    why = [
        {"text": "visit", "kind": "stated", "attributed_to": None, "evidence": Q1},
        {"text": "Four decades of cooperation between both sides", "kind": "stated",
         "attributed_to": None, "evidence": Q2},
    ]  # fmt: skip
    cleaned, report = clean(analysis(why=why), PARAGRAPHS, publisher_names=("Hespress",))

    assert len(cleaned.why) == 2  # never removed
    assert [(f.path, f.reason) for f in report.flags] == [("$.why[0]", "uninformative cause")]


def test_the_publisher_is_matched_exactly_never_by_substring() -> None:
    paragraphs = [
        "Ambassador Martin visited Agadir on Monday, HESPRESS English reports.",
        "The Hespress English Readers Club and the Press Office answered questions.",
    ]
    original = analysis(
        who=[
            actor("Martin", "Martin"),
            actor("Hespress English Readers Club", "Hespress English Readers Club"),
            actor("Press Office", "Press Office"),  # "press" is inside "Hespress"
            actor("the Hespress", "the Hespress"),
        ]
    )
    cleaned, report = clean(original, paragraphs, publisher_names=("Hespress English", "Hespress"))
    assert [a.name_as_written for a in cleaned.who] == [
        "Martin",
        "Hespress English Readers Club",
        "Press Office",
    ]
    assert [c.reason for c in report.items] == ["the publisher itself"]


def test_an_attribution_the_article_never_names_is_flagged_not_changed() -> None:
    quote = {"paragraph": 1, "quote": "Ambassador Martin visited Agadir on Monday"}
    claims = [
        {"text": "Visit held", "kind": "fact", "attributed_to": "Martin", "evidence": quote},
        {"text": "Visit held", "kind": "fact", "attributed_to": "Leila Haddad", "evidence": quote},
        {"text": "Visit held", "kind": "fact", "attributed_to": "unnamed officials",
         "evidence": quote},
    ]  # fmt: skip
    original = analysis(claims=claims)
    cleaned, report = clean(original, PARAGRAPHS, publisher_names=("Hespress",))
    assert cleaned == original
    assert [(f.path, f.reason) for f in report.flags] == [
        ("$.claims[1].attributed_to", "attribution not named in the article")
    ]
