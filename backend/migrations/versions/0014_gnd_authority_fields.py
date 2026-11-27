"""Person gender, text creation date and external concept URIs for the GND authority import."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Existing rows stay unknown (NULL); vocabulary URIs are filled by init_db's sync_vocabulary."""
    with op.batch_alter_table("person") as batch:
        batch.add_column(sa.Column("gender", sa.String(), nullable=True))
        batch.create_check_constraint("ck_person_gender", "gender IN ('m', 'f', 'none', 'div')")
    op.add_column("text", sa.Column("created", sa.DateTime(), nullable=True))
    op.add_column(
        "text",
        sa.Column(
            "created_precision",
            postgresql.ENUM("YEAR", "MONTH", "DAY", "MINUTE", name="dateprecision", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("tag", sa.Column("uri", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop the GND authority fields."""
    op.drop_column("tag", "uri")
    op.drop_column("text", "created_precision")
    op.drop_column("text", "created")
    with op.batch_alter_table("person") as batch:
        batch.drop_constraint("ck_person_gender", type_="check")
        batch.drop_column("gender")
