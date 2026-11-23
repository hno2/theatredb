"""Add pgvector and embedded description chunks for semantic search."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Requires a PostgreSQL image with pgvector (see docker-compose.yml)."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "descriptionchunk",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("production_id", sa.Uuid(), sa.ForeignKey("production.id", ondelete="CASCADE"), nullable=True),
        sa.Column("text_id", sa.Uuid(), sa.ForeignKey("text.id", ondelete="CASCADE"), nullable=True),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("description_hash", sa.String(), nullable=False),
        sa.Column("pipeline", sa.String(), nullable=False),
        sa.Column("embedding", Vector(640), nullable=False),
        sa.CheckConstraint("(production_id IS NULL) <> (text_id IS NULL)", name="ck_descriptionchunk_one_owner"),
    )
    op.create_index("ix_descriptionchunk_production_id", "descriptionchunk", ["production_id"])
    op.create_index("ix_descriptionchunk_text_id", "descriptionchunk", ["text_id"])


def downgrade() -> None:
    """The vector extension stays installed; it may be shared with other schemas."""
    op.drop_table("descriptionchunk")
