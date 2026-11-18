# TheatreDB

TheatreDB helps research theatres, productions, people, texts, and their sources.

## Run locally

Add database settings to a root-level `.env` file:

```dotenv
POSTGRES_USER=theaterdb
POSTGRES_PASSWORD=replace-this
POSTGRES_DB=theaterdb
```

Install project dependencies, start PostgreSQL, then start the app:

```bash
uv sync --all-groups
docker compose up -d --wait db
uv run python -m backend.db migrate
uv run streamlit run frontend/app.py
```

Open <http://localhost:8501>.

To run the app and crawler worker in Docker instead:

```bash
docker network create web  # once, if the network does not exist
docker compose up -d --build
```

Open <http://localhost:8501>. Stop Docker services with `docker compose down`.

## Docs

- [Product specification](specs/specification.md)
- [Design decisions](specs/design-decisions.md)
- [Development commands](specs/development.md)
- [Ingestion guide](backend/ingestion/README.md)
- [Crawler guide](backend/crawling/README.md)
