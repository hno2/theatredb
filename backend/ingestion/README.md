# Theatre ingestion

The pipeline is **website -> validated staged JSON -> reconciliation proposals ->
explicit review**. Scraping, schema validation and change checks do not connect to
the database. Never use the historical Scrapy/DB scripts for ongoing imports.

## Setup

From the repository root:

```bash
uv sync --group ingest --group dev
uv run python -m backend.ingestion.scrape --help
```

Bundled configs: [Karlsruhe](configs/karlsruhe.yaml) and
[Stuttgart](configs/stuttgart.yaml). `--config` accepts either venue name or a
path to a YAML/JSON file. YAML is loaded with `safe_load`; configuration is data,
not executable Python.

## Staging JSON contract

[staging.schema.json](staging.schema.json) is JSON Schema draft 2020-12, generated
from the Pydantic BaseModels in [staging.py](staging.py). The importer and scraper
use those same models. Regenerate after changing the contract:

```bash
uv run python -m backend.ingestion.scrape schema
uv run python -m backend.ingestion.scrape validate path/to/productions.json
```

The root is a **non-empty array of productions**, retaining existing Karlsruhe
snapshots' shape:

```json
[
  {
    "title": "Example Play",
    "url": "https://theatre.example/productions/1",
    "premiere": "2026-10-16",
    "duration": 170,
    "age_rating": 14,
    "tags": ["drama"],
    "critics": [],
    "author": "Example Author",
    "memberships": [
      {
        "name": "Example Person",
        "type": ["actor"],
        "url": "https://theatre.example/people/1",
        "urls": [],
        "role": "Hamlet",
        "character": "Hamlet",
        "description": "A source biography.",
        "born": "1980-01-02",
        "died": null,
        "nationality": null
      }
    ]
  }
]
```

Only production `title`/`url` and membership `name` are mandatory. Unknown keys,
database IDs, invalid URLs/dates, negative durations/ages and non-integer numeric
fields are rejected before creating any source or review cases. Optional fields
may be omitted; nullable fields may be `null`. Person `type` accepts the current
`Person.type` list (non-empty), or a legacy scalar such as `"director"`.
An omitted/null type retains the importer's role-derived type (or `actor` when
both type and role are absent).

Mapping to current [domain models](../models.py):

| Staging | Domain / review behavior |
| --- | --- |
| `title` | `Production.name` and a proposed `Text.title` |
| `description`, `premiere`, `duration`, `age_rating`, `tags`, `critics`, `url` | Production fields; premiere accepts ISO date or RFC3339 datetime with timezone |
| membership `name`, `type`, `description`, `born`, `died`, `nationality` | Person fields |
| membership `url`, `urls` | Combined into `Person.urls`; first URL identifies the source profile |
| membership `role`, `character` | Canonical production credit; actor character stays separate |
| `author` | An author credit if not already represented in memberships |

Organization/source identity comes from the config and explicit import options,
not embedded database foreign keys. Unlinked people remain source assertions
scoped to the production; names are not globally unique identities. A standalone
`page --kind person` result is a person object for inspection, **not** an importable
production array. Linked biographies are copied into production memberships by
`scrape`, without replacing credit names or inferring identity from profile text.

## Exactly one page: inspect selectors

`page` fetches **only the requested URL**, even when a production links to dozens
of people. It never fetches a listing or follows profile links. HTTP redirects on
the configured origin are allowed and bounded.

```bash
uv run python -m backend.ingestion.scrape page --config karlsruhe \
  --url https://www.staatstheater-karlsruhe.de/programm/info/3899/ \
  --output data/single-production.json

uv run python -m backend.ingestion.scrape page --config karlsruhe --kind person \
  --url https://www.staatstheater-karlsruhe.de/ensemble/id/3667/

uv run python -m backend.ingestion.scrape page --config stuttgart \
  --url https://www.schauspiel-stuttgart.de/spielplan/premieren/koenig-richard-der-dritte/

uv run python -m backend.ingestion.scrape page --config stuttgart --kind person \
  --url https://www.schauspiel-stuttgart.de/spielplan/premieren/koenig-richard-der-dritte/max-simonischek/
```

URLs are examples checked during implementation; a venue may later remove them.
Use a current detail/profile URL when that happens. Missing required selectors,
HTTP failures and invalid extracted values are errors, never successful empty
snapshots. Stuttgart's `ab Klasse 10` is a school grade, not age 10.

## Check whether one page changed

```bash
uv run python -m backend.ingestion.scrape check --config karlsruhe \
  --url https://www.staatstheater-karlsruhe.de/programm/info/3899/
```

First run creates a local baseline under `data/scrape-checks/<source>/`.
Subsequent runs compare against it and show:

- whether raw HTML changed (including dynamic tokens/markup);
- whether extracted data changed, with old/new field values and person labels.

This also fetches **exactly one page**. Use `--kind person` for a profile, or
`--state-dir <directory>` to isolate another set of baselines. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Initial baseline, or no change |
| 1 | HTML and/or extracted data changed |
| 2 | Config, parsing, HTTP, validation or filesystem error |

`--data-only` ignores raw-HTML-only changes for the exit status, while still
reporting them. `--accept` shows the diff then advances the baseline; it still
returns 1 when changes were found. Without it, later checks are read-only.
Each config/kind/URL has its own baseline, so changing extraction rules starts a
new baseline instead of misrepresenting parser changes as website changes.
Baseline files contain extracted source text and hashes, not complete HTML.

## Bounded scraping and history

```bash
# Default: one listing and at most one production, then its linked profiles.
uv run python -m backend.ingestion.scrape scrape --config karlsruhe

# Test without following any people (listing + one production only).
uv run python -m backend.ingestion.scrape scrape --config stuttgart --limit 1 --no-people

# Larger bounded sample, only when intended.
uv run python -m backend.ingestion.scrape scrape --config karlsruhe --limit 3
```

Requests are sequential with a configurable one-second default delay, explicit
user agent and timeout. Profiles are fetched once per run even if a person has
multiple credits. Only the configured origin is allowed, including redirects.
Honor the venue's robots policy/terms and choose a suitable delay/user agent.
There is no automatic retry storm, external-domain crawl or database upsert.

Full scraping is opt-in via `scrape --all`; do not use it to test selectors.
Pagination follows only the configured `next_selector` and fails if `max_pages`
would truncate a snapshot. Failures abort without saving partial data or
advancing scrape history.

Successful scrapes save timestamped, same-day-safe arrays below
`data/staged/<source>/`. A readable summary reports added/removed/changed
productions, followed by changed fields and people. Ordering changes alone are
ignored. A comparison only uses previous snapshots with the **same config,
production limit and profile-enrichment setting**. A limited scrape never reports
the remainder of a full scrape as removed.

Compare any two production snapshots offline:

```bash
uv run python -m backend.ingestion.scrape diff before.json after.json
```

The former `python -m backend.ingestion.orgs.karlsruhe` entry point now delegates
to the shared config, supports `--section`, `--max`, `--no-people` and `--all`, and
**defaults to one production**, unlike the old unbounded default. The Python
`KarlsruheScraper` API remains available; `max_productions=None` explicitly means
all configured productions.

## Configure another venue

Copy a bundled YAML file, or express the same object as JSON.

- `source_id`: safe local directory name; `organization_name`, `base_url`: identity.
- `listing.urls`: overview URLs; `selector`: anchors with `href`; `include` and
  optional `exclude`: URL regexes. Optional `next_selector` selects the next-page
  anchor, with bounded `max_pages` (default 20). Relative URLs respect HTML `<base>`.
- `production`: staging field names mapped to extraction rules. `title` must have
  `required: true`.
- `credits`: repeated row `selector`, plus `name`, `role`, optional `url` rules.
  `carry_role` preserves table continuation roles, `actor_labels` removes labels
  like `Mit`, and `default_type` controls unknown roles. Set `infer_type: false`
  for actors so character names cannot be mistaken for crew roles.
  `role_types` adds regex ->
  canonical person-type overrides; known crew types otherwise use shared helpers.
  Multiple profile links and names must match one-to-one; ambiguous rows fail.
- `person`: membership person-field extraction rules; `name` is required. Set
  biography `required: true` when absent content signals a broken selector.

Field rules:

| Key | Behavior |
| --- | --- |
| `selector` | CSS selector, relative to the document or credit row |
| `attribute` | Read an attribute instead of text, e.g. `href` |
| `match` / `exclude` | Case-insensitive regex filters |
| `extract` | First capture group (or full match) |
| `strip` | Remove regex matches, e.g. Stuttgart's trailing `*` |
| `join` | Join text blocks, e.g. `"\n\n"` for descriptions; otherwise first match |
| `many` | Return all matching text/URL values as an array, e.g. `tags` or `urls` |
| `split` | Split selected text by regex into an array, e.g. comma-separated extras |
| `min_length`, `limit` | Filter short text / cap matching blocks |
| `stop_tag` | Ignore sibling nodes after this tag, e.g. description after `hr` |
| `required` | Error if no value was extracted |
| `format` | `text`, `date_de`, `duration`, `integer`, `author`, `url` |

`many`/`split` cannot combine with `join` or scalar numeric/date formats. Use
`many: true` on corresponding name/URL rules for multi-link credits.
Config typos, regexes, selectors and staging field names are checked before HTTP
requests. Use `page` for a production and one person before any listing scrape.

## Submit for review

After inspecting the diff and validating a production array:

```bash
uv run python -m backend.ingestion.staged_to_db \
  --json data/staged/staatstheater-karlsruhe.de/<timestamp-and-scope>_productions.json \
  --source-name staatstheater-karlsruhe.de \
  --source-base-url https://www.staatstheater-karlsruhe.de/ \
  --organization-name "Badisches Staatstheater Karlsruhe"
```

For Stuttgart use source `schauspiel-stuttgart.de`, base URL
`https://www.schauspiel-stuttgart.de/` and organization `Schauspiel Stuttgart`.
The filename's `YYYYMMDD_` prefix supplies the capture date. Files can also be
uploaded through **Verwaltung -> Datenabgleich & Duplikate**. Organization, person,
text and production cases require review in that order. New domain entities
still need explicit `allow_create=True` approval; scraping never approves cases.

## Tests

```bash
uv run pytest tests/test_configurable_ingestion.py tests/test_base_scraper.py tests/test_reconciliation.py
```

Tests use synthetic venue DOMs and a loopback HTTP server. They cover schema
generation/validation, current model fields reaching review, both venue parsers,
single-page/bounded requests, profile de-duplication, failures, baseline acceptance
and scoped snapshot diffs. They do not contact venue websites or run a full scrape.
