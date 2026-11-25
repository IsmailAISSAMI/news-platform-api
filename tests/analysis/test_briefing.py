"""make_briefing: no evidence anywhere, unverified items removed (synthetic data only)."""

import copy
from typing import Any

from app.analysis.briefing import make_briefing


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


def test_empty_list_and_out_of_range_paths_are_harmless() -> None:
    data = cleaned()
    data["who"] = []
    briefing = make_briefing(data, ["$.who[0].evidence", "$.claims[7].evidence"])
    assert briefing["who"] == []
    assert briefing["claims"] == []
    assert briefing["removed_items"] == 0
    assert briefing["review_required"] is True


def test_an_item_inside_a_removed_item_is_not_counted_twice() -> None:
    data = cleaned()
    data["who"][0]["nested"] = [{"evidence": ev()}]
    briefing = make_briefing(data, ["$.who[0].nested[0].evidence", "$.who[0].evidence"])
    assert [a["full_name"] for a in briefing["who"]] == ["Actor 1", "Actor 2", "Actor 3", "Actor 4"]
    assert briefing["removed_items"] == 1


def test_duplicate_and_malformed_paths() -> None:
    briefing = make_briefing(cleaned(), ["$.who[2].evidence", "$.who[2].evidence", "nonsense"])
    assert len(briefing["who"]) == 4
    assert briefing["removed_items"] == 1
