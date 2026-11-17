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

Theapolis uses a separate API export and imports accepted domain records directly,
without generic review cases. Missing fields and credits are added; conflicting
non-empty values and existing relationship targets are preserved. Export and
import use the central database, so PostgreSQL must be configured and running.

```bash
uv run python -m backend.ingestion.theapolis download --concurrency 10
uv run python -m backend.ingestion.theapolis images --concurrency 10
uv run python -m backend.ingestion.theapolis import
```

By default, import includes only organizations in Germany, Austria or Switzerland
with at least two distinct productions in the snapshot, plus those productions
and their texts and people. Use `import --all` to import the full snapshot;
`import --dach-only` explicitly selects the default filter.
Organizations without a recognized country, productions without an organization,
and organizations with fewer than two productions are excluded. Partial snapshots
may therefore exclude organizations with additional productions outside the
snapshot. The filter does not delete records from previous imports.

Import validates the entire snapshot, including source URLs, before writing.
Embedded NUL bytes in source text are removed with field-specific warnings
before validation, including text stored in JSON observations and audit history.
Source IDs containing NUL bytes are rejected rather than changed; the original
export files remain untouched.
It then saves batches of up to 500 organizations, people, and productions, in
that order. Organizations and people are visible before production import
finishes; refresh the UI to see committed progress. Each production batch saves
its texts, credits, source observations and audit history atomically.

Re-run the same command after an interruption: completed batches remain saved,
and stable source identities prevent duplicates. Only the current failed batch
is rolled back. An importer already running old code must be restarted to use
batch commits.

Imports are incremental. Unchanged observations skip domain and credit writes
and add no audit events. A newer unchanged export advances observation timestamps
only, so older exports cannot later fill blanks using stale data. New records,
missing values and added credits are imported; changed non-empty values are kept
in the source observation without overwriting existing domain values. Records
and credits absent from a newer export are not deleted.

For a bounded export check, add `--max-pages 1`; partial exports are not selected
by the default import.

Reimports use stable Theapolis source IDs, never names alone. Director and creator
IDs use separate namespaces (`directors:<id>` / `creators:<id>`), even when names
or profile URLs match. This can create duplicates against records without a
source mapping. Merge people/texts in **Verwaltung → Personen / Texte**: select
multiple rows and click **Zusammenführen**. Without field conflicts, merging is
immediate; otherwise a native comparison dialog lets you choose the surviving
record and field values. Production credits, text contributions, memberships,
awards and text lineage are updated; sources, old-ID redirects and audit remain.
Reimporting follows those redirects instead of recreating deleted duplicates.

To explicitly migrate stored pending Theapolis cases, back up the database, then:

```bash
uv run python -m backend.ingestion.theapolis migrate-cases
```

Migration reads stored payloads and dependencies, accepts them transactionally,
and retains the original cases with approved decisions and target IDs. It is
idempotent. Legacy unqualified person IDs are converted only when their role
proves the namespace; ambiguous cases fail with case IDs and require the original
snapshot. Missing dependencies or source collisions roll back the migration.
Migration is not run automatically at app startup or on import.

Other sources still use **Datenabgleich & Duplikate → Andere Quellen & gemeldete
Duplikate**; theatre-upload conflicts remain in **Produktionskonflikte**.
Existing exports remain importable; download again to look up profiles skipped
due to cross-role ID collisions. Image downloads do not grant publication rights.
