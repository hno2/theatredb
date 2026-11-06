# Source-owned crawls

## Decision: Procrastinate, not Celery

The application already uses PostgreSQL, psycopg and async aiohttp crawlers.
[Procrastinate](https://procrastinate.readthedocs.io/en/stable/) supplies a durable
PostgreSQL queue, async workers, cancellation, retry policies and optional periodic
tasks without adding Redis or RabbitMQ.

Packages considered before implementation:

| Package | Fit |
| --- | --- |
| [Procrastinate](https://procrastinate.readthedocs.io/en/stable/) | Selected: existing PostgreSQL; async tasks; transactional enqueue and cancellation. |
| [Celery](https://docs.celeryq.dev/en/stable/userguide/workers.html) | Mature distributed runner, but adds a broker and another operational component; unnecessary for this first implementation. |
| [Scrapyd](https://scrapyd.readthedocs.io/en/latest/overview.html) | Already provides crawl scheduling, cancellation and status APIs, but runs Scrapy spiders; current supported crawlers are aiohttp-based. Rewriting them would discard working extraction rules. |
| [scrapy-deltafetch](https://github.com/scrapy-plugins/scrapy-deltafetch) | Skips previously seen Scrapy requests. Not a scheduler and not appropriate for detecting edits at existing production/profile URLs. |

No evaluated package supplies the application-specific Source ownership,
theatre-YAML contract, generic review workflow and safe import checkpoints.
Keep those in this small application layer, not a new crawler framework.

## Components and ownership

```text
Streamlit / Quellen
    -> service.save_config / start / stop
    -> PostgreSQL: Source + SourceCrawl + CrawlRun + Procrastinate job
                                              |
                                      separate worker process
                                              |
                         conditional HTTP -> extraction -> validation
                                              |
                                     full staging artifact
                                              |
                          compare submitted payload fingerprints
                             /                            \
               theatre YAML adapter                 record config adapter
               ingest_records                       propose_record
               existing automatic rules             generic review only
                             \                            /
                         import + fingerprints + success COMMIT
```

`Source.id` is the authoritative owner, not a YAML `source_id`, directory name,
theatre name, source name, Streamlit session or queue job ID. One Source can have
one configured crawler in this version. Sources such as books need no crawler.
Theatre configuration additionally requires an existing `Organization.id`;
it never creates a theatre implicitly.

[Source](../models.py) exposes `crawl_config`, `crawl_runs`, `crawl_pages` and
`crawl_records` relationships. All four new tables have real Source foreign keys:

| Table | Responsibility |
| --- | --- |
| `sourcecrawl` | One-to-one definition, enable flag and edit time. |
| `crawlrun` | Attempt history, frozen definition, queue job ID, actor, timestamps, state, heartbeat, phase, counters, artifact path and error. |
| `crawlpage` | Per-source/URL/representation HTTP validators and response body. Transport cache, **not** an import checkpoint. |
| `crawlrecord` | Per-source/adapter scope/stable record key last successfully submitted payload hash and originating run. Separate from approved `SourceRecord` identities. |

The existing `ImportRun` remains the theatre import audit. Crawl delta imports
use `scope="delta"` rather than falsely reporting complete website coverage.
Unchanged crawls create a `CrawlRun`, but no empty `ImportRun`. The staging
artifact contains the entire selected crawl result, not only changed records.
Queue retention does not remove application run history.

## Configurations

Under **Verwaltung -> Quellen -> Crawler**, select a Source:

* **Theater (YAML)**: choose an existing theatre, paste/edit YAML or start from a
  bundled template. This is the existing validated `ScraperConfig` contract.
  Config is persisted as normalized JSON, not an arbitrary worker-local path.
* **Weitere Quelle (Crawler-Konfiguration)**: configure discovery and extraction
  as typed JSON. A concrete review-source starting point:

```json
{
  "kind": "records",
  "base_url": "https://reviews.example",
  "entity_type": "production",
  "listing": {
    "urls": ["https://reviews.example/reviews"],
    "selector": "a.review-link",
    "include": "/reviews/",
    "next_selector": "a.next",
    "max_pages": 20
  },
  "fields": {
    "name": {"selector": "h1", "required": true},
    "critics": {"selector": ".review-summary", "many": true}
  },
  "request_delay": 1,
  "timeout_seconds": 30
}
```

Replace example selectors/URLs with those of the actual permitted source.
`fields` uses the existing `FieldRule` CSS/attribute/format extraction rules and
must map to the chosen domain model. Reviews currently live in
`Production.critics`; there is no separate Review entity. The review-page URL is
the proposal's citation, not automatically a production's official website.
Matching a review to a production remains an explicit generic reconciliation
decision; no title-only automatic cross-theatre match is introduced.

Both adapters are data-only and registered by a fixed `kind` discriminator.
No arbitrary Python imports, shell commands or expressions come from config.
Foreign-key extraction needs a dedicated adapter rather than guessed IDs.
Existing standalone CLI scraping remains database-independent.
Theapolis' separate API/image/import commands remain unchanged; they are not
silently treated as theatre YAML or generic HTML crawls.

## Incremental correctness

"Only the difference" has two independent meanings:

1. **Reduce downloaded bytes.** Revisit discovery pages and all discovered detail
   pages with `If-None-Match`/`If-Modified-Since` when available. On HTTP 304, use
   cached HTML. On 200, replace validators/body, including clearing validators
   the server no longer supplies. Without validators, fetching the body is
   necessary to detect edits; a previously seen URL is not permanently skipped.
2. **Reduce database work.** Parse/validate, normalize empty optional values and
   list ordering, then compare payload hashes. Submit only new/changed records.
   Never use raw HTML hashes as the import decision: navigation/ad changes must
   not produce new review cases.

Important details:

* A listing 304 does **not** prove detail pages are unchanged. Reuse its body to
  discover links, then revalidate detail pages independently.
* Person pages are also revalidated. Their data enriches productions before
  hashing, so a biography-only change is not missed when the production is 304.
* Parser/config changes reparse cached HTML; an unchanged HTTP response is not a
  reason to skip a new parser. HTTP cache variants include User-Agent/Accept.
* Import fingerprints include source ownership, adapter/entity or theatre
  scope, and for theatres whether biographies were requested. Run limits are
  retained explicitly; partial runs are never labelled full crawls.
* Response cache writes may survive a failed run. Submitted fingerprints never
  advance until all imports/proposals and the final run state commit together.
  Retrying after a failed import therefore cannot lose records.
* A rejected or still-pending proposal is not resubmitted every run. Only a
  changed source payload creates another submission; reviewer decisions remain
  separate from crawler observations.
* Missing links, 404s, failed discovery, pagination bounds and stopped runs never
  delete domain data. Failed extraction cannot turn a previous payload into
  "empty but successful". Removals/archival would need a separate explicit
  policy and comparable complete runs.
* **Alle Seiten neu laden** bypasses conditional headers, useful for unreliable
  server validators. It still avoids importing unchanged extracted records.

This first version deliberately prefers complete revalidation over speculative
timestamp watermarks. An API/feed adapter may later use a trustworthy updated-at
cursor with stable ordering, an overlap window, deletion semantics and periodic
full audits. Publish such a cursor only with its successful ingestion checkpoint.
Do not stop at the first known article or assume publication order equals update
order: old reviews and casts can be edited.

## Lifecycle and failure handling

States: `queued -> running -> succeeded | failed`, or
`queued -> cancelled` / `running -> cancelling -> cancelled`.

* Start locks the Source row and inserts the run and Procrastinate job using the
  **same psycopg transaction**. A partial unique index allows only one active run
  per Source across users/processes. Double-clicks report an explicit conflict.
* A worker atomically claims only a queued run. Definition edits affect the next
  run, never an already queued/running snapshot.
* Stop commits durable cancellation intent and queue cancellation together.
  Procrastinate interrupts async tasks; application checks also fence subsequent
  HTTP-cache writes and imports. Cancellation is not worker-wide termination.
* Final submission locks the run and commits data, fingerprints and success
  together. A stop racing an already committed import cannot undo that import.
* Heartbeats update every five seconds. A run silent for over three minutes can
  be explicitly marked failed in the UI. Its old worker then fails ownership
  checks; a new run may start. Queueing locks prevent duplicate pending jobs;
  no extra queue execution lock can trap a replacement behind a dead worker.
* Transient HTTP connection failures, 429 and 5xx receive bounded retries.
  `Retry-After` is respected up to 120 seconds; longer requests fail explicitly
  for a later run instead of retrying before the server permits it.
  Parsing/validation/import failures are recorded and re-raised, not converted
  to empty success. There is no automatic whole-crawl retry.
* UI polls persisted state every five seconds; closing/reloading Streamlit does
  not kill the worker. Logs go to worker logging; run failures remain in the DB.

## Setup and operations

New application tables are additive (`init_db()` creates missing tables); no
existing domain column is changed by this feature. Back up the DB before deployment.
App and worker startup automatically initialize the Procrastinate queue schema
when absent. No manual initialization command is required:

```bash
uv sync --all-groups
uv run python -m backend.crawling.worker
```

Run Streamlit separately with the same database configuration. Both use the
existing `THEATERDB_DATABASE_URL` / `DATABASE_URL` / `POSTGRES_*` settings.
SQLite is supported for unit tests only, not for the durable queue.

Docker:

```bash
docker compose build
docker compose up -d app crawl-worker
```

Set container `POSTGRES_HOST=db`, or a database URL reachable from both services.
The worker image includes the ingest dependency group. `crawl-data` persists
artifacts under `/app/data/crawls`, owned by the non-root worker. Locally, override
the artifact directory with `THEATERDB_CRAWL_DIR`. Artifact paths shown in the UI
refer to the worker filesystem; downloads are not exposed through a public URL.
Existing DBs must still satisfy the repository's earlier domain migrations.
Initialization is transactional and protected by a PostgreSQL advisory lock, so
concurrent app/worker startups cannot create the queue schema twice. Existing
queue data is preserved. `--init-schema` remains an idempotent setup-only command.
An incomplete/incompatible installation fails with a migration message rather
than overwriting existing objects. Automatic setup is not an upgrade migration; follow
[Procrastinate migrations](https://procrastinate.readthedocs.io/en/stable/howto/production/migrations.html)
when upgrading the dependency.

Treat source configuration as a trusted management operation, using the existing
management access policy. Requests and redirects remain on the configured
origin, responses are bounded to 5 MiB, and requests are serialized/rate-limited
per source. Two distinct Sources pointing at one host do not yet share a global
host limiter. Respect site permissions/robots policies before enabling a source;
this version does not automatically interpret robots.txt. Restrict worker
network access appropriately. JS/browser rendering, cron configuration UI,
generic API pagination, automatic removals and retention cleanup are future work,
not silently approximated by this implementation.

## Validation

```bash
uv run pytest -q tests/test_crawling.py
THEATERDB_TEST_POSTGRES=1 uv run pytest -q tests/test_crawling.py
```

The opt-in integration test creates and removes a uniquely named disposable
database using configured PostgreSQL credentials (requires `CREATEDB`).
It exercises a real queue worker, transactional enqueue rollback, HTTP 304,
automatic generic proposals, and queued/running cancellation. Other tests cover
source relationships, theatre imports, biography dependencies, import rollback,
partial/failing crawls, stale-worker fencing and Streamlit start/stop controls.
