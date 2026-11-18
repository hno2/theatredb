# Ingestion

Theatre ingestion is **scrape → staged JSON → upload → review conflicts**.
Standalone scraping never writes to the database. The importer validates the
whole file before writing; conflict-free matches may apply automatically, while
ambiguous matches wait for review. See the [crawler guide](../crawling/README.md)
for managed background crawls. Import behavior and data guarantees are in the
[product specification](../../specs/specification.md).

## Scrape a theatre

Run commands from the repository root. Install scraper dependencies if needed:

```bash
uv sync --all-groups
```

`--config` accepts `karlsruhe`,
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
review changes before using `--accept` to advance the baseline:

```bash
uv run python -m backend.ingestion.scrape check --config karlsruhe \
  --url https://www.staatstheater-karlsruhe.de/programm/info/3899/
uv run python -m backend.ingestion.scrape check --config karlsruhe \
  --url https://www.staatstheater-karlsruhe.de/programm/info/3899/ --accept
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

Snapshots may include an optional `text` object, independently of the production
title:

```json
{
  "title": "Production title",
  "url": "https://theatre.example/production",
  "text": {
    "title": "German play title",
    "original_title": "Original play title",
    "original_language": "en",
    "language": "de",
    "authors": ["Original Author"],
    "translators": ["German Translator"],
    "based_on": {"title": "Source novel", "authors": ["Novel Author"]}
  }
}
```

`based_on` accepts a title or recursively nested text metadata. Optional `genre`,
`published`/`published_precision`, `composers`, and `lyricists` are also accepted.
Languages use two-letter codes or `mul`. Without `text`, the existing production
author credits and title are used; canonical translator credits also inform text
matching. Adapters remain production memberships, not text authors. Ambiguous
texts create persistent `link_text` cases retaining the production IDs; no
arbitrary text is attached.
An author-only upload preserves an already linked specific text, even when the
production's display title differs. Translator refinement without explicit text
metadata starts from the linked text's title, not the production display title.

## Publisher metadata

`backend.ingestion.publishers.ingest_text_metadata(session, source_name, records)`
imports a list of normalized text metadata dictionaries with a required `url`
and optional publisher `external_id`. The caller commits the transaction.
The source registry covers **theatertexte.de**, **Rowohlt**, **S. Fischer**,
**Felix Bloch Erben**, **Henschel**, and **Verlag der Autoren** (`VdA`).
Source URLs are retained in `EntitySource`; full observations, including `UA`,
are retained in `SourceRecord`. Ambiguous observations are retained on the
text-link review case instead.
Approved text-link cases can persist these observations with
`resolve_publisher_observations(session, text, observations)`; each observation
contains `source_id`. The single-observation helper is
`save_publisher_observation(session, text, observation)`.
Reimports resolve publisher identity before matching titles. Changed nonempty text
metadata queues review without creating a duplicate or overwriting canonical text
fields; known identities only fill missing fields and credits automatically.

The same text resolver handles publisher and theatre metadata. German labels
`Titel`, `Originaltitel`, `Originalsprache`, `Autor`/`Autoren`, `Übersetzung`,
`Sprache`, and `UA`/`Uraufführung` are accepted. `UA` is retained verbatim as
`world_premiere`, not mistaken for a text publication date. Names may be lists
or a single unsplit string; dates and language codes must use the staging format.

This entry point makes **no network requests**. For supplied HTML,
`extract_metadata(html, url=..., rules=...)` reuses the configurable crawler's
validated CSS `FieldRule` and extraction helpers. No publisher-specific selectors
or live-page parsing are bundled: verified page fixtures and source permissions
are still needed before configuring actual publisher crawls. The existing managed
crawler's production/person pipeline is not silently repurposed as a text crawler.

## Theapolis

Theapolis uses a separate API export and imports accepted domain records directly.
Configure PostgreSQL before importing.

```bash
uv run python -m backend.ingestion.theapolis download --concurrency 10
uv run python -m backend.ingestion.theapolis images --concurrency 10
uv run python -m backend.ingestion.theapolis import
```

Default import uses the DACH filter. Use `import --all` for the full export.
Theapolis sends country and sometimes a state or canton in `organization.location.country`.
The shared [`backend.locations`](../locations.py) helper resolves country codes,
English/German country names, and unambiguous ISO subdivision names offline using
`pycountry`, storing regions in `Organization.region` and countries in
`Organization.country`. A small alias list covers local spellings absent from
the package. Country-only labels leave the region unset; unknown or ambiguous
labels are retained as-is with `--all`. Reimport the latest snapshot to repair
previously imported region labels that still match their stored source observation.
Partial exports, such as `download --max-pages 1`, are not selected by default.
See the [product specification](../../specs/specification.md) for filtering,
matching, conflict, and resume behavior.

To merge records manually, use **Verwaltung → Personen / Texte → Zusammenführen**.

To explicitly migrate stored pending Theapolis cases, back up the database first:

```bash
uv run python -m backend.ingestion.theapolis migrate-cases
```

Migration is idempotent and not run automatically. Ambiguous legacy IDs,
missing dependencies, or source collisions stop migration; resolve the reported
cases before retrying.

Other sources still use **Datenabgleich & Duplikate → Andere Quellen & gemeldete
Duplikate**; theatre-upload conflicts remain in **Produktionskonflikte**.
Download again to look up profiles skipped due to cross-role ID collisions.
Image downloads do not grant publication rights.
