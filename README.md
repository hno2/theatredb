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

Source-owned background crawls run from **Verwaltung → Quellen → Crawler**.
See the [crawler guide](backend/crawling/README.md) for worker commands and
operational details.

## Check

```bash
uv run --all-groups pytest
uv run --all-groups prek run
```
