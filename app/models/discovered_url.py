from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import BacklogStatus, check_in


class DiscoveredUrl(Base):
    """Archive backlog: article URLs known to exist but not yet resolved to metadata."""

    __tablename__ = "discovered_urls"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint(check_in("status", BacklogStatus), name="status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # The work queue: only pending rows are scanned.
        Index(
            "ix_discovered_urls_pending",
            "source_id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    lastmod: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{BacklogStatus.PENDING}'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"))
