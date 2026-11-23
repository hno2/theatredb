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

`download` now defaults to a local content-addressed SQLite archive at
`data/staged/theapolis.de/theapolis.sqlite3`. No Postgres writes are made by
archive download. Production pages are committed independently; rerun the same
command to resume, or pass `--restart` if the upstream pagination has drifted.
`--max-pages` is for testing and cannot replace a full snapshot. Completed
refreshes still enumerate the upstream list; Theapolis list deltas are not
assumed. Postgres import, however, applies only changed in-scope records after
the first successful baseline. It remembers its checkpoint per archive and
import scope. Keep using the same archive file.

When switching from legacy downloads, add `download --reuse-profile-cache` to
copy fresh lookup results from the existing PostgreSQL cache into the archive.
This only reads Postgres. Positive and negative lookups are then cached locally
for 30 days. Large batches of remaining cache misses scan the paginated public
profile directory instead of searching one name at a time; only unique exact
matches request profile details.

To start offline with an existing full export:

```bash
uv run python -m backend.ingestion.theapolis seed --json path/to/full.source.json
```

`download --json path.json` and `import --json path.json` retain the legacy
format. Archive imports preserve existing stable source mappings; they do not
merge director and creator records solely by name.

### Cast and crew

The public production list exposes directors and creators, not complete cast
and crew. Normal archive `download` now also fetches the paginated JSON API
`/api/de/productions/{id}/participations` for every production missing fresh
credits. Cast, characters, music, crew and assistants are retained, along with
the raw API evidence. Profile IDs are scoped separately from creator/director
IDs; missing profiles use participation-scoped IDs, never guessed name matches.
Different credit names for the same numeric profile ID retain one person.
The downloader fetches that profile's official `name` from
`/api/de/profiles/{id}`, once per profile with a 30-day SQLite cache.
That name takes precedence; other observed names are stored in `Person.alt_names`
and stay unchanged in the production's source context and raw evidence.
When the official name is unavailable in an older archive or supplied feed,
fallback selection prefers accents and umlauts, hyphenated names and spelled-out
initials; otherwise it prefers fewer name parts. Thus `Clarke-Hasters` wins over
`Clarke`, `Hans Herbert Diehl` over `Hans H. Diehl`, and `Martina Mann` over
`Martina Beate Mann`. These rules select names within an established identity,
never merge separate people. Manual main-name edits are not overwritten;
an imported main name can change only while it still matches its previous
source observation. The importer logs name variants rather than aborting.
Archive imports link a legacy director/creator ID to a numeric profile ID when
both feeds agree on the production, credit name, role and Theapolis profile URL
without ambiguity. Explicit feed aliases take precedence. Existing separate
people are logged for a reviewed merge, not automatically merged. No new
download is needed for this matching when the archive already has both feeds.
The legacy `download --json` interchange export remains director/creator-only;
use the archive workflow for full API credits.

To backfill an already downloaded archive without downloading its list or
repeating biography lookups:

```bash
uv run python -m backend.ingestion.theapolis details
```

After upgrading, run `uv run python -m backend.db migrate`, then `details` and
`import` to populate official names and alternatives. The first updated detail
capture refreshes older participation-cache entries once; no production-list
redownload or database reset is needed. Older archives still import with fallback
name selection. A normal unchanged import does not backfill previously skipped
records.

Detail downloads default to 32 concurrent requests with connection reuse and
existing HTTP retry/backoff. Use `details --concurrency N` or
`download --detail-concurrency N` to change that separately from list/profile
concurrency. Successful responses and their cache checkpoints commit in bounded
batches, with a final flush on interruption or error. Rerun after interruption to resume;
fresh participation results are reused for seven days, independently of
production-list timestamps (which may not reflect cast changes). The first
backfill necessarily needs at least one API request per production; later
imports still apply only changed records. Empty cast is confirmed only after
validating all reported pages and counts. API errors fail the download without
sealing the incomplete run or losing previous observations.

The explicit authorized JSON Lines feed remains supported:

```bash
uv run python -m backend.ingestion.theapolis details --file authorized-credits.jsonl
uv run python -m backend.ingestion.theapolis import
```

One line per production, using the list production ID and stable provider
person IDs:

```json
{"production_id":"123","theapolis_detail_status":"parsed","theapolis_detail":{"credits":[{"person_id":"person-367","name":"Example Performer","role":"Spiel","character":"Hamlet"},{"person_id":"person-992","name":"Example Director","role":"Regie","legacy_ids":{"directors":"17"}}]}}
```

Only supply `legacy_ids` for provider-verified aliases; `creators` is the other
supported scope. Use `confirmed_empty` with `credits: []` for a verified empty
detail; omit unobserved productions. Missing and empty data do not delete any
accepted memberships. Raw observations stay in the archive.

Default import uses the DACH filter. Use `import --all` for the full export.
`import` logs total wall time on completion or failure.
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

Production premiere types come from the API's `type.pill` (for example `UA`
or `DEA`), falling back to `type.name`, and are retained as tags alongside genre.
Downloads fetch organization descriptions through the paginated organization
directory, not one request per production. Public person `biographyText` and
`extraBiographyText` are preserved as descriptions; HTML is converted to Markdown.
Production descriptions are imported when supplied; the production list and
detail endpoints sampled in November 2026 did not expose one.
Existing nonempty descriptions are never overwritten.

Download again to collect organization descriptions and biographies. Older
positive profile-cache entries are refreshed once to include biography data.
Old exports remain importable and gain premiere tags from their raw source
metadata; new exports validate those tags and descriptions against staged JSON.

Cross-role matching batches compatible Theapolis-only people, their foreign-key
references, source mappings, redirects, review decisions, and audit history.
Mixed-source identities retain the reviewed merge path. Schema migration `0006`
indexes person references so matching does not scan every credit table per name.

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
