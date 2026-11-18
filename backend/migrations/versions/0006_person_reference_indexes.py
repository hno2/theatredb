"""Index references queried when matching imported people.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("organizationmembership", "textcontribution", "productionmembership", "awardnomination"):
        op.create_index(f"ix_{table}_person_id", table, ["person_id"], if_not_exists=True)


def downgrade() -> None:
    for table in ("awardnomination", "productionmembership", "textcontribution", "organizationmembership"):
        op.drop_index(f"ix_{table}_person_id", table_name=table)
