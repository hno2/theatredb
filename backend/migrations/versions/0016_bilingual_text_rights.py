"""English text descriptions, translated labels and publisher rights metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve existing descriptions; absent language and rights fields stay unknown."""
    for field in ("description_en", "subtitle_en", "cast_note_en", "school_amateur_note", "foreign_rights_note"):
        op.add_column("text", sa.Column(field, sa.String(), nullable=True))
    for field in (
        "is_free_for_school_amateur_play",
        "is_novel_or_film_material",
        "released_for_udse",
        "has_role_book",
        "foreign_rights",
    ):
        op.add_column("text", sa.Column(field, sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Remove English and rights fields without changing the original description."""
    for field in (
        "description_en",
        "subtitle_en",
        "cast_note_en",
        "school_amateur_note",
        "foreign_rights_note",
        "is_free_for_school_amateur_play",
        "is_novel_or_film_material",
        "released_for_udse",
        "has_role_book",
        "foreign_rights",
    ):
        op.drop_column("text", field)
