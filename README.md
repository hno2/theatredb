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

Upload theatre production JSON or review reconciliation cases in **Verwaltung →
Datenabgleich & Duplikate**. Uploads can apply conflict-free changes; ambiguous
matches wait for review. For standalone scraping, Theapolis imports, schemas and
validation, see the [ingestion guide](backend/ingestion/README.md).

In **Verwaltung → Personen / Texte**, select multiple table rows and click
**Zusammenführen**. Conflict-free selections merge immediately; differing field
values open a comparison dialog with survivor and value choices. All dependent
relationships, sources and old IDs follow the merged record.
The same table autosaves simple fields: birth/death dates and nationality for
people, publication dates and language for texts. Dates accept precision-preserving
ISO inputs (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`); invalid values show errors without
changing the record. Select a row's checkbox for the full editor, or multiple
checkboxes for Merge. Names, titles and relationship columns remain read-only
inline.

Theapolis imports are accepted directly, without pending source reviews. Existing
conflicting values are kept; missing values and credits are added. Other sources
retain their review workflow under **Andere Quellen & gemeldete Duplikate**.

Source-owned background crawls run from **Verwaltung → Quellen → Crawler**.
See the [crawler guide](backend/crawling/README.md) for worker commands and
operational details.

## Check

```bash
uv run --all-groups pytest
uv run --all-groups prek run
```
