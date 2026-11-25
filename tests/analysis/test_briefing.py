"""make_briefing: no evidence anywhere, unverified items removed (synthetic data only)."""

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from app.analysis.briefing import UnresolvablePathError, make_briefing


def ev(n: int = 1) -> dict[str, Any]:
    return {"paragraph": n, "quote": f"synthetic quote {n}"}


def cleaned() -> dict[str, Any]:
    return {
        "overview": {"headline": "h", "report_type": "new_event", "event_status": "completed"},
        "what": {"main_event": "m", "secondary_events": []},
        "who": [
            {"full_name": f"Actor {i}", "role": "r", "involvement": "central", "evidence": ev(i)}
            for i in range(5)
        ],
        "when": {
            "event_date": {"value": "2026-11-23", "evidence": ev()},
            "timeline": [{"date": "2026-11-01", "event": "e", "evidence": ev(2)}],
        },
        "where": [{"name_as_written": "Rabat", "role": "event_location", "evidence": ev()}],
        "claims": [],
        "how": None,
        "topics": ["t"],
        "tone": {"label": "neutral", "explanation": "x"},
    }


def keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys(v)}
    if isinstance(node, list):
        return {k for item in node for k in keys(item)}
    return set()


def test_every_evidence_key_is_removed_at_any_depth() -> None:
    briefing = make_briefing(cleaned(), [])
    assert "evidence" not in keys(briefing)
    assert "quote" not in keys(briefing)
    assert briefing["when"]["timeline"] == [{"date": "2026-11-01", "event": "e"}]
    assert briefing["when"]["event_date"] == {"value": "2026-11-23"}
    assert (briefing["review_required"], briefing["removed_items"]) == (False, 0)


def test_input_is_not_modified() -> None:
    data = cleaned()
    before = copy.deepcopy(data)
    make_briefing(data, ["$.who[0].evidence"])
    assert data == before


def test_several_items_of_one_list_are_removed_by_their_original_index() -> None:
    briefing = make_briefing(cleaned(), ["$.who[1].evidence", "$.who[3].evidence", "$.who[4]"])
    assert [a["full_name"] for a in briefing["who"]] == ["Actor 0", "Actor 2"]
    assert (briefing["review_required"], briefing["removed_items"]) == (True, 3)


def test_nested_list_item_is_removed() -> None:
    briefing = make_briefing(cleaned(), ["$.when.timeline[0].evidence"])
    assert briefing["when"]["timeline"] == []
    assert briefing["when"]["event_date"]["value"] == "2026-11-23"
    assert briefing["removed_items"] == 1


def test_unverified_event_date_keeps_the_field_but_nulls_the_date() -> None:
    briefing = make_briefing(cleaned(), ["$.when.event_date.evidence"])
    assert briefing["when"]["event_date"] == {"value": None}
    assert len(briefing["when"]["timeline"]) == 1
    assert briefing["review_required"] is True
    assert briefing["removed_items"] == 1


def test_verified_event_date_is_kept() -> None:
    briefing = make_briefing(cleaned(), ["$.where[0].evidence"])
    assert briefing["when"]["event_date"] == {"value": "2026-11-23"}
    assert briefing["where"] == []


def test_an_out_of_range_path_raises_instead_of_serving_the_item() -> None:
    data = cleaned()
    data["who"] = []
    with pytest.raises(UnresolvablePathError):
        make_briefing(data, ["$.who[0].evidence"])
    with pytest.raises(UnresolvablePathError):
        make_briefing(cleaned(), ["$.claims[7].evidence"])


def test_an_item_inside_a_removed_item_is_not_counted_twice() -> None:
    data = cleaned()
    data["who"][0]["nested"] = [{"evidence": ev()}]
    briefing = make_briefing(data, ["$.who[0].nested[0].evidence", "$.who[0].evidence"])
    assert [a["full_name"] for a in briefing["who"]] == ["Actor 1", "Actor 2", "Actor 3", "Actor 4"]
    assert briefing["removed_items"] == 1


def test_duplicate_paths_count_once_and_malformed_paths_raise() -> None:
    briefing = make_briefing(cleaned(), ["$.who[2].evidence", "$.who[2].evidence"])
    assert len(briefing["who"]) == 4
    assert briefing["removed_items"] == 1
    for bad in ("nonsense", "$.who[2]garbage", "$who"):
        with pytest.raises(UnresolvablePathError):
            make_briefing(cleaned(), [bad])


ARTICLE = [
    "The regional council approved the new water plan for twelve rural districts "
    "after a long debate on Tuesday evening.",
    "Farmers said the reservoirs were low.",
]
COPIED = "approved the new water plan for twelve rural districts after a long debate"


def test_a_list_item_sharing_eight_tokens_with_the_article_is_dropped() -> None:
    data = cleaned()
    data["who"][1]["role"] = f"It {COPIED.upper()}."  # case and punctuation do not hide it
    data["what"]["secondary_events"] = ["council vote on water", COPIED]
    briefing = make_briefing(data, [], paragraphs=ARTICLE)
    assert [a["full_name"] for a in briefing["who"]] == ["Actor 0", "Actor 2", "Actor 3", "Actor 4"]
    assert briefing["what"]["secondary_events"] == ["council vote on water"]
    assert set(briefing["copied_paths"]) == {"$.what.secondary_events[1]", "$.who[1].role"}
    assert briefing["removed_items"] == 2


def test_a_copied_scalar_field_is_nulled_and_seven_tokens_are_allowed() -> None:
    data = cleaned()
    data["overview"]["headline"] = COPIED
    data["what"]["main_event"] = "Members approved the new water plan for twelve"  # 7 shared
    data["how"] = "By vote: the regional council approved the new water plan for twelve rural"
    briefing = make_briefing(data, [], paragraphs=ARTICLE)
    assert briefing["overview"]["headline"] is None
    assert briefing["how"] is None
    assert briefing["what"]["main_event"] == data["what"]["main_event"]
    assert set(briefing["copied_paths"]) == {"$.overview.headline", "$.how"}
    assert briefing["removed_items"] == 2


def test_proper_names_are_exempt_from_the_originality_filter() -> None:
    data = cleaned()
    data["who"][0]["full_name"] = "The Regional Council Approved The New Water Plan For Twelve"
    briefing = make_briefing(data, [], paragraphs=ARTICLE)
    assert len(briefing["who"]) == 5
    assert briefing["copied_paths"] == []


def test_fields_without_evidence_are_listed_as_unverified() -> None:
    briefing = make_briefing(cleaned(), [], paragraphs=ARTICLE)
    assert briefing["unverified_fields"] == [
        "$.overview.headline",
        "$.what.main_event",
        "$.topics",
        "$.tone.explanation",
    ]  # empty secondary_events and null `how` are not listed


def test_the_migrations_frozen_copy_matches_the_app() -> None:
    """Revision aa372fadc9eb recomputes stored briefings with a frozen copy of this code."""
    path = next(Path("alembic/versions").glob("*aa372fadc9eb*.py"))
    spec = importlib.util.spec_from_file_location("frozen_aa372fadc9eb", path)
    assert spec is not None
    assert spec.loader is not None
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    data = cleaned()
    data["who"][1]["role"] = COPIED
    data["how"] = COPIED
    paths = ["$.who[3].evidence", "$.when.event_date.evidence"]
    assert frozen._briefing(data, paths, ARTICLE, True) == make_briefing(
        data, paths, paragraphs=ARTICLE, review_required=True
    )
