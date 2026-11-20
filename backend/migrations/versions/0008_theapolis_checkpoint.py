"""Track the last applied Theapolis archive run."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "theapolisimportcheckpoint",
        sa.Column("archive_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("run", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("archive_id", "scope"),
    )


def downgrade() -> None:
    op.drop_table("theapolisimportcheckpoint")
