from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import DiscoveryChannel, check_in

# Weighted so title matches rank above excerpt matches.
SEARCH_TSV_EXPRESSION = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(excerpt, '')), 'B')"
)


class Article(Base):
    """Article metadata. Bodies are never stored here (see the source's rights policy)."""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint(check_in("first_seen_via", DiscoveryChannel), name="first_seen_via"),
        Index("ix_articles_published_at_id", text("published_at DESC"), text("id DESC")),
        Index("ix_articles_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_articles_categories", "categories", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    language: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    first_seen_via: Mapped[str] = mapped_column(Text)
    # Hash of the stored fields; a change means the publisher edited the article.
    content_hash: Mapped[str] = mapped_column(Text)
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed(SEARCH_TSV_EXPRESSION, persisted=True)
    )
