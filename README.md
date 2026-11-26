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

### Read-only API

After installing dependencies and applying database migrations as above, run:

```bash
uv run uvicorn backend.api:app --reload
```

Open <http://localhost:8000/docs> for the interactive API documentation.
The API uses the same database settings as the app; it does not create databases
or run migrations on startup.

- `GET /api/organizations`, `/api/productions`, `/api/people`, `/api/texts`:
  list or search with `q`, `limit` (default 12, maximum 100), and `offset`
  (default 0). Responses contain `items` and `total` matching records.
- `GET /api/{resource}/{uuid}`: get a public entity graph with relationships
  and source links. Merged IDs resolve to the surviving entity. Missing records
  return 404; invalid UUIDs or pagination parameters return 422.
- `GET /api/organizations/{uuid}/orif`, `/api/productions/{uuid}/orif`,
  `/api/people/{uuid}/orif`: export as
  [Open Repertoire](https://buehnenverein.github.io/open-repertoire/) v2
  (`application/ld+json`). People return one document per theatre. See
  [specs/open-repertoire.md](specs/open-repertoire.md).

Search and ranking reuse the public catalog helpers, including theatre context
for productions and alternative titles for texts. Production lists include
future and undated premieres. No UI filters or upcoming-preview exclusions are
applied. Internal reviewer identities are omitted from public organization data.

Only GET routes are available. The API is unauthenticated and intended for
public archive reads; it exposes no management, user, or crawler endpoints.

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
