"""api_calls: estimated input tokens and error, for cross-process pacing

Revision ID: 7c3e9a1f5b20
Revises: de01c28240f2
Create Date: 2026-11-25 12:00:00

`requested_at` (timestamptz, default now()) already stamps each reservation; with
`est_input_tokens` a new process can rebuild the last 60 seconds of use.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c3e9a1f5b20"
down_revision: str | None = "de01c28240f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_calls", sa.Column("est_input_tokens", sa.Integer(), nullable=True))
    op.add_column("api_calls", sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_calls", "error")
    op.drop_column("api_calls", "est_input_tokens")
