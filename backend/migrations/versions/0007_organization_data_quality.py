"""Organization editorial assessments and checked seasons.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("general_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organization", sa.Column("general_verified_by", sa.String(), nullable=True))
    op.add_column("organization", sa.Column("general_verification_evidence", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("history_from_season", sa.Integer(), nullable=True))
    op.add_column("organization", sa.Column("history_through_season", sa.Integer(), nullable=True))
    op.add_column("organization", sa.Column("history_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organization", sa.Column("history_verified_by", sa.String(), nullable=True))
    op.add_column("organization", sa.Column("history_verification_evidence", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("update_scope", sa.String(), nullable=True))
    op.add_column("organization", sa.Column("update_reason", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("update_flagged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organization", sa.Column("update_flagged_by", sa.String(), nullable=True))
    with op.batch_alter_table("organization") as batch:
        batch.create_check_constraint(
            "ck_organization_general_assessment",
            "(general_verified_at IS NULL AND general_verified_by IS NULL "
            "AND general_verification_evidence IS NULL) OR "
            "(general_verified_at IS NOT NULL AND general_verified_by IS NOT NULL "
            "AND general_verification_evidence IS NOT NULL "
            "AND length(trim(general_verified_by)) > 0 "
            "AND length(trim(general_verification_evidence)) > 0)",
        )
        batch.create_check_constraint(
            "ck_organization_history_assessment",
            "(history_from_season IS NULL AND history_through_season IS NULL "
            "AND history_verified_at IS NULL AND history_verified_by IS NULL "
            "AND history_verification_evidence IS NULL) OR "
            "(history_from_season IS NOT NULL AND history_through_season IS NOT NULL "
            "AND history_from_season BETWEEN 1000 AND 9998 "
            "AND history_through_season BETWEEN history_from_season AND 9998 "
            "AND history_verified_at IS NOT NULL AND history_verified_by IS NOT NULL "
            "AND history_verification_evidence IS NOT NULL "
            "AND length(trim(history_verified_by)) > 0 "
            "AND length(trim(history_verification_evidence)) > 0)",
        )
        batch.create_check_constraint(
            "ck_organization_update_flag",
            "(update_scope IS NULL AND update_reason IS NULL "
            "AND update_flagged_at IS NULL AND update_flagged_by IS NULL) OR "
            "(update_scope IS NOT NULL AND update_scope IN ('general', 'productions', 'both') "
            "AND update_reason IS NOT NULL AND update_flagged_at IS NOT NULL "
            "AND update_flagged_by IS NOT NULL AND length(trim(update_flagged_by)) > 0 "
            "AND length(trim(update_reason)) > 0 "
            ")",
        )
    op.create_table(
        "organizationseasoncheck",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("season_start_year", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_by", sa.String(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.CheckConstraint("season_start_year BETWEEN 1000 AND 9998", name="ck_season_check_start_year"),
        sa.CheckConstraint("length(trim(checked_by)) > 0", name="ck_season_check_actor"),
        sa.CheckConstraint("length(trim(evidence)) > 0", name="ck_season_check_evidence"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "season_start_year", name="uq_organization_season_check"),
    )
    op.create_index(
        "ix_organizationseasoncheck_organization_id",
        "organizationseasoncheck",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_organizationseasoncheck_organization_id", table_name="organizationseasoncheck")
    op.drop_table("organizationseasoncheck")
    with op.batch_alter_table("organization") as batch:
        batch.drop_constraint("ck_organization_update_flag", type_="check")
        batch.drop_constraint("ck_organization_history_assessment", type_="check")
        batch.drop_constraint("ck_organization_general_assessment", type_="check")
        for column in (
            "update_flagged_by",
            "update_flagged_at",
            "update_reason",
            "update_scope",
            "history_verification_evidence",
            "history_verified_by",
            "history_verified_at",
            "history_through_season",
            "history_from_season",
            "general_verification_evidence",
            "general_verified_by",
            "general_verified_at",
        ):
            batch.drop_column(column)
