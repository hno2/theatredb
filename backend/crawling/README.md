# Background crawls

Manage crawls in **Verwaltung → Quellen → Crawler**. A separate worker fetches
pages, validates extracted data and submits changed records through the existing
theatre-import or review workflow.

## Run

For local development, start PostgreSQL and the app as described in the
[repository README](../../README.md), then run the worker in another terminal:

```bash
uv run python -m backend.crawling.worker
```

Docker Compose starts the app and worker together:

```bash
docker compose up -d --build
```

App and worker initialize the durable PostgreSQL queue schema automatically.
SQLite is supported for tests only.

## Configure

- **Theater (YAML):** use a bundled scraper config and select an existing theatre.
- **Other source (JSON):** define listing URLs, selectors and fields for generic
  review proposals. Config is data; it cannot run Python or shell commands.

`Source` owns each crawler and its run history. Changed extracted records are
submitted; unchanged records are skipped. Failed or cancelled runs never delete
domain data. HTTP cache entries may survive a failed run, but import checkpoints
advance only with a successful import. Respect source permissions and robots
policies; requests are bounded and rate-limited per source.

Artifacts are stored under `data/crawls/` locally and in the `crawl-data` Docker
volume. SQLite and the PostgreSQL queue are not interchangeable.

## Test

```bash
uv run pytest -q tests/test_crawling.py
THEATERDB_TEST_POSTGRES=1 uv run pytest -q tests/test_crawling.py
```

The second command requires PostgreSQL credentials with `CREATEDB`; it creates
and removes a disposable test database.
