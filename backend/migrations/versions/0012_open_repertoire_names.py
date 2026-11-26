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


def _rename_at(data: object, key: str | None, mapping: dict[str, str]) -> object:
    """Rename keys of ``data`` itself, or of its nested ``key`` object."""
    if key is None:
        return _rename_keys(data, mapping)
    if isinstance(data, dict) and isinstance(data.get(key), dict):
        return {**data, key: _rename_keys(data[key], mapping)}
    return data


def _rewrite_column(
    table: str, id_column: str, column: str, key: str | None, mapping: dict[str, str], types: tuple[str, ...] = ()
) -> None:
    bind = op.get_bind()
    rows = sa.table(table, sa.column(id_column), sa.column("entity_type"), sa.column(column, sa.JSON))
    query = sa.select(rows.c[id_column], rows.c[column])
    if types:
        query = query.where(sa.cast(rows.c.entity_type, sa.String).in_(types))
    for row_id, value in bind.execute(query):
        if (renamed := _rename_at(value, key, mapping)) != value:
            bind.execute(rows.update().where(rows.c[id_column] == row_id).values({column: renamed}))


def _rewrite_json(reverse: bool) -> None:
    def mapping(kind: str) -> dict[str, str]:
        names = RENAMES[kind]
        return {new: old for old, new in names.items()} if reverse else names

    for kind in PAYLOAD_KINDS:
        types = (kind, kind.upper())
        _rewrite_column("reconciliationcase", "id", "incoming", None, mapping(kind), types)
        _rewrite_column("sourcerecord", "id", "payload", "incoming", mapping(kind), types)
    _rewrite_column("sourcecrawl", "source_id", "definition", "production", mapping("production"))


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
