from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import RunStatus, check_in


class IngestionRun(Base):
    """One execution of `news ingest`: the audit trail and the source of the run report."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (CheckConstraint(check_in("status", RunStatus), name="status"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{RunStatus.RUNNING}'"))
    feeds_processed: Mapped[int] = mapped_column(server_default=text("0"))
    pages_fetched: Mapped[int] = mapped_column(server_default=text("0"))
    items_new: Mapped[int] = mapped_column(server_default=text("0"))
    items_updated: Mapped[int] = mapped_column(server_default=text("0"))
    backlog_resolved: Mapped[int] = mapped_column(server_default=text("0"))
    backlog_remaining: Mapped[int] = mapped_column(server_default=text("0"))
    error_class: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
