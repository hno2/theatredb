# TheatreDB

Research database and browser for theatres, productions, people and their sources.

## Run

Requirements: Python 3.14+, `uv`, Docker, and a project-root `.env` containing
`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`.

Run the app locally:

```bash
docker compose up -d --wait db
uv run streamlit run frontend/app.py
```

Open <http://localhost:8501>. To run the app and crawler worker in Docker:

```bash
docker network create web  # if the configured Traefik network is missing
docker compose up -d --build
```

App and worker share the same runtime image layers, including ingestion and
Streamlit dependencies. The runtime image excludes `uv` and development tools;
use `python` or the installed commands for operations inside the container.

## Database

App and worker startup create a missing database and apply pending Alembic
migrations ([backend/migrations](backend/migrations)). Databases created before
Alembic are stamped as the baseline revision, so their schema must match it;
back up before upgrading.

Alembic settings live in `[tool.alembic]` in `pyproject.toml`; direct commands
such as `uv run alembic heads` work from the project root without an INI file.

```bash
uv run python -m backend.db migrate              # apply pending migrations
uv run python -m backend.db status               # current vs head, model drift
uv run python -m backend.db revision -m "msg"    # autogenerate after model changes
uv run python -m backend.db reset                # drop ALL data, recreate at head
```

After changing models in `backend/models.py`, run `migrate` and then
`revision`, review the generated file (autogenerate misses renames, enum value
changes and data migrations), and commit it. `reset` asks for the database name
(`--yes` skips); stop the app and worker first, as open connections block it.
Crawl files under `data/crawls` are kept. In Docker, run commands as
`docker compose run --rm app python -m backend.db ...`.

## Data

Theatre profiles batch-load productions and credits without recursive relationship
graphs, cache public profile data for up to 30 seconds (including during imports),
and render 12 productions per page. Search and filters still cover all productions,
not just the visible page. Production **Technische Angaben** shows the complete
loaded production JSON, including relationships, for debugging.

Profile lookup indexes cover organization foreign keys and entity sources;
PostgreSQL does not automatically index referencing foreign-key columns.
Apply pending migrations before deploying. Index creation can briefly block writes
on large tables; schedule migrations outside busy import periods.

Upload theatre production JSON or review reconciliation cases in **Verwaltung →
Datenabgleich & Duplikate**. Uploads can apply conflict-free changes; ambiguous
matches wait for review. Productions with different known premiere years are
imported separately without duplicate review, even when titles match. If either
year is unknown, the usual matching checks remain. For false positives, click
**Upload trotzdem hinzufügen** in the conflict review to keep both productions.
For standalone scraping, Theapolis imports, schemas and
validation, see the [ingestion guide](backend/ingestion/README.md).

Source URLs are clickable in every management view: selected entities' **Sources**
tables, reconciliation reviews, and **Quellen & Crawls**. Links open the source
record when available, otherwise the source website in general reviews.
Missing URLs stay unlinked; invalid or non-HTTP(S) URLs show a warning.

In **Verwaltung → Personen / Texte**, select multiple table rows and click
**Zusammenführen**. Conflict-free selections merge immediately; differing field
values open a comparison dialog with survivor and value choices. All dependent
relationships, sources and old IDs follow the merged record.
Both pages show the total record count below the heading. The people table loads
people and their production/theatre context in two queries, regardless of record count.
The same table autosaves simple fields: birth/death dates and nationality for
people, publication dates and comma-separated languages for texts. Dates accept precision-preserving
ISO inputs (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`); invalid values show errors without
changing the record. Select a row's checkbox for the full editor, or multiple
checkboxes for Merge. Names, titles and relationship columns remain read-only
inline.

Theapolis imports are accepted directly, without pending source reviews. Existing
conflicting values are kept; missing values and credits are added. Incremental
imports skip unchanged records and commit batches, making organizations, people
and productions visible on UI refresh before the whole import finishes. After a
failure, completed batches remain saved; rerun to resume without duplicates.
Missing export records are not deleted. Other sources retain their review
workflow under **Andere Quellen & gemeldete Duplikate**.

Source-owned background crawls run from **Verwaltung → Quellen → Crawler**.
See the [crawler guide](backend/crawling/README.md) for worker commands and
operational details.

### Texts and work families

A **Text** is a readable or licensable version. Alternative titles (with optional
two-letter language codes) belong to that text; its display title is always
searchable too. `languages` is an ordered JSON list: first language is primary,
`[]` means unknown, and `mul` means multilingual/unspecified. Optional genres are
`play`, `musical`, `opera`, `novel`, `film`, and `other`.

Translations and published adaptations form a computed work family. **Vorlage /
nach** links different works instead (for example a novel and its musical).
Self-links, duplicate edges and cycles are rejected. Director-specific Fassungen
stay production `adapter` credits, unless published as their own text.

**Verwaltung → Texte** shows credits, source works, versions, dependent works and
productions. It also provides title editing, sentence-style lineage links, pasted
credit previews and the text review inbox. Ambiguous imports wait there rather
than choosing a work arbitrarily. Theatre JSON and publisher metadata use the
same title/family/translator resolver; see the [ingestion guide](backend/ingestion/README.md).

Wikidata search is read-only. Selecting an item creates an explicitly identified
stub; enrichment adds labels/aliases, authors, source works, languages and
publication dates. Network failures and unsupported metadata are reported.
Publisher extraction accepts supplied HTML and CSS rules; site-specific crawling
rules require verified fixtures rather than guessed selectors.

AI is optional and disabled until **all three** settings exist:
`AI_BASE_URL` (OpenAI-compatible API base, such as `http://localhost:11434/v1`),
`AI_API_KEY` (use a non-empty local placeholder for Ollama), and `AI_MODEL`.
AI actions require explicit opt-in and send the displayed text/candidate metadata
to that endpoint. Parsing starts with regex; AI lineage suggestions enter the
existing review inbox and cannot invent candidate IDs. Suggested titles remain
unverified unless a database title or Wikidata label/alias confirms them.

Back up before applying migration `0004`. It backfills titles/languages, folds
duplicate lineage notes, converts unknown relations to `based_on` while retaining
their original value in notes, and replaces legacy source-author credits with
source texts. Invalid legacy language codes, self-links or cycles stop migration
with a repair message rather than silently deleting data. Downgrading discards
alternative titles and all but each text's first language.

### Architecture decisions: performance

- **Text labels:** batch family roots + joined credits. Three queries per picker,
  not four per text. Request-local graph; no stale global ORM cache.
- **AI JSON:** use `jsonschema`, not a homegrown validator. Draft 2020-12,
  local references only, explicit errors. Reject non-finite numbers at decode.
- **Credit parsing:** match separator tokens first; trim preceding whitespace after
  splitting. Shared helper preserves Unicode rules without quadratic retries.
- **Review desk:** native Streamlit 1.66 tracked tabs; run only active panel.
  Conflict badge uses SQL `COUNT`, not case hydration. No custom tab component.
  Inactive widgets follow native cleanup: save edits before switching tabs.
- **Homepage metrics:** combine scalar count subqueries in one SQL statement.
  Five round trips become one; counts stay live, no cache invalidation.
- **Text credits:** fetch ancestor contributions once; inherit names in memory.
  Never scan all people to recover display spelling. Same-name strangers stay out.
- **Publisher imports:** reuse validated metadata and identity lookup when saving.
  One identity query per record, including replays. Standalone saves still validate.

Local SQLite samples: 100 isolated text choices, `402 -> 3` SQL statements;
inherited musical credits, `28 -> 5`, with zero full people-table scans.
Round-trip reductions, not production latency guarantees. Deferred work: [TODO](TODO.md#performance--reuse-follow-up).

## Check

```bash
uv run --all-groups pytest
uv run --all-groups prek run
```
