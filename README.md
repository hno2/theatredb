## Install
### Docker
### Local Usage
The app loads database settings from the project-root `.env` file, regardless of
the working directory. Existing environment variables are not overwritten.
Set `THEATERDB_DATABASE_URL` (highest priority), `DATABASE_URL`, or individual
`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and
`POSTGRES_DB` values. Plain PostgreSQL URLs and legacy `postgresql+psycopg2`
URLs use the installed `psycopg` driver.
Keep credentials in `.env`, not in the `.venv` Python environment directory.

## Quick Start
### Data Ingestion

See the separate [ingestion guide](backend/ingestion/README.md) for the staging
JSON Schema, configurable Karlsruhe/Stuttgart scrapers, safe single-page tests,
scrape diffs and persistent page-change checks. Scraping does not write to the
database; uploads only auto-apply conflict-free changes.

#### Review-first reconciliation

Open **Verwaltung → Datenabgleich & Duplikate**:

1. **Theater-JSON hochladen**: choose the existing theatre, upload scraper output,
   confirm source and capture date (a `YYYYMMDD_` filename supplies the date).
   Matching happens only inside the selected theatre:
   - same title, no conflicting values → merged automatically (new fields, people,
     text and the upload as additional source are added);
   - no title in that theatre with ≥ 80 % similarity → created automatically;
   - everything else → **Produktionskonflikte**.
   People are matched by source profile URL, existing cast of the production, then a
   unique name (namesakes prefer the theatre's people); otherwise created. Texts are
   reused when the same author already has a text of that title.
   Every upload is logged (`importrun`: file, scrape scope from the snapshot filename,
   capture date, outcome counts). Selecting a theatre shows its latest data date and
   scope, how many productions have an uploaded source / a source from the theatre's
   own website (stored URL, otherwise the scraped site) and how complete description,
   premiere, direction, cast and text are. Below, **Scraping-Stand aller Theater**
   shows how many theatres (with productions) have recent data (≤ 30/90/365 days),
   stalest first.
2. **Produktionskonflikte** shows upload and best-fitting production side by side
   (theatre always shown, no UUIDs). Rows are coloured: red = conflict, green =
   added/extended, yellow = new person. Pick another production of the same theatre
   or create a new one; for each conflict choose *Bestand behalten* (default) or
   *Upload übernehmen*. Merging people, texts and sources happens in the backend.
3. **Weitere Prüffälle** keeps older generic review cases.
4. **Duplikate suchen / melden**: select the area at the top (people, organizations,
   texts, productions or awards). The people table has leading selection checkboxes
   and a **Produktionen · Theater** column, fetched with one people query and one
   joined context query per page (100 people), including productions linked through
   text contributions. UUID searches additionally follow existing redirects.
   Search by name or UUID; selection
   applies to the current page. Select two or more people, choose the surviving
   record and click **Personen zusammenführen** to preview. Empty fields are filled
   and lists combined; choose which value to keep for conflicting scalar fields.
   Confirm identity, then **Zusammenführung speichern** applies the whole selection
   in one transaction. Previewing never changes the database.
   Other areas retain the pair-review workflow. Suggested pairs and manual reports
   (including people under the expander) never merge automatically; inspect and
   approve them under **Weitere Prüffälle**. Rejected cases can be reopened with a reason.

The same import is available on the command line (theatre by name or ID):

```bash
uv run python -m backend.ingestion.staged_to_db \
  --json data/staged/staatstheater-karlsruhe.de/20260607_productions.json \
  --organization-name "Badisches Staatstheater Karlsruhe"
```

A whole upload is validated first and written in one transaction. Re-uploading is
idempotent; rejected uploads are not queued again. Older snapshots of an already
linked page only fill empty fields. Known role aliases such as
`Regie`/`Inszenierung`/`director` share a canonical credit role; actors' characters
are stored separately. Credits absent from a scrape are not deleted.

Confirmed source IDs/URLs are mapped persistently. Identical submissions reuse
their cases. Later snapshots require review again; older/undated snapshots cannot
replace a newer approved snapshot from the same source. Existing manually edited
values are not automatically overwritten. There is no global “official source
always wins” policy: conflicting fields need an explicit reviewer choice.

Merges run in one transaction, redirect all domain foreign keys (including
memberships, nominations and both text-lineage directions), collapse equivalent
credits/links, and preserve source identities, decision snapshots and SCD2 history.
Only self-lineages created by the merge are removed. A stale comparison is
rejected; refresh it and review again. Detail-page lookups follow old UUID
redirects. Merge is not a one-click reversible operation; back up the database
before large cleanup runs.

The app and `init_db()` add the new tables (`reconciliationcase`,
`sourcerecord`, `entityredirect`, `importrun`) without rewriting existing domain tables.
The person biography field is a required schema prerequisite. For an existing
PostgreSQL database without that column, apply this additive migration before
starting the app (fresh databases already include it):

```sql
ALTER TABLE person ADD COLUMN IF NOT EXISTS description VARCHAR;
```

The [separate Theapolis pipeline](backend/ingestion/README.md#theapolis-separate-directory-import)
exports API records, downloads public profile photos/theatre logos in parallel,
and queues generic review cases. It never uses the venue-upload importer or sets
Theapolis URLs as official websites. Do not use the historical `cli/ingest.py`
or other legacy direct-write scripts.

##### Python integration

All five entity types use the same backend API. Source metadata must exist first.
Theatre uploads use `backend.production_import.ingest_records()`; the generic
review API below serves other sources, legacy cases and duplicates:

```python
from backend.models import EntityType
from backend.reconciliation import (
    approve_case, discover_duplicates, find_candidates, list_cases,
    preview_case, propose_record, queue_duplicate,
)

# source_id and existing_person_id are existing database UUIDs.
case = propose_record(
    EntityType.PERSON,
    {"name": "Example Person", "description": "Updated biography"},
    source_id=source_id,
    external_id="person-123",
    external_url="https://theatre.example/people/123",
    captured_at=capture_datetime,  # timezone-aware, or omit if unknown
    actor_id="my-scraper",
)
preview = preview_case(case.id, target_id=existing_person_id)
# Show preview["fields"], ["candidates"] and ["blocked"] to a reviewer.
# Only after approval; omit fields to fill blanks/combine lists, not overwrite.
approve_case(
    case.id, target_id=existing_person_id,
    revision=preview["revision"], fields=["description"],
    actor_id="reviewer",
)

# Manual or scheduled duplicate discovery only submits candidates for review.
pairs = discover_duplicates(EntityType.PERSON, query="Example", limit=30)
duplicate_case = queue_duplicate(
    EntityType.PERSON, surviving_person_id, duplicate_person_id,
    reason="Same person confirmed by source profiles", actor_id="my-checker",
)
```

Use `find_candidates()` for read-only matching and `list_cases()` for queue
integration. `propose_record()` accepts `organization`, `person`, `production`,
`text` and `award`; use `title` for texts and `name` for the other types.
It requires a stable external ID, URL or explicit `record_key`. An approved person case can be referenced in
`context["credits"]` (`case_id`, canonical `role`, optional `character`).
`context["dependencies"]` entries (`case_id`, foreign-key `field`) resolve approved
organization/text references before applying a production. Unapproved prerequisites
block approval. New domain entities require `allow_create=True`; default is
update-only. `decide_case()` rejects/reopens cases with an audit note.

### UI
Start the German-language Streamlit app with `uv run streamlit run frontend/app.py`.
The start page introduces the research goal, shows database counts and location
coverage, and offers a searchable theatre map and directory. Select a map point
to reveal its detail-page link, or use “Ansehen” in the directory. Entries without
valid coordinates remain searchable in the directory. Counts describe the
current database, not the entire German-speaking theatre landscape.
If several theatres share coordinates, selecting the point shows a link for
each matching entry.

The interface uses native Streamlit components and a theme in
`.streamlit/config.toml`. German is the current UI language; a future translation
mode should extract UI strings into language catalogs and use one shared language
selection across pages, leaving theatre names and source data unchanged.

**Theater & Produktionen** opens a searchable directory with direct detail links,
not an entity-selection form. Theatre search includes city and country;
production search also includes the producing theatre and its location. Results
are paginated in groups of 12, with productions ordered by newest recorded
premiere first and undated entries last. Native `st.pagination` controls appear
above and below results and stay synchronized. Streamlit handles numbered pages,
previous/next arrows and responsive truncation. People remain discoverable as well.
Each profile links back to the directory; existing UUID detail URLs still work.

Theatre detail pages show an address and location map, a compact production list,
and readable source citations with review status and capture date. Unboxed
production previews show directors first (bold names, grouped once under Regie),
then comma-separated actors under “mit”. Other team credits remain on the full
production profile. Premiere, age guidance and duration share one icon facts line;
descriptions are word-truncated to 200 characters and clamped to two lines.

Title and person searches update while typing using native live inputs inside a
Streamlit fragment, so filtering does not reload the map. A collapsed **Filter**
expander offers exact recorded age recommendations (including unknown), an
inclusive premiere date range, and person search across all production credits
and text contributors. Filters combine; incomplete date ranges are flagged and
not applied. With no range selected, undated entries stay visible; an active range
excludes them. Sort by newest/oldest premiere (undated entries last) or title.
Long lists use native pagination, reset when search, filters or sorting change.
Premiere dates do not represent production run dates. Missing coordinates and
unusable source links are indicated explicitly. **Spielstätten &
Menschen** lists stored venues, seating capacities and linked theatre memberships
with their recorded dates; it does not claim to be a current ensemble roster.

Production profiles use the same style, with premiere, duration, age guidance,
cast and team credits, sources, and links to other productions at the theatre or
with the same contributors. Production text foundations show the recorded
title, language, publication year and linked authors/translators where available.
Known canonical credit roles display German labels; source-specific roles remain
unchanged. Person profiles list productions and shared credits with theatres and
other people. Counts use distinct recorded productions, not
roles or inferred similarity; “other” excludes the production currently shown.
Detail links navigate in the same tab and replace the entity kind and UUID in
the URL, including links between two entries on the detail page.
## Contribution

Pull requests run the **PR checks** workflow with Python 3.14 and locked uv
dependencies, including all dependency groups. `prek` selects files changed
between the PR base and head; hooks configured with `pass_filenames: false`
(including Pyrefly) still run their repository-wide checks. The full pytest suite
runs even if hooks fail, provided dependency installation succeeded.

## Planned Features
- [ ]
- [ ] Actual Dates of performances
- [ ] Crawl all Staatstheater/Stadttheater for new info weekly
