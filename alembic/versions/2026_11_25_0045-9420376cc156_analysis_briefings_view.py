"""analysis briefings view

Adds `article_analyses.briefing` (the servable part of an analysis: no evidence quotes,
no unverified item), backfills it, and exposes it to the API role through the
`analysis_briefings` view. A view runs with its owner's privileges, so `news_api` reads
the view while it still has no right at all on `article_analyses`.

Revision ID: 9420376cc156
Revises: 61b8c5b98bf3
Create Date: 2026-11-25 00:45:03.730052

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Data migration: uses make_briefing AS OF THIS REVISION. If the function changes later,
# a new migration recomputes the column; this one is never edited.
from app.analysis.briefing import make_briefing

# revision identifiers, used by Alembic.
revision: str = "9420376cc156"
down_revision: str | Sequence[str] | None = "61b8c5b98bf3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "article_analyses",
        sa.Column("briefing", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    table = sa.table(
        "article_analyses",
        sa.column("id", sa.BigInteger),
        sa.column("status", sa.Text),
        sa.column("cleaned", postgresql.JSONB),
        sa.column("unverified_paths", postgresql.JSONB),
        sa.column("briefing", postgresql.JSONB),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(table.c.id, table.c.cleaned, table.c.unverified_paths).where(
            table.c.status.in_(("succeeded", "needs_review")), table.c.cleaned.is_not(None)
        )
    ).all()
    for row_id, cleaned, unverified in rows:
        bind.execute(
            table.update()
            .where(table.c.id == row_id)
            .values(briefing=make_briefing(cleaned, list(unverified or [])))
        )

    op.execute(
        """
        CREATE VIEW analysis_briefings AS
        SELECT article_id, analyzer_version, status, briefing, updated_at
        FROM article_analyses
        WHERE status IN ('succeeded', 'needs_review') AND briefing IS NOT NULL
        """
    )
    # scripts/db_roles.sql repeats this grant (and keeps article_analyses revoked).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_api') THEN
                GRANT SELECT ON analysis_briefings TO news_api;
                REVOKE ALL ON article_analyses FROM news_api;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP VIEW analysis_briefings")
    op.drop_column("article_analyses", "briefing")
