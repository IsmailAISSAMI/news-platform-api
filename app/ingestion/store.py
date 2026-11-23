"""Persisting gate decisions. Idempotent: re-ingesting the same item is a no-op."""

import hashlib
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.policy import ArticleRecord, GateDecision
from app.models import Article, ArticleBody
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

    The body, when the rights gate released one, is stored privately in `article_bodies`.
    Adding or changing a body on an existing article counts as an update.
    """
    record = decision.record
    article = await session.scalar(
        select(Article).where(
            Article.source_id == source_id, Article.external_id == record.external_id
        )
    )
    if article is None:
        article = _new_article(source_id, record, channel)
        session.add(article)
        await session.flush()
        await _save_body(session, article.id, decision.body_text)
        return SaveOutcome.NEW

    if insert_only:
        return SaveOutcome.UNCHANGED

    metadata_changed = not (
        article.content_hash == record.content_hash
        and article.canonical_url == record.canonical_url
        and article.published_at == record.published_at
    )
    if metadata_changed:
        _apply(article, record)
    body_changed = await _save_body(session, article.id, decision.body_text)
    await session.flush()
    return SaveOutcome.UPDATED if metadata_changed or body_changed else SaveOutcome.UNCHANGED


def _new_article(source_id: int, record: ArticleRecord, channel: DiscoveryChannel) -> Article:
    return Article(
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


def _apply(article: Article, record: ArticleRecord) -> None:
    article.canonical_url = record.canonical_url
    article.title = record.title
    article.excerpt = record.excerpt
    article.author = record.author
    article.categories = list(record.categories)
    article.language = record.language
    article.published_at = record.published_at
    article.modified_at = record.modified_at
    article.content_hash = record.content_hash


async def _save_body(session: AsyncSession, article_id: int, body_text: str | None) -> bool:
    """Insert or update the private body. Returns True when something was written."""
    if body_text is None:
        return False
    body_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    existing = await session.get(ArticleBody, article_id)
    if existing is None:
        session.add(ArticleBody(article_id=article_id, body_text=body_text, body_hash=body_hash))
        await session.flush()
        return True
    if existing.body_hash == body_hash:
        return False
    existing.body_text = body_text
    existing.body_hash = body_hash
    await session.flush()
    return True
