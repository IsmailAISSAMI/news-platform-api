"""Persisting gate decisions. Idempotent: re-ingesting the same item is a no-op."""

from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.policy import GateDecision
from app.models import Article
from app.models.enums import DiscoveryChannel


class SaveOutcome(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


async def save_article(
    session: AsyncSession,
    *,
    source_id: int,
    decision: GateDecision,
    channel: DiscoveryChannel,
    insert_only: bool = False,
) -> SaveOutcome:
    """Insert, or update when the publisher edited the article.

    `insert_only` is for thin channels (the news sitemap has no author or categories):
    they may add a missing article but must never overwrite a richer RSS-sourced row.
    """
    if decision.body_text is not None:
        # Fail loudly rather than silently drop licensed content: there is no
        # article_bodies table yet, and no source is licensed.
        msg = "storing licensed bodies is not implemented (article_bodies)"
        raise NotImplementedError(msg)

    record = decision.record
    existing = await session.scalar(
        select(Article).where(
            Article.source_id == source_id, Article.external_id == record.external_id
        )
    )
    if existing is None:
        session.add(
            Article(
                source_id=source_id,
                external_id=record.external_id,
                canonical_url=record.canonical_url,
                title=record.title,
                excerpt=record.excerpt,
                author=record.author,
                categories=list(record.categories),
                language=record.language,
                published_at=record.published_at,
                modified_at=record.modified_at,
                first_seen_via=channel,
                content_hash=record.content_hash,
            )
        )
        await session.flush()
        return SaveOutcome.NEW

    if insert_only:
        return SaveOutcome.UNCHANGED

    unchanged = (
        existing.content_hash == record.content_hash
        and existing.canonical_url == record.canonical_url
        and existing.published_at == record.published_at
    )
    if unchanged:
        return SaveOutcome.UNCHANGED

    existing.canonical_url = record.canonical_url
    existing.title = record.title
    existing.excerpt = record.excerpt
    existing.author = record.author
    existing.categories = list(record.categories)
    existing.language = record.language
    existing.published_at = record.published_at
    existing.modified_at = record.modified_at
    existing.content_hash = record.content_hash
    await session.flush()
    return SaveOutcome.UPDATED
