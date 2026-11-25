from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ArticleAnalysis(Base):
    """One AI analysis of one article, for one analyzer version (model + prompt + schema).

    Private: evidence quotes and the raw answer are article text, so the `news_api` role
    cannot read this table (scripts/db_roles.sql), exactly like `article_bodies`.

    Status:
    - `succeeded`: `analysis` (the model's answer, unchanged) and `cleaned` are set, and no
      evidence path is unverified (CHECK in the migration);
    - `needs_review`: answered, but `unverified_paths` lists the JSON paths whose evidence
      was not found in the article: a person must look before it is trusted;
    - `failed`: the last answer was unusable (cut off, invalid); retried automatically
      until `attempts` reaches the pipeline's maximum, then left for `--retry-failed`;
    - `skipped`: never sendable (too long for the free tier's token limit).
    """

    __tablename__ = "article_analyses"
    __table_args__ = (
        # Also serves lookups by article_id alone (leftmost column).
        UniqueConstraint("article_id", "analyzer_version"),
        CheckConstraint(
            "status IN ('succeeded', 'needs_review', 'failed', 'skipped')", name="status_known"
        ),
        CheckConstraint(
            "status <> 'succeeded' OR unverified_paths IS NULL "
            "OR jsonb_array_length(unverified_paths) = 0",
            name="succeeded_fully_verified",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative"),
        CheckConstraint("duration_s >= 0", name="duration_s_non_negative"),
        # The analysis_briefings view (newest briefing per article).
        Index(
            "ix_article_analyses_briefings",
            "article_id",
            text("updated_at DESC"),
            postgresql_where=text(
                "status IN ('succeeded', 'needs_review') AND briefing IS NOT NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE")
    )
    analyzer_version: Mapped[str] = mapped_column(Text)  # e.g. "gemma-4-31b-it|p8|s7"
    model: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    raw_output: Mapped[str | None] = mapped_column(Text)
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cleaned: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    corrections: Mapped[list[Any] | None] = mapped_column(JSONB)
    review_flags: Mapped[list[Any] | None] = mapped_column(JSONB)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # path -> status
    unverified_paths: Mapped[list[Any] | None] = mapped_column(JSONB)  # JSON paths, no evidence
    # Servable part (app/analysis/briefing.py): no evidence, no unverified item. Read by the
    # API only through the `analysis_briefings` view.
    briefing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # One entry per attempt, appended (never overwritten): status, error, finish reason,
    # tokens and time. No raw answer and no article text.
    attempt_history: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
