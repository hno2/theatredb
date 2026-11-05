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
database; imports still require review.

#### Review-first reconciliation

Open **Verwaltung → Datenabgleich & Duplikate**:

1. **Theater-JSON einreichen** uploads existing scraper output. Enter the source
   and theatre name and confirm the actual capture date. A `YYYYMMDD_` filename
   supplies the suggested date; an unknown date stays unknown.
2. **Prüfliste** compares source records with existing entities. Review in order:
   organization, people, texts, productions. Search cases by name/title, reason,
   or case UUID; use the type/status filters and pagination for larger queues.
   Approved prerequisites resolve organization, text and credit references.
   After approving prerequisites, use **Vergleich aktualisieren** on an already
   open dependent case.
3. Select an existing target, inspect matching evidence and choose which fields
   to update. Empty source values never erase existing values. Populated scalar
   fields remain unchanged unless selected; lists are combined. Confirm identity,
   fields and credits before saving. Creation requires choosing **Neuen Datensatz
   anlegen** and explicitly confirming; creation uses the complete source payload.
4. **Duplikate suchen / melden** searches existing organizations, people, texts,
   productions and awards. Submit suggested pairs or manually select any two
   records, even with different names. Submission never merges automatically.
   Review the pair, choose the surviving UUID, inspect affected links and approve.
   Rejected cases can be reopened with a reason.

The staged importer now queues proposals instead of directly creating entities:

```bash
uv run python -m backend.ingestion.staged_to_db \
  --json data/staged/staatstheater-karlsruhe.de/20260607_productions.json \
  --organization-name "Badisches Staatstheater Karlsruhe"
```

Organizations are resolved before production matching. Exact titles alone are
never sufficient for an automatic merge (there are no automatic approvals).
Production suggestions compare organization, premiere, directors and overlapping
personnel by canonical role. Missing credits/dates are unknown, not conflicts.
Text suggestions compare authors; person names alone remain review candidates.
Candidate searches are bounded to 200 records, with source-linked identities
always included; duplicate discovery is a bounded search, not an exhaustive audit.
Use manual search/UUID selection when the desired candidate is not suggested.
Known role aliases such as `Regie`/`Inszenierung`/`director` share a canonical
credit role; actors' characters are stored separately. Credits absent from a
scrape are not deleted.

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

The app and `init_db()` add the three new tables (`reconciliationcase`,
`sourcerecord`, `entityredirect`) without rewriting existing domain tables.
The person biography field is a required schema prerequisite. For an existing
PostgreSQL database without that column, apply this additive migration before
starting the app (fresh databases already include it):

```sql
ALTER TABLE person ADD COLUMN IF NOT EXISTS description VARCHAR;
```

Legacy Theapolis import scripts, where available locally, bypass this review
workflow: do not use them for ongoing reconciliation.

##### Python integration

All five entity types use the same backend API. Source metadata must exist first.
Theatre-specific staged JSON is an adapter on top of this generic API:

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
It requires a stable external ID, URL or explicit `record_key`. The staged adapter
uses source assertions scoped to each production for people without profile URLs,
not globally unique names. An approved person case can be referenced in
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

Theatre detail pages show an address and location map, a premiere timeline
(newest or oldest first), and readable source citations with review status and
capture date. Productions without a valid premiere remain listed separately;
premiere dates do not represent production run dates. Missing coordinates and
unusable source links are indicated explicitly. Production titles are searchable;
long lists scroll within the production tab, while sources have their own tab.
Production cards show premiere dates and recorded credits. **Spielstätten &
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
