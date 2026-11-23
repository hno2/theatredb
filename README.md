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

Productions also carry a two-layer taxonomy: **Profil** (what it is: Sparte,
Premierenstatus, Arbeitsweise, Publikum, Kontext) and **Inhalt** (what it is
about: Thema, Handlungszeit, Schauplatz, Vorlage & Bezug). Each facet is a
filter (`tag_form=Tanz&tag_premiere=Uraufführung`); values within a facet match
any, facets combine with AND. Legacy tags map onto the vocabulary automatically;
after upgrading an existing database run once:

```bash
uv run python -m backend.taxonomy backfill
```

Production management edits assignments per facet. Manual choices always win
over source-mapped tags and survive re-imports and merges.

### Semantic search and tag suggestions

Production and text descriptions can be searched by meaning ("Familienkonflikt
im Exil") with the **In Beschreibungen suchen** toggle (`semantic=true`). It
needs PostgreSQL with pgvector (the compose `db` image is
`pgvector/pgvector`; switch existing deployments to it before migrating) and the
optional embeddings extra, which runs
[`microsoft/harrier-oss-v1-270m`](https://huggingface.co/microsoft/harrier-oss-v1-270m)
locally on CPU:

```bash
uv sync --all-groups --extra embeddings
uv run python -m backend.semantic index --limit 500   # new/changed descriptions only
uv run python -m backend.semantic search "Krieg und Flucht"
uv run python -m backend.semantic suggest              # Inhalt suggestions for review
uv run python -m backend.semantic cluster --min-size 5 # candidate new Inhalt tags
```

The first run downloads the model; pin `THEATERDB_EMBEDDING_REVISION` and set
`HF_HUB_OFFLINE=1` in production. Changing model, revision or chunking marks
all vectors stale. Suggestions appear in production management for accept or
reject; the model never overrides manual, source or rejected decisions.
`cluster` only prints a report: clusters with their typical words, examples and
already published tags, to decide which new Inhalt tags are worth adding.

## Docs

- [Production deployments](specs/development.md#production-deployments)
- [Product specification](specs/specification.md)
- [Design decisions](specs/design-decisions.md)
- [Development commands](specs/development.md)
- [Ingestion guide](backend/ingestion/README.md)
- [Crawler guide](backend/crawling/README.md)
