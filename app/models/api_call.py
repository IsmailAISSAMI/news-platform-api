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
        CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative"),
        Index("ix_api_calls_quota_day_model", "quota_day", "model"),
        Index("ix_api_calls_run_id", "run_id"),
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
    # The estimate made before sending, so the per-minute window survives across processes.
    est_input_tokens: Mapped[int | None] = mapped_column(Integer)
    # Why a request did not end as answered (for example "cancelled", "HTTP 503").
    error: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
