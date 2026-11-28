"""Track successful non-theatre imports.

Revision ID: 0018
Revises: 2fbde2f2ab45
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "2fbde2f2ab45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create import history for publisher and directory imports."""
    op.create_table(
        "ingestionrun",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("scope", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ingestionrun_imported_at"), "ingestionrun", ["imported_at"], unique=False)


def downgrade() -> None:
    """Remove non-theatre import history."""
    op.drop_index(op.f("ix_ingestionrun_imported_at"), table_name="ingestionrun")
    op.drop_table("ingestionrun")
