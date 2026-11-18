# Background crawls

Manage crawls in **Verwaltung → Quellen → Crawler**. A separate worker fetches
pages, validates extracted data and submits changed records through the existing
theatre-import or review workflow. For behavior and data guarantees, see the
[product specification](../../specs/specification.md).

## Run

For local development, start PostgreSQL and the app as described in the
[repository README](../../README.md), then run the worker in another terminal:

```bash
uv run python -m backend.crawling.worker
```

For Docker, create the external `web` network once if it does not exist, then
start the app and worker:

```bash
# Run once, only if the network is missing:
docker network create web
docker compose up -d --build
```

View worker logs with `docker compose logs -f crawl-worker`. App and worker
initialize the PostgreSQL queue schema automatically. SQLite is supported for
tests only.

## Configure

- **Theater (YAML):** use a bundled scraper config and select an existing theatre.
- **Other source (JSON):** define listing URLs, selectors and fields for generic
  review proposals under **Datenabgleich & Duplikate → Andere Quellen & gemeldete
  Duplikate**. Config is data; it cannot run Python or shell commands.

Minimal generic-source config:

```json
{
  "kind": "records",
  "base_url": "https://example.org",
  "listing": {
    "urls": ["https://example.org/people"],
    "selector": "a.person",
    "include": "/people/"
  },
  "entity_type": "person",
  "fields": {
    "name": {"selector": "h1", "required": true}
  }
}
```

Listing URLs must stay on the configured origin. Selectors and fields are
validated before a run; relationship fields need a dedicated adapter. Respect
source permissions and robots policies. Artifacts are stored under
`data/crawls/` locally and in the `crawl-data` Docker volume.

## Test

```bash
uv run --all-groups pytest -q tests/test_crawling.py
THEATERDB_TEST_POSTGRES=1 uv run --all-groups pytest -q tests/test_crawling.py
```

The second command requires PostgreSQL credentials with `CREATEDB`; it creates
and removes a disposable test database.
