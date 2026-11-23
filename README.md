# TheatreDB

TheatreDB helps research theatres, productions, people, texts, and their sources.

## Run locally

Add database settings to a root-level `.env` file:

```dotenv
POSTGRES_USER=theaterdb
POSTGRES_PASSWORD=replace-this
POSTGRES_DB=theaterdb
# Local only: act as admin without SSO (see specs/development.md)
THEATERDB_DEV_AUTH=1
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

### Browse the archive

The **Entdecken** menu has separate pages for theatres, productions, people, and
texts. Search and filters can be shared as a URL, for example
`/produktionen?q=Hamlet&year_from=2012&year_to=2020`. The year filters refer to
founding, premiere, birth, or publication, respectively. The productions page
shows the next dated premieres first in a separate preview when no search or
filters are active; premieres shown there are left out of the list below.

Production results exclude premieres after today by default (undated entries
remain visible); the upcoming-premieres preview is unaffected. Use
`exclude_planned=false` to include future dates. Sort by `sort=Name` or
`sort=Premiere` (default newest first, names A-Z); `order=asc|desc` overrides
the direction. Widget changes keep the browser URL in sync.

Theatre listings show the city only if it is not already part of the name.
Full addresses still include the city.

Organization and production tags appear as pills. Their filters offer all stored
tags; selecting multiple tags matches any selected value. Organization tags are
independent of production tags and can be edited in organization management.

## Docs

- [Production deployments](specs/development.md#production-deployments)
- [Sign-in and development access](specs/development.md#sign-in-and-development-access)
- [Product specification](specs/specification.md)
- [Personal library and sign-in](specs/user-library.md)
- [Design decisions](specs/design-decisions.md)
- [Development commands](specs/development.md)
- [Ingestion guide](backend/ingestion/README.md)
- [Crawler guide](backend/crawling/README.md)
