from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import RightsPolicy, check_in


class Source(Base):
    """A publisher. Its rights policy governs everything ingested from it."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(check_in("rights_policy", RightsPolicy), name="rights_policy"),
        CheckConstraint("excerpt_max_chars >= 0", name="excerpt_max_chars_non_negative"),
        # Full text can only be enabled with a recorded license.
        CheckConstraint(
            "rights_policy <> 'licensed_full_text' OR license_ref IS NOT NULL",
            name="license_required_for_full_text",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    site_url: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)  # BCP 47, e.g. "en"
    rights_policy: Mapped[str] = mapped_column(Text)
    excerpt_max_chars: Mapped[int] = mapped_column(server_default=text("0"))
    license_ref: Mapped[str | None] = mapped_column(Text)
    license_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
