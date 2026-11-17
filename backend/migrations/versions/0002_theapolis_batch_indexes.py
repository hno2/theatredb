"""Index Theapolis batch identity and credit lookups.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add indexes without changing existing identity constraints."""
    op.create_index(
        "ix_entitysource_external_identity",
        "entitysource",
        ["source_id", "entity_type", "external_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_sourcerecord_external_identity",
        "sourcerecord",
        ["source_id", "entity_type", "external_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_productionmembership_production_id", "productionmembership", ["production_id"], if_not_exists=True
    )
    op.create_index("ix_textcontribution_text_id", "textcontribution", ["text_id"], if_not_exists=True)


def downgrade() -> None:
    """Remove only the batch lookup indexes."""
    op.drop_index("ix_textcontribution_text_id", table_name="textcontribution")
    op.drop_index("ix_productionmembership_production_id", table_name="productionmembership")
    op.drop_index("ix_sourcerecord_external_identity", table_name="sourcerecord")
    op.drop_index("ix_entitysource_external_identity", table_name="entitysource")
