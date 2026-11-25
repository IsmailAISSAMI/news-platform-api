from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApiCall(Base):
    """One request sent to the Gemini API, recorded before and after it is answered.

    `quota_day` is the Google quota day (Pacific time), computed by the caller and passed
    in. Outcomes:
    - `answered`: Google returned a usable HTTP 200;
    - `refused_busy`: 503, 500, 502, 504 or a timeout (the service was unavailable);
    - `failed`: any other error or an unusable answer;
    - `no_send`: the request was decided against before it was sent.
    """

    __tablename__ = "api_calls"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('answered', 'refused_busy', 'failed', 'no_send')",
            name="outcome_known",
        ),
        Index("ix_api_calls_quota_day_model", "quota_day", "model"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    quota_day: Mapped[date] = mapped_column(Date)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    model: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
