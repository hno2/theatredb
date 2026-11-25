"""Private library: wishes, stars, reviews, visits and playlists; SSO users have no password."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.alter_column("password_hash", existing_type=sa.String(), nullable=True)
    op.create_table(
        "libraryentry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("text_id", sa.Uuid(), sa.ForeignKey("text.id"), nullable=True),
        sa.Column("production_id", sa.Uuid(), sa.ForeignKey("production.id"), nullable=True),
        sa.Column("want_to_see", sa.Boolean(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("review", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "text_id"),
        sa.UniqueConstraint("user_id", "production_id"),
        sa.CheckConstraint("(text_id IS NULL) != (production_id IS NULL)", name="ck_libraryentry_one_target"),
        sa.CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_libraryentry_rating"),
    )
    for column in ("user_id", "text_id", "production_id"):
        op.create_index(f"ix_libraryentry_{column}", "libraryentry", [column])
    op.create_table(
        "libraryvisit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("libraryentry.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("visited_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('read', 'watched')", name="ck_libraryvisit_kind"),
    )
    op.create_index("ix_libraryvisit_entry_id", "libraryvisit", ["entry_id"])
    op.create_table(
        "playlist",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name"),
    )
    op.create_index("ix_playlist_user_id", "playlist", ["user_id"])
    op.create_table(
        "playlistitem",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("playlist_id", sa.Uuid(), sa.ForeignKey("playlist.id"), nullable=False),
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("libraryentry.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("playlist_id", "entry_id"),
    )
    op.create_index("ix_playlistitem_playlist_id", "playlistitem", ["playlist_id"])
    op.create_index("ix_playlistitem_entry_id", "playlistitem", ["entry_id"])


def downgrade() -> None:
    for table in ("playlistitem", "playlist", "libraryvisit", "libraryentry"):
        op.drop_table(table)
    with op.batch_alter_table("user") as batch:
        batch.alter_column("password_hash", existing_type=sa.String(), nullable=False)
