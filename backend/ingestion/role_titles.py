"""Export observed Theapolis credit titles from PostgreSQL.

Run: uv run python -m backend.ingestion.role_titles output.csv
"""

import argparse
import csv
from pathlib import Path

from sqlalchemy import text

from backend.user_management import _get_engine


def main() -> None:
    """Write one row per exact source credit title, ordered by frequency."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    engine = _get_engine()
    if engine.dialect.name != "postgresql":
        raise ValueError("This export requires PostgreSQL.")
    query = text("""
        SELECT credit->>'role' AS title, count(*) AS mentions
        FROM sourcerecord AS record
        JOIN source ON source.id = record.source_id
        CROSS JOIN LATERAL json_array_elements(
            COALESCE(record.payload->'context'->'theapolis_detail'->'credits', '[]'::json)
        ) AS credit
        WHERE source.name = 'theapolis.de'
          AND record.entity_type = 'PRODUCTION'
          AND credit->>'role' IS NOT NULL
        GROUP BY title
        ORDER BY mentions DESC, title
    """)
    with engine.connect() as connection:
        rows = connection.execute(query).all()
    with args.output.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("title", "mentions"))
        writer.writerows(rows)
    print(f"Wrote {len(rows)} titles to {args.output}")


if __name__ == "__main__":
    main()
