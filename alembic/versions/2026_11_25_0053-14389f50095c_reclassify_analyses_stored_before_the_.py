"""reclassify analyses stored before the quarantine rule

Analyses saved before migration 61b8c5b98bf3 were marked 'succeeded' even when some of
their evidence quotes were not found in the article (6 quotes in 3 articles on
2026-11-24). The quarantine rule says such an analysis is 'needs_review', with the
unverified paths listed and removed from the served briefing. This data migration
applies the rule to the rows stored before it existed, from their stored evidence
statuses (no article text is read and no model is called).

It uses `app.analysis.briefing.make_briefing` and the evidence status names as of this
revision.

Revision ID: 14389f50095c
Revises: 9420376cc156
Create Date: 2026-11-25 00:53:18.821427

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.analysis.briefing import make_briefing

# revision identifiers, used by Alembic.
revision: str = "14389f50095c"
down_revision: str | Sequence[str] | None = "9420376cc156"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same set as app.analysis.pipeline.UNVERIFIED, as of this revision.
UNVERIFIED = {"fabricated", "misplaced", "bad_paragraph"}


def upgrade() -> None:
    """Mark stored analyses with unverified quotes as needs_review."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, evidence, cleaned FROM article_analyses "
            "WHERE status = 'succeeded' AND evidence IS NOT NULL AND cleaned IS NOT NULL"
        )
    ).all()
    for row_id, evidence, cleaned in rows:
        unverified = sorted(
            path
            for path, status in evidence.items()
            if not path.startswith("issue:") and status in UNVERIFIED
        )
        if not unverified:
            continue
        connection.execute(
            sa.text(
                "UPDATE article_analyses SET status = 'needs_review', "
                "unverified_paths = CAST(:paths AS jsonb), "
                "briefing = CAST(:briefing AS jsonb), updated_at = now() WHERE id = :id"
            ),
            {
                "id": row_id,
                "paths": json.dumps(unverified),
                "briefing": json.dumps(make_briefing(cleaned, unverified)),
            },
        )


def downgrade() -> None:
    """Data-only: the reclassification is the correct state and is kept.

    Reverting it would mark analyses with unverified quotes as succeeded again, which
    the CHECK constraint ck_article_analyses_succeeded_fully_verified forbids.
    """
