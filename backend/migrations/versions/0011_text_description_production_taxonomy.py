"""Add text descriptions and the two-layer controlled production taxonomy."""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, length=max(map(len, values)))


def upgrade() -> None:
    """Vocabulary rows are synchronized by init_db; legacy tags via `python -m backend.taxonomy backfill`."""
    op.add_column("text", sa.Column("description", sa.String(), nullable=True))
    op.create_table(
        "tag",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("layer", _enum("taglayer", "profile", "content"), nullable=False),
        sa.Column("facet", sa.String(), nullable=False),
        sa.Column("definition", sa.String(), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_tag_facet", "tag", ["facet"])
    op.create_table(
        "productiontag",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("production_id", sa.Uuid(), sa.ForeignKey("production.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag_code", sa.String(), sa.ForeignKey("tag.code"), nullable=False),
        sa.Column("status", _enum("tagstatus", "suggested", "published", "rejected"), nullable=False),
        sa.Column("origin", _enum("tagorigin", "manual", "source", "model"), nullable=False),
        sa.Column("evidence", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("description_hash", sa.String(), nullable=True),
        sa.Column("pipeline", sa.String(), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.UniqueConstraint("production_id", "tag_code"),
    )
    op.create_index("ix_productiontag_production_id", "productiontag", ["production_id"])
    op.create_index("ix_productiontag_tag_code", "productiontag", ["tag_code"])


def downgrade() -> None:
    """Drop taxonomy tables and text descriptions; legacy production tags are untouched."""
    op.drop_table("productiontag")
    op.drop_table("tag")
    op.drop_column("text", "description")
