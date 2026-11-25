"""Rights-gate matrix: every policy x rights_basis x license state. Synthetic data only."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.ingestion.policy import FetchedItem, PolicyError, SourceRights, apply_policy
from app.models.enums import RightsPolicy

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
ITEM = FetchedItem(
    external_id="1",
    url="https://example.test/1-placeholder.html",
    title="Placeholder headline",
    published_at=NOW,
    language="en",
    body_html="<p>Synthetic body paragraph.</p>",
)
BASES: tuple[str | None, ...] = (None, "", " ", "x")
LICENSES: dict[str, tuple[str | None, datetime | None]] = {
    "none": (None, None),
    "valid": ("LIC-1", NOW + timedelta(days=1)),
    "expired": ("LIC-1", NOW - timedelta(days=1)),
}


@pytest.mark.parametrize(
    ("policy", "basis", "license_state"),
    list(itertools.product(RightsPolicy, BASES, LICENSES)),
)
def test_body_only_leaves_the_gate_when_allowed(
    policy: RightsPolicy, basis: str | None, license_state: str
) -> None:
    license_ref, expires = LICENSES[license_state]
    rights = SourceRights(
        policy=policy,
        excerpt_max_chars=0,
        license_ref=license_ref,
        license_expires_at=expires,
        active=True,
        rights_basis=basis,
    )

    if policy is RightsPolicy.DISABLED:
        with pytest.raises(PolicyError):
            apply_policy(ITEM, rights, NOW)
        return

    decision = apply_policy(ITEM, rights, NOW)

    allowed = (policy is RightsPolicy.RESTRICTED and bool((basis or "").strip())) or (
        policy is RightsPolicy.LICENSED_FULL_TEXT and license_state == "valid"
    )
    assert (decision.body_text is not None) is allowed
    if not allowed:
        assert decision.effective_policy is RightsPolicy.METADATA_ONLY
