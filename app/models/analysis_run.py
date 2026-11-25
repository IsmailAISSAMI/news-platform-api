from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalysisRun(Base):
    """One execution of an analysis command (`analyze`, `lab`): the audit trail of a run.

    Counters are written when the run finishes; `stop_reason` says why it stopped early,
    if it did (for example a quota or busy-service limit).
    """

    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    command: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    concurrency: Mapped[int] = mapped_column(Integer)
    queued: Mapped[int] = mapped_column(Integer)
    succeeded: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    needs_review: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    skipped: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    stop_reason: Mapped[str | None] = mapped_column(Text)
