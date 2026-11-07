# Ingestion

Theatre ingestion is **scrape → staged JSON → upload → review conflicts**.
Standalone scraping never writes to the database. The importer validates the
whole file before writing; conflict-free matches may apply automatically, while
ambiguous matches wait for review. See the [crawler guide](../crawling/README.md)
for managed background crawls.

## Scrape a theatre

Run commands from the repository root. `--config` accepts `karlsruhe`,
`stuttgart`, `badische-landesbuehne`, or a YAML/JSON config path. Bundled configs
are in this folder.

Inspect exactly one page, or run a bounded scrape before scraping everything:

```bash
uv run python -m backend.ingestion.scrape page --config karlsruhe \
  --url https://www.staatstheater-karlsruhe.de/programm/info/3899/
uv run python -m backend.ingestion.scrape scrape --config karlsruhe \
  --limit 1 --no-people
```

A full scrape fetches all configured productions and linked profiles:

```bash
uv run python -m backend.ingestion.scrape scrape --config karlsruhe
```

Scrapes save timestamped snapshots under `data/staged/<source>/`. Follow source
permissions and robots policies; requests stay on the configured origin.

## Validate and compare

The staging contract is [staging.schema.json](staging.schema.json), defined by
[staging.py](staging.py).

```bash
uv run python -m backend.ingestion.scrape validate path/to/productions.json
uv run python -m backend.ingestion.scrape diff before.json after.json
uv run python -m backend.ingestion.scrape schema
```

`page` fetches one URL only. `check` compares one page with a local baseline;
`--accept` advances it. Use `--help` for options:

```bash
uv run python -m backend.ingestion.scrape --help
```

## Upload for review

Select an existing theatre by name or ID. This is also available in the
Streamlit reconciliation page.

```bash
uv run python -m backend.ingestion.staged_to_db \
  --json data/staged/staatstheater-karlsruhe.de/<snapshot>_productions.json \
  --organization-name "Badisches Staatstheater Karlsruhe"
```

Matching is limited to that theatre. Re-uploading is idempotent; unresolved
conflicts go to **Produktionskonflikte**. Missing credits are not treated as
deletions.

## Theapolis

Theapolis uses a separate API export and queues generic review cases; it does not
auto-create or update domain records. Export and import use the central database
for profile caching, so PostgreSQL must be configured and running.

```bash
uv run python -m backend.ingestion.theapolis download --concurrency 10
uv run python -m backend.ingestion.theapolis images --concurrency 10
uv run python -m backend.ingestion.theapolis import
```

For a bounded export check, add `--max-pages 1`; partial exports are not selected
by the default import.

Review cases under **Verwaltung → Datenabgleich & Duplikate → Weitere Prüffälle**.
Director and creator IDs use separate source namespaces, so the same person may
have separate review cases in each role. Check suggested matches when approving
them. Older cases using unqualified numeric person IDs are not reused; review
them separately to avoid duplicate approvals. Existing exports remain importable;
download again to look up profiles skipped due to cross-role ID collisions.
Approve organizations and people before texts and productions. Image downloads
do not grant publication rights.
