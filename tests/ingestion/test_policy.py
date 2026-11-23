"""The rights gate. Fixtures are synthetic: never real publisher text (public repo)."""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.ingestion.policy import (
    ArticleRecord,
    FetchedItem,
    InvalidItemError,
    PolicyError,
    SourceRights,
    apply_policy,
    effective_policy,
    may_fetch,
)
from app.models.enums import RightsPolicy
from app.models.source import Source

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
BODY = "<p>Synthetic full body paragraph one.</p><p>Paragraph two.</p>"


def rights(policy: RightsPolicy = RightsPolicy.METADATA_ONLY, **overrides: object) -> SourceRights:
    fields: dict[str, object] = {
        "policy": policy,
        "excerpt_max_chars": 0,
        "license_ref": None,
        "license_expires_at": None,
        "active": True,
    }
    fields.update(overrides)
    return SourceRights(**fields)  # type: ignore[arg-type]  # test helper with overrides


def item(**overrides: object) -> FetchedItem:
    fields: dict[str, object] = {
        "external_id": "149695",
        "url": "https://example.test/149695-placeholder.html",
        "title": "Placeholder headline &amp; more",
        "published_at": datetime(2026, 11, 23, 13, 30, tzinfo=timezone(timedelta(hours=1))),
        "language": "en-US",
        "author": "  Desk   Name ",
        "categories": ("Economy", "economy", " Trade ", ""),
        "summary_html": "<p>A synthetic summary sentence used only for testing purposes.</p>",
        "body_html": BODY,
    }
    fields.update(overrides)
    return FetchedItem(**fields)  # type: ignore[arg-type]  # test helper with overrides


LICENSED = rights(RightsPolicy.LICENSED_FULL_TEXT, license_ref="LIC-1")


def test_source_rights_are_read_from_the_source_model() -> None:
    source = Source(
        slug="s",
        name="S",
        site_url="https://example.test",
        language="en",
        rights_policy="metadata_only",
        excerpt_max_chars=0,
        license_ref=None,
        license_expires_at=None,
        active=True,
    )

    assert SourceRights.from_source(source) == rights()


# --- Structural guarantees ---------------------------------------------------------


def test_storable_record_has_no_field_that_could_hold_a_body() -> None:
    names = {field.name for field in dataclasses.fields(ArticleRecord)}

    assert not names & {"body", "body_html", "body_text", "content", "summary_html"}


@pytest.mark.parametrize(
    "policy", [RightsPolicy.METADATA_ONLY, RightsPolicy.RESTRICTED], ids=lambda p: p.value
)
def test_body_is_dropped_for_every_non_licensed_policy(policy: RightsPolicy) -> None:
    decision = apply_policy(item(), rights(policy, excerpt_max_chars=500), NOW)

    assert decision.body_text is None
    assert "Paragraph two" not in repr(decision)


# --- Policies ----------------------------------------------------------------------


def test_metadata_only_with_zero_excerpt_stores_metadata_only() -> None:
    decision = apply_policy(item(), rights(), NOW)
    record = decision.record

    assert decision.effective_policy is RightsPolicy.METADATA_ONLY
    assert record.excerpt is None
    assert record.title == "Placeholder headline & more"
    assert record.author == "Desk Name"
    assert record.categories == ("Economy", "Trade")
    assert record.language == "en-us"
    assert record.published_at == datetime(2026, 11, 23, 12, 30, tzinfo=UTC)
    assert record.published_at.tzinfo is UTC


def test_metadata_only_excerpt_is_plain_text_and_bounded() -> None:
    decision = apply_policy(item(), rights(excerpt_max_chars=30), NOW)
    excerpt = decision.record.excerpt

    assert excerpt is not None
    assert len(excerpt) <= 30
    assert "<" not in excerpt


def test_disabled_source_is_refused() -> None:
    with pytest.raises(PolicyError, match="forbidden"):
        apply_policy(item(), rights(RightsPolicy.DISABLED), NOW)
    assert not may_fetch(rights(RightsPolicy.DISABLED), NOW)


def test_inactive_source_is_treated_as_disabled() -> None:
    inactive = rights(active=False)

    assert effective_policy(inactive, NOW) == (RightsPolicy.DISABLED, "source is inactive")
    assert not may_fetch(inactive, NOW)
    with pytest.raises(PolicyError, match="inactive"):
        apply_policy(item(), inactive, NOW)


def test_restricted_stores_like_metadata_only() -> None:
    restricted = apply_policy(item(), rights(RightsPolicy.RESTRICTED), NOW)
    metadata = apply_policy(item(), rights(), NOW)

    assert restricted.record == metadata.record
    assert restricted.effective_policy is RightsPolicy.RESTRICTED


def test_licensed_full_text_keeps_the_body_as_plain_text() -> None:
    decision = apply_policy(item(), LICENSED, NOW)

    assert decision.effective_policy is RightsPolicy.LICENSED_FULL_TEXT
    assert decision.body_text == "Synthetic full body paragraph one. Paragraph two."
    assert decision.downgrade_reason is None


def test_licensed_with_empty_body_stores_no_body() -> None:
    assert apply_policy(item(body_html="  "), LICENSED, NOW).body_text is None


# --- Fail-safe license handling ------------------------------------------------------


def test_expired_license_downgrades_to_metadata_only() -> None:
    expired = rights(RightsPolicy.LICENSED_FULL_TEXT, license_ref="LIC-1", license_expires_at=NOW)

    decision = apply_policy(item(), expired, NOW)

    assert decision.effective_policy is RightsPolicy.METADATA_ONLY
    assert decision.body_text is None
    assert decision.downgrade_reason == "license LIC-1 expired"


def test_unexpired_license_keeps_full_text() -> None:
    valid = rights(
        RightsPolicy.LICENSED_FULL_TEXT,
        license_ref="LIC-1",
        license_expires_at=NOW + timedelta(days=1),
    )

    assert apply_policy(item(), valid, NOW).body_text is not None


def test_licensed_without_license_ref_downgrades() -> None:
    misconfigured = rights(RightsPolicy.LICENSED_FULL_TEXT, license_ref=None)

    decision = apply_policy(item(), misconfigured, NOW)

    assert decision.effective_policy is RightsPolicy.METADATA_ONLY
    assert decision.body_text is None


# --- Invalid items -------------------------------------------------------------------


@pytest.mark.parametrize("title", ["", "   ", "<p> </p>"])
def test_item_without_title_is_rejected(title: str) -> None:
    with pytest.raises(InvalidItemError, match="no title"):
        apply_policy(item(title=title), rights(), NOW)


def test_item_without_external_id_is_rejected() -> None:
    with pytest.raises(InvalidItemError, match="external_id"):
        apply_policy(item(external_id="  "), rights(), NOW)


@pytest.mark.parametrize("field", ["published_at", "modified_at"])
def test_naive_timestamps_are_rejected(field: str) -> None:
    with pytest.raises(InvalidItemError, match="timezone-aware"):
        apply_policy(item(**{field: datetime(2026, 11, 23, 12, 0)}), rights(), NOW)


# --- Edit detection ------------------------------------------------------------------


def test_content_hash_is_stable_across_runs() -> None:
    first = apply_policy(item(), rights(), NOW).record.content_hash
    second = apply_policy(item(), rights(), NOW).record.content_hash

    assert first == second


def test_content_hash_changes_when_the_title_is_edited() -> None:
    original = apply_policy(item(), rights(), NOW).record.content_hash
    edited = apply_policy(item(title="Corrected headline"), rights(), NOW).record.content_hash

    assert original != edited


def test_content_hash_ignores_a_bumped_modified_timestamp() -> None:
    base = apply_policy(item(), rights(), NOW).record.content_hash
    bumped = apply_policy(item(modified_at=NOW), rights(), NOW).record.content_hash

    assert base == bumped


def test_content_hash_ignores_the_body() -> None:
    with_body = apply_policy(item(), LICENSED, NOW).record.content_hash
    other_body = apply_policy(item(body_html="<p>Other</p>"), LICENSED, NOW).record.content_hash

    assert with_body == other_body
