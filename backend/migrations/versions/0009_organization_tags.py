"""Add independently curated organization tags."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Initialize existing organizations with empty tags, without inventing labels."""
    op.add_column("organization", sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.alter_column("organization", "tags", server_default=None)


def downgrade() -> None:
    """Remove organization tags."""
    op.drop_column("organization", "tags")
