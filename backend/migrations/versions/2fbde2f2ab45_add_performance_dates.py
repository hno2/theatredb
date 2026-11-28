"""add performance dates

Revision ID: 2fbde2f2ab45
Revises: 0017
Create Date: 2026-11-28 10:45:50.574014

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2fbde2f2ab45"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add empty performance dates to existing productions."""
    op.add_column("production", sa.Column("performance_dates", sa.JSON(), nullable=False, server_default="[]"))
    op.alter_column("production", "performance_dates", server_default=None)


def downgrade() -> None:
    """Remove performance dates."""
    op.drop_column("production", "performance_dates")
