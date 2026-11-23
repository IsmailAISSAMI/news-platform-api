from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import FeedKind, FeedState, check_in


class Feed(Base):
    """An input channel of a source, with its HTTP cache validators and walk checkpoint."""

    __tablename__ = "feeds"
    __table_args__ = (
        CheckConstraint(check_in("kind", FeedKind), name="kind"),
        CheckConstraint(check_in("state", FeedState), name="state"),
        CheckConstraint("consecutive_failures >= 0", name="consecutive_failures_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    url: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)

    # Conditional GET validators from the last 200 response.
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)

    # Newest item seen by the last *completed* walk. Moves forward only on completion.
    checkpoint_external_id: Mapped[str | None] = mapped_column(Text)
    checkpoint_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    consecutive_failures: Mapped[int] = mapped_column(server_default=text("0"))
    state: Mapped[str] = mapped_column(Text, server_default=text(f"'{FeedState.ACTIVE}'"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
