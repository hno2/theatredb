"""Preserve alternative person names."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Initialize existing people without inventing alternative names."""
    op.add_column("person", sa.Column("alt_names", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    with op.batch_alter_table("person") as batch:
        batch.alter_column("alt_names", existing_type=sa.JSON(), server_default=None)


def downgrade() -> None:
    """Remove alternative person names."""
    op.drop_column("person", "alt_names")
