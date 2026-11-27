"""Cast requirements and descriptive publisher text metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep existing cast counts unknown and initialize empty metadata lists."""
    for field in ("subtitle", "cast_note"):
        op.add_column("text", sa.Column(field, sa.String(), nullable=True))
    for field in ("keywords", "genre_labels"):
        op.add_column("text", sa.Column(field, sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    with op.batch_alter_table("text") as batch:
        for field in ("keywords", "genre_labels"):
            batch.alter_column(field, server_default=None)
    for field in (
        "women_min",
        "women_max",
        "men_min",
        "men_max",
        "total_cast_min",
        "total_cast_max",
        "suggested_min_age",
    ):
        op.add_column("text", sa.Column(field, sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove publisher metadata fields."""
    for field in (
        "subtitle",
        "cast_note",
        "keywords",
        "genre_labels",
        "women_min",
        "women_max",
        "men_min",
        "men_max",
        "total_cast_min",
        "total_cast_max",
        "suggested_min_age",
    ):
        op.drop_column("text", field)
