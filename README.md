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

Startup creates missing tables, but does not migrate existing domain tables.
Back up existing databases and ensure their schema matches the current models
before upgrading.

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
