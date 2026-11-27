"""Store Rowohlt availability of licensed translations."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep translated-rights notes separate from work translation lineage."""
    op.add_column("text", sa.Column("translations_note", sa.String(), nullable=True))
    op.add_column("text", sa.Column("translations_note_en", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove publisher availability notes."""
    op.drop_column("text", "translations_note_en")
    op.drop_column("text", "translations_note")
