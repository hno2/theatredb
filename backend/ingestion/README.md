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

The file may also be an Open Repertoire v2 document; see
[specs/open-repertoire.md](../../specs/open-repertoire.md). Matching is limited
to that theatre. Re-uploading is idempotent; unresolved
conflicts go to **Produktionskonflikte**. Missing credits are not treated as
deletions.

Optional `performance_dates` are local ISO dates (`2026-03-01`) or
minute-precision date-times (`2026-03-01T19:30`). Reuploads add dates without
deleting existing dates. ORIF exports omit date-only performances because ORIF
requires a time.

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
and optional publisher `external_id`. The caller commits the transaction, including
the import-history record used by the admin overview.
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
`Sprache`, `Beschreibung`/`Inhalt`/`Klappentext` (→ `description`), and
`UA`/`Uraufführung` are accepted. `StagedText.description` fills an empty text
description only. `UA` is retained verbatim as
`world_premiere`, not mistaken for a text publication date. Names may be lists
or a single unsplit string; dates and language codes must use the staging format.

This entry point makes **no network requests** and accepts normalized publisher
metadata only. No publisher-specific selectors are bundled for the other publishers:
verified page fixtures and source permissions are still needed before configuring
their crawls. The existing managed crawler's production/person pipeline is not
silently repurposed as a text crawler.

### Rowohlt downloader

Rowohlt's public API supplies a paginated catalogue and play details. Downloads
write a validated publisher JSON snapshot, not database rows:

```bash
uv run python -m backend.ingestion.rowohlt download --play-id 8915
uv run python -m backend.ingestion.rowohlt download --limit 10
uv run python -m backend.ingestion.rowohlt download
uv run python -m backend.ingestion.rowohlt import
```

The default snapshot is `data/staged/rowohlt/plays.json`; use `download --output`
and `import --json` for another path. A successful download atomically replaces
the snapshot; failed downloads leave the previous file untouched. Full downloads
follow every catalogue page and fetch each unique play detail, sequentially with
a 200 ms pause before each request. A full run can take several minutes.
The CLI uses the existing `tqdm` terminal progress bars: downloads count validated
plays, with the catalogue total (capped by `--limit`) providing percentage and ETA;
imports count processed observations, including observations queued for review.
Failed detail requests do not advance the download bar. Bars close on completion
or failure; final success messages are only printed after saving or committing.
`import` validates the complete file before initializing the database, then
commits the shared publisher import in one transaction. Known identities replay
idempotently; conflicting metadata waits for text-link review.

Like the other API downloader, Rowohlt uses module-level `download`, `_fetch`,
and `_download_pages` helpers; `parse_detail` verifies and normalizes one API
response. `normalize_play` remains available for offline metadata normalization. Rowohlt
consumes structured API responses and remains separate from the configurable
theatre HTML pipeline.
The shared `ingest_text_metadata` accepts optional `progress`; existing non-CLI
callers remain silent by default.

Normalized fields include descriptions, subtitles, publisher genre labels,
keywords, minimum age, separate women/men/total cast minima and maxima, cast notes,
and title variants. Zero is a valid count; unknown stays `null`. Gender ranges
are independent of total cast size (doubling and flexible casting). Contradictory
upstream ranges are omitted with an explicit warning, never sorted or guessed;
the full original detail response remains in `SourceRecord.payload.source_metadata`,
including rights restrictions, contributor IDs and biographies, keyword hierarchy,
awards, links, original subtitles, premiere information and related records.
API timestamps and premiere dates are not text publication dates.

Rowohlt `description` (German) and `descriptionEnglish` (English) are converted
from HTML to Markdown with the installed `markdownify` library, preserving
paragraphs, emphasis, lists and links. They populate `Text.description` and
`Text.description_en` respectively; the existing description field stays
compatible with current readers. Missing English does not copy German or invent
a translation. `subtitleEnglish` and `castNoteEnglish` populate `subtitle_en` and
`cast_note_en`; these labels remain plain text.

Rights/material flags are normalized into nullable booleans:
`isFreeForSchoolAmateurPlay` → `is_free_for_school_amateur_play`,
`isNovelOrFilmMaterial` → `is_novel_or_film_material`,
`releasedForUdse` → `released_for_udse`, `hasRoleBook` → `has_role_book`, and
`foreignRights` → `foreign_rights`. Explicit `false` is retained; absent flags
remain unknown, not false. `freeTextSchoolAmateurPlay` and `foreignRightsText`
become Markdown `school_amateur_note` and `foreign_rights_note`. Flags and notes
are publisher observations, not an automatic grant of performance rights.
Re-download to obtain the new fields. Imports fill blanks only; existing
nonempty descriptions that differ after Markdown conversion wait for review
rather than overwriting previous imports or manual edits.

English foreign-rights titles are title variants, not proof of a translation.
`originalTitle` explicitly identifies the source title, so it creates a
translation edge even when a translator credit is missing; the source language
stays unknown unless the API says otherwise. Existing matching source titles and
authors are reused; title collisions without that source edge wait for review.
`originalSubtitle` belongs to source text. `translations` and
`translationsEnglish` describe licensed translation availability and premiere
information, not identified translated texts, so they are retained as Markdown
notes and do not create speculative lineage.

Explicit adaptation labels (`Bearbeitet von`,
`Fassung von`) put adapters on the dramatic text and original authors on its
`based_on` source. Source-book titles/authors take precedence over inferred
same-title sources. Unspecified source medium stays `other` (existing novels or
films can match); novel/film material catalogue entries without an explicit
adaptation are source texts, not invented stage versions. Other editor labels
stay editor credits. Unstructured “nach” mentions remain in the observation
rather than creating speculative lineage.

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
format. Both import paths preserve stable source mappings and then apply the
name and text matching described below.

### Cast and crew

The public production list exposes directors and creators, not complete cast
and crew. Normal archive `download` now also fetches the paginated JSON API
`/api/de/productions/{id}/participations` for every production missing fresh
credits; productions the list reports with `numberOfParticipants: 0` are stored
as confirmed empty without a request. Cast, characters, music, crew and assistants are retained, along with
the raw API evidence. Profile IDs are scoped separately from creator/director
IDs; missing profiles use participation-scoped IDs, never guessed name matches.
Different credit names for the same numeric profile ID retain one person.
A credit without a custom name (`usedNameCheck`/`usedName`) shows the profile's
main name, so the downloader fetches the official `name` from
`/api/de/profiles/{id}` only for credits with a custom name (about 2%), once per
profile with a 30-day SQLite cache; a deleted profile (404) keeps the fallback.
That name takes precedence; other observed names are stored in `Person.alt_names`
and stay unchanged in the production's source context and raw evidence.
Otherwise the name on most credits without a custom name wins; ties prefer
accents and umlauts, hyphenated names and spelled-out initials, then fewer name
parts. Thus `Clarke-Hasters` wins over
`Clarke`, `Hans Herbert Diehl` over `Hans H. Diehl`, and `Martina Mann` over
`Martina Beate Mann`. These rules select names within an established identity,
never merge separate people. Manual main-name edits are not overwritten;
an imported main name can change only while it still matches its previous
source observation. The importer logs name variants rather than aborting.
Archive imports link a legacy director/creator ID to a numeric profile ID when
both feeds agree on the production, credit name, role and Theapolis profile URL
without ambiguity. Explicit feed aliases take precedence. Remaining separate
people are merged by exact name (see below). No new download is needed for
this matching when the archive already has both feeds.
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

Export the current PostgreSQL Theapolis source-credit titles and their frequencies:

```bash
uv run python -m backend.ingestion.role_titles role-titles.csv
```

The export counts exact recorded credit labels, including assistant and leadership
prefixes, in the latest approved production source observations. It excludes
legacy director/creator-only observations without detail credits. Gesang and
Sopran classify as singer, separate from actor; dancers and puppeteers also have
their own types. Conducting, choir direction, sound design, directing assistance
and directing leadership have distinct categories. Musiktheaterregie, Sopran,
Choreografie / Direktion and the mapped directing assistant/leadership titles
remain distinct credits. Performance and sonstige Musiktheater stay unmapped.
Existing records are not rewritten: public person types and production credits
are classified on read, but filters on stored person types still search the
original values. Already-canonicalized actor credits cannot be distinguished
from acting without consulting their source observations.

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

Every import merges Theapolis people with the exact same name (director,
creator and profile records alike), unless the group contains two or more
distinct Theapolis profiles: those are treated as homonyms and left apart.
The survivor is the record linked to the Theapolis profile, so its URL stays.
Texts are merged when their normalized title (case, whitespace and punctuation
ignored) and their non-empty author set match; the merged text keeps the best
formatted title (heading case preferred, with deterministic tie-breaking).
Groups with other conflicting field values are skipped and logged for a manual
merge instead of aborting the import. Compatible Theapolis-only groups merge in
batches with their foreign-key references, source mappings, redirects, review
decisions, and audit history. Mixed-source identities, and texts with title
variants, lineage or library entries, use the reviewed merge path. Schema
migration `0006` indexes person references so matching does not scan every
credit table per name.

To apply this matching to an existing database without importing:

```bash
uv run python -m backend.ingestion.theapolis merge-duplicates
```

To merge records manually, use **Verwaltung → Personen / Texte → Zusammenführen**.
Merging people also merges their compatible texts with the same normalized title
and complete non-empty author set, in the same transaction. Conflicting text
metadata is logged and requires a manual text merge.

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
