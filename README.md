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
matches wait for review. For standalone scraping, Theapolis imports, schemas and
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
people, publication dates and language for texts. Dates accept precision-preserving
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

## Check

```bash
uv run --all-groups pytest
uv run --all-groups prek run
```
