"""Index organization profile lookups.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Index foreign-key and polymorphic source lookups."""
    op.create_index("ix_production_organization_id", "production", ["organization_id"], if_not_exists=True)
    op.create_index(
        "ix_organizationmembership_organization_id", "organizationmembership", ["organization_id"], if_not_exists=True
    )
    op.create_index("ix_entitysource_entity", "entitysource", ["entity_type", "entity_id"], if_not_exists=True)


def downgrade() -> None:
    """Remove profile lookup indexes."""
    op.drop_index("ix_entitysource_entity", table_name="entitysource")
    op.drop_index("ix_organizationmembership_organization_id", table_name="organizationmembership")
    op.drop_index("ix_production_organization_id", table_name="production")
