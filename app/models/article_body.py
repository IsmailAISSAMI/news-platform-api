from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ArticleBody(Base):
    """Full article text, stored privately for internal analysis.

    Written only when the source's rights policy allows it (`restricted` with a recorded
    basis, or `licensed_full_text`). Never served by the API: the `news_api` role cannot
    even read this table (scripts/db_roles.sql).
    """

    __tablename__ = "article_bodies"

    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    body_text: Mapped[str] = mapped_column(Text)
    body_hash: Mapped[str] = mapped_column(Text)  # detects publisher edits to the text
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
