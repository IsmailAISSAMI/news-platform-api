"""The rights gate: the single place that decides what may be stored per source.

Every source adapter returns a `FetchedItem`, which may carry the publisher's full body.
Only `apply_policy` turns it into something persistable. Its output types make the
rules structural:

- `ArticleRecord` (what goes into `articles`) has **no body field at all**.
- `GateDecision.body_text` is non-None only under `licensed_full_text` (valid, unexpired
  license) or `restricted` (a recorded `rights_basis`). Bodies are stored privately in
  `article_bodies` and never served.

See docs/architecture.md §5 for the legal reasoning behind each policy.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.ingestion.normalize import (
    collapse_whitespace,
    html_to_paragraphs,
    html_to_text,
    truncate,
)
from app.models.enums import RightsPolicy
from app.models.source import Source


class PolicyError(Exception):
    """The source's policy forbids ingesting this item."""


class InvalidItemError(Exception):
    """The item lacks data required for storage (e.g. no title, naive timestamp)."""


@dataclass(frozen=True, slots=True)
class SourceRights:
    policy: RightsPolicy
    excerpt_max_chars: int
    license_ref: str | None
    license_expires_at: datetime | None
    active: bool
    rights_basis: str | None = None

    @classmethod
    def from_source(cls, source: Source) -> "SourceRights":
        return cls(
            policy=RightsPolicy(source.rights_policy),
            excerpt_max_chars=source.excerpt_max_chars,
            license_ref=source.license_ref,
            license_expires_at=source.license_expires_at,
            active=source.active,
            rights_basis=source.rights_basis,
        )


@dataclass(frozen=True, slots=True)
class FetchedItem:
    """An item as parsed from a feed or page. May contain text we are not allowed to keep."""

    external_id: str
    url: str
    title: str
    published_at: datetime
    language: str
    modified_at: datetime | None = None
    author: str | None = None
    categories: tuple[str, ...] = ()
    summary_html: str | None = None
    body_html: str | None = None


@dataclass(frozen=True, slots=True)
class ArticleRecord:
    """Exactly the fields that may be persisted in `articles`. No body, by construction."""

    external_id: str
    canonical_url: str
    title: str
    excerpt: str | None
    author: str | None
    categories: tuple[str, ...]
    language: str
    published_at: datetime
    modified_at: datetime | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    record: ArticleRecord
    effective_policy: RightsPolicy
    body_text: str | None = None
    downgrade_reason: str | None = None


def effective_policy(rights: SourceRights, now: datetime) -> tuple[RightsPolicy, str | None]:
    """The policy actually applied, and why it differs from the configured one (if it does).

    Fails safe: anything doubtful about a license or a basis downgrades to `metadata_only`.
    """
    if not rights.active:
        return RightsPolicy.DISABLED, "source is inactive"
    if rights.policy is RightsPolicy.LICENSED_FULL_TEXT:
        if not rights.license_ref:
            return RightsPolicy.METADATA_ONLY, "licensed_full_text without a license_ref"
        if rights.license_expires_at is not None and rights.license_expires_at <= now:
            return RightsPolicy.METADATA_ONLY, f"license {rights.license_ref} expired"
    if rights.policy is RightsPolicy.RESTRICTED and not (rights.rights_basis or "").strip():
        return RightsPolicy.METADATA_ONLY, "restricted without a recorded rights_basis"
    return rights.policy, None


def may_fetch(rights: SourceRights, now: datetime) -> bool:
    policy, _ = effective_policy(rights, now)
    return policy is not RightsPolicy.DISABLED


def apply_policy(item: FetchedItem, rights: SourceRights, now: datetime) -> GateDecision:
    """Reduce a fetched item to what this source's rights allow us to store.

    Raises `PolicyError` for disabled sources and `InvalidItemError` for unusable items.
    """
    policy, reason = effective_policy(rights, now)
    if policy is RightsPolicy.DISABLED:
        msg = f"ingestion forbidden: {reason or 'source policy is disabled'}"
        raise PolicyError(msg)

    record = _build_record(item, excerpt_max_chars=rights.excerpt_max_chars)

    body_text: str | None = None
    if policy in {RightsPolicy.LICENSED_FULL_TEXT, RightsPolicy.RESTRICTED}:
        body_text = html_to_paragraphs(item.body_html) or None
    # metadata_only: the body is dropped here and never leaves this function.

    return GateDecision(
        record=record, effective_policy=policy, body_text=body_text, downgrade_reason=reason
    )


def _build_record(item: FetchedItem, *, excerpt_max_chars: int) -> ArticleRecord:
    title = html_to_text(item.title)
    if not title:
        msg = f"item {item.external_id!r} has no title"
        raise InvalidItemError(msg)

    external_id = item.external_id.strip()
    if not external_id:
        msg = "item has an empty external_id"
        raise InvalidItemError(msg)

    excerpt = truncate(html_to_text(item.summary_html), excerpt_max_chars)
    author = (collapse_whitespace(item.author) if item.author else "") or None
    categories = _clean_categories(item.categories)
    modified_at = (
        _as_utc(item.modified_at, "modified_at", external_id) if item.modified_at else None
    )

    return ArticleRecord(
        external_id=external_id,
        canonical_url=item.url.strip(),
        title=title,
        excerpt=excerpt,
        author=author,
        categories=categories,
        language=item.language.strip().lower(),
        published_at=_as_utc(item.published_at, "published_at", external_id),
        modified_at=modified_at,
        content_hash=_content_hash(title, excerpt, author, categories),
    )


def _clean_categories(categories: tuple[str, ...]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for raw in categories:
        name = html_to_text(raw)
        if name and name.casefold() not in {key.casefold() for key in seen}:
            seen[name] = None
    return tuple(seen)


def _as_utc(value: datetime, field: str, external_id: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"item {external_id!r}: {field} must be timezone-aware"
        raise InvalidItemError(msg)
    return value.astimezone(UTC)


def _content_hash(
    title: str, excerpt: str | None, author: str | None, categories: tuple[str, ...]
) -> str:
    """Hash of the editorial fields; a change means the publisher edited the article.

    `modified_at` is excluded: a bumped timestamp without content changes is not an edit.
    """
    editorial = {
        "title": title,
        "excerpt": excerpt,
        "author": author,
        "categories": list(categories),
    }
    payload = json.dumps(editorial, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
