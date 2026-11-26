"""Rename columns to their Open Repertoire Interchange Format (ORIF) equivalents.

Stored import payloads and crawler definitions use entity field names too, so
their keys are renamed alongside the columns. SCD2 history keeps its recorded shape.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

RENAMES = {
    "organization": {
        "street": "street_address",
        "zip_code": "postal_code",
        "city": "address_locality",
        "country": "address_country",
        "lat": "latitude",
        "lon": "longitude",
    },
    "production": {"age_rating": "suggested_min_age"},
    "productionmembership": {"role": "role_name", "character": "character_name"},
}
PAYLOAD_KINDS = ("organization", "production")


def _rename_keys(data: object, mapping: dict[str, str]) -> object:
    if not isinstance(data, dict):
        return data
    return {mapping.get(key, key): value for key, value in data.items()}


def _rewrite_json(reverse: bool) -> None:
    def mapping(kind: str) -> dict[str, str]:
        names = RENAMES[kind]
        return {new: old for old, new in names.items()} if reverse else names

    bind = op.get_bind()
    cases = sa.table("reconciliationcase", sa.column("id"), sa.column("entity_type"), sa.column("incoming", sa.JSON))
    records = sa.table("sourcerecord", sa.column("id"), sa.column("entity_type"), sa.column("payload", sa.JSON))
    for kind in PAYLOAD_KINDS:
        types = (kind, kind.upper())
        for row in bind.execute(sa.select(cases.c.id, cases.c.incoming).where(cases.c.entity_type.in_(types))):
            if (incoming := _rename_keys(row.incoming, mapping(kind))) != row.incoming:
                bind.execute(cases.update().where(cases.c.id == row.id).values(incoming=incoming))
        for row in bind.execute(sa.select(records.c.id, records.c.payload).where(records.c.entity_type.in_(types))):
            payload = row.payload
            if isinstance(payload, dict) and isinstance(payload.get("incoming"), dict):
                renamed = {**payload, "incoming": _rename_keys(payload["incoming"], mapping(kind))}
                if renamed != payload:
                    bind.execute(records.update().where(records.c.id == row.id).values(payload=renamed))
    crawls = sa.table("sourcecrawl", sa.column("source_id"), sa.column("definition", sa.JSON))
    for row in bind.execute(sa.select(crawls.c.source_id, crawls.c.definition)):
        definition = row.definition
        if isinstance(definition, dict) and isinstance(definition.get("production"), dict):
            renamed = {**definition, "production": _rename_keys(definition["production"], mapping("production"))}
            if renamed != definition:
                bind.execute(crawls.update().where(crawls.c.source_id == row.source_id).values(definition=renamed))


def _rename_columns(reverse: bool) -> None:
    for table, names in RENAMES.items():
        with op.batch_alter_table(table) as batch:
            for old, new in names.items():
                current, target = (new, old) if reverse else (old, new)
                batch.alter_column(current, new_column_name=target)


def upgrade() -> None:
    """Adopt ORIF field names in tables, pending import payloads and crawler definitions."""
    _rename_columns(reverse=False)
    _rewrite_json(reverse=False)


def downgrade() -> None:
    """Restore the previous column and payload names."""
    _rewrite_json(reverse=True)
    _rename_columns(reverse=True)
