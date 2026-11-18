"""Text titles, multilingual texts, and constrained derivation.

Revision ID: 0004
Revises: 0003
"""

import logging
import re
import unicodedata
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None
logger = logging.getLogger("alembic.runtime.migration")


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", value).casefold()).split())


def _migrate_titles() -> tuple[sa.Table, sa.Table]:
    bind = op.get_bind()
    op.add_column("text", sa.Column("languages", sa.JSON(), nullable=True))
    op.add_column("text", sa.Column("genre", sa.String(), nullable=True))
    texts = sa.Table("text", sa.MetaData(), sa.Column("id", sa.Uuid(), primary_key=True), autoload_with=bind)
    title_rows = []
    for row in bind.execute(sa.select(texts)).mappings():
        language = row["language"].strip().lower() if row["language"] else None
        if language and not re.fullmatch(r"[a-z]{2}|mul", language):
            raise ValueError(f"Text {row['id']}: invalid legacy language {language!r}; repair before migrating")
        bind.execute(texts.update().where(texts.c.id == row["id"]).values(languages=[language] if language else []))
        title_rows.append(
            {
                "id": uuid.uuid4(),
                "text_id": row["id"],
                "title": row["title"],
                "language": language if language != "mul" else None,
                "normalized": _normalize(row["title"]),
            }
        )
    op.create_table(
        "texttitle",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("text_id", sa.Uuid(), sa.ForeignKey("text.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=True),
        sa.Column("normalized", sa.String(), nullable=False),
        sa.UniqueConstraint("text_id", "normalized"),
    )
    op.create_index("ix_texttitle_text_id", "texttitle", ["text_id"])
    op.create_index("ix_texttitle_normalized", "texttitle", ["normalized"])
    titles = sa.Table(
        "texttitle",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("text_id", sa.Uuid()),
        autoload_with=bind,
    )
    if title_rows:
        bind.execute(titles.insert(), title_rows)
    return texts, titles


def _migrate_lineages() -> sa.Table:
    bind = op.get_bind()
    lineages = sa.Table(
        "textlineage",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_text_id", sa.Uuid()),
        sa.Column("derived_text_id", sa.Uuid()),
        autoload_with=bind,
    )
    aliases = {
        "translation": "translation",
        "übersetzung": "translation",
        "translation of": "translation",
        "adaptation": "adaptation",
        "bearbeitung": "adaptation",
        "fassung": "adaptation",
        "based_on": "based_on",
        "based on": "based_on",
        "nach": "based_on",
        "vorlage": "based_on",
    }
    seen = {}
    edges = {}
    for row in bind.execute(sa.select(lineages).order_by(lineages.c.id)).mappings():
        if row["source_text_id"] == row["derived_text_id"]:
            raise ValueError(f"Self-referential text lineage {row['id']}; repair before migrating")
        relation = aliases.get(row["relation"].strip().lower(), "based_on")
        notes = row["notes"]
        if row["relation"].strip().lower() not in aliases:
            notes = "\n".join(filter(None, (notes, f"Original relation: {row['relation']}")))
        key = (row["source_text_id"], row["derived_text_id"], relation)
        if key in seen:
            survivor_id, survivor_notes = seen[key]
            merged = "\n".join(dict.fromkeys(filter(None, (survivor_notes, notes)))) or None
            bind.execute(lineages.update().where(lineages.c.id == survivor_id).values(notes=merged))
            bind.execute(lineages.delete().where(lineages.c.id == row["id"]))
            seen[key] = (survivor_id, merged)
        else:
            bind.execute(lineages.update().where(lineages.c.id == row["id"]).values(relation=relation, notes=notes))
            seen[key] = (row["id"], notes)
        edges.setdefault(row["source_text_id"], set()).add(row["derived_text_id"])
    pending = set(edges) | {child for children in edges.values() for child in children}
    while pending:
        leaves = {node for node in pending if not edges.get(node, set()) & pending}
        if not leaves:
            raise ValueError("Text lineage cycle; repair legacy graph before migrating")
        pending -= leaves
    return lineages


def _migrate_source_authors(texts: sa.Table, titles: sa.Table, lineages: sa.Table) -> None:
    bind = op.get_bind()
    contributions = sa.Table(
        "textcontribution",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("text_id", sa.Uuid()),
        sa.Column("person_id", sa.Uuid()),
        autoload_with=bind,
    )
    source_authors = list(
        bind.execute(sa.select(contributions).where(contributions.c.role == "source author")).mappings()
    )
    logger.info("Migrating %d source author contributions", len(source_authors))
    source_stubs = {}
    for row in source_authors:
        has_source = bind.execute(
            sa.select(lineages.c.id).where(
                lineages.c.derived_text_id == row["text_id"], lineages.c.relation == "based_on"
            )
        ).first()
        if not has_source:
            if row["text_id"] not in source_stubs:
                original_title = bind.execute(sa.select(texts.c.title).where(texts.c.id == row["text_id"])).scalar_one()
                source_id, title = uuid.uuid4(), f"{original_title} (Vorlage)"
                bind.execute(texts.insert().values(id=source_id, title=title, languages=[]))
                bind.execute(
                    titles.insert().values(
                        id=uuid.uuid4(), text_id=source_id, title=title, normalized=_normalize(title)
                    )
                )
                bind.execute(
                    lineages.insert().values(
                        id=uuid.uuid4(),
                        source_text_id=source_id,
                        derived_text_id=row["text_id"],
                        relation="based_on",
                        notes="Source text reconstructed from legacy source author credits.",
                    )
                )
                source_stubs[row["text_id"]] = source_id
            bind.execute(
                contributions.insert().values(
                    id=uuid.uuid4(), text_id=source_stubs[row["text_id"]], person_id=row["person_id"], role="author"
                )
            )
        elif row["text_id"] in source_stubs:
            bind.execute(
                contributions.insert().values(
                    id=uuid.uuid4(), text_id=source_stubs[row["text_id"]], person_id=row["person_id"], role="author"
                )
            )
        bind.execute(contributions.delete().where(contributions.c.id == row["id"]))


def upgrade() -> None:
    texts, titles = _migrate_titles()
    lineages = _migrate_lineages()
    _migrate_source_authors(texts, titles, lineages)
    with op.batch_alter_table("text") as batch:
        batch.alter_column("languages", existing_type=sa.JSON(), nullable=False)
        batch.drop_column("language")
    with op.batch_alter_table("textlineage") as batch:
        batch.create_unique_constraint("uq_textlineage_edge", ["source_text_id", "derived_text_id", "relation"])
        batch.create_check_constraint("ck_textlineage_not_self", "source_text_id != derived_text_id")
        batch.create_check_constraint(
            "ck_textlineage_relation", "relation IN ('translation', 'adaptation', 'based_on')"
        )


def downgrade() -> None:
    with op.batch_alter_table("textlineage") as batch:
        batch.drop_constraint("ck_textlineage_relation", type_="check")
        batch.drop_constraint("ck_textlineage_not_self", type_="check")
        batch.drop_constraint("uq_textlineage_edge", type_="unique")
    op.add_column("text", sa.Column("language", sa.String(), nullable=True))
    texts = sa.Table("text", sa.MetaData(), sa.Column("id", sa.Uuid(), primary_key=True), autoload_with=op.get_bind())
    for row in op.get_bind().execute(sa.select(texts)).mappings():
        op.get_bind().execute(
            texts.update().where(texts.c.id == row["id"]).values(language=next(iter(row["languages"]), None))
        )
    with op.batch_alter_table("text") as batch:
        batch.drop_column("languages")
        batch.drop_column("genre")
    op.drop_table("texttitle")
