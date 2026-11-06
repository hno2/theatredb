# Theatre ingestion

The pipeline is **website -> validated staged JSON -> theatre upload (automatic for
conflict-free changes) -> review of conflicts**. Standalone CLI scraping, schema validation and change checks do not connect to
the database. Never use the historical Scrapy/DB scripts for ongoing imports.

For source-owned start/stop controls in Streamlit, persisted run history,
conditional HTTP fetching and automatic changed-record imports, see the
[background crawl architecture and setup](../crawling/README.md). Theatre YAML
and standalone commands below remain compatible; generic review-source crawls
use their own typed configuration and generic reconciliation rather than theatre uploads.

## Setup

From the repository root:

```bash
uv sync --group ingest --group dev
uv run python -m backend.ingestion.scrape --help
```

Bundled configs: [Karlsruhe](configs/karlsruhe.yaml) and
[Stuttgart](configs/stuttgart.yaml). `--config` accepts either venue name or a
path to a YAML/JSON file. Bundled venue names are case-insensitive (`Karlsruhe`
and `karlsruhe` are equivalent). YAML is loaded with `safe_load`; configuration is data,
not executable Python.

Shared inline lookup tables live in [backend/config.py](../config.py). Its
`ROLE_CONFIG` defines canonical role names, German display labels, exact aliases
and patterns for descriptive credits; scraping, imports and UI labels reuse it.
For example, `Vermittlung` maps to `theatre_educator`, not `actor`.
Lighting uses `light_designer`; translation and adaptation use `translator` and
`adapter` consistently. Venue YAML `role_types` overrides still take precedence
and remain local. Unknown role labels are retained by reconciliation and the UI;
scraping keeps its existing actor/default-type fallback.

Uploads also recognize `Vermittlung` in older snapshots whose type was inferred
as `actor`. Existing database records are not bulk-rewritten by this change.
Entity registries, field/status labels and other inline lookup tables share the
same central module; page-specific form configuration remains with its page.
German month numbers are derived from Babel's locale data in the ingestion
module, not maintained as a second handwritten calendar. This does not depend on
installed OS locales or change process-wide locale settings. Domain mappings
(roles, entity types, coverage fields and review statuses) remain explicit:
they encode application rules rather than generic locale data.

## Theapolis: separate directory import

Theapolis has one independent entry point, `backend.ingestion.theapolis`. It
uses the **same staging JSON schema** as all venue scrapers: a root array of
productions with `title`, `url`, `premiere`, and `memberships`. It does not use
venue configs, theatre uploads or their automatic matching/import rules.
API export and image downloads do not connect to the database. The separate
Theapolis import queues generic reconciliation cases only.


Re-export and reimport from the repository root:

```bash
uv sync --all-groups
uv run python -m backend.ingestion.theapolis download --concurrency 10
uv run python -m backend.ingestion.theapolis images --concurrency 10
uv run python -m backend.ingestion.theapolis import
```

To **create JSON only**, without downloading images or writing to the database:

```bash
uv run --group ingest python -m backend.ingestion.theapolis download --concurrency 10
```

Default staged JSON:
`data/staged/theapolis.de/YYYYMMDD_HHMMSS_microseconds_all-people_<config-hash>_productions.json`,
using the same timestamp/scope naming helper as venue scrapers. Each run keeps
earlier snapshots. Limited runs use `limit-<max-records>` instead of `all`
(`--max-pages 1` is at most 500 records). Default import selects the latest full
snapshot for the current API configuration, never a limited smoke export.
Image manifest:
`data/theapolis/images/manifest.json`. `--json PATH` selects another export/import
file. A sibling `<timestamp-and-scope>_productions.source.json` keeps raw IDs, organization metadata,
capture time and partial-export status outside the shared staging records.
Keep both files together for the separate Theapolis review import; it checks
that staged JSON matches its source metadata before writing any cases.
`images --output DIRECTORY` selects another image directory. Existing flat
`{"items": [...]}` exports (including `productions_new.json` and
`theapolis_export.json`) and API `{"data": {"items": [...]}}` envelopes are accepted.
For example:

```bash
uv run python -m backend.ingestion.theapolis import --json productions_new.json
```

Staged dates are normalized to a datetime plus a precision field; raw API IDs,
dates, logos, attribution and the timezone-aware capture timestamp stay in the
sidecar. `premierDate` accepts ISO year, month, day, or time, as well as
German `DD.MM.YYYY`. `premierYear` (or detail API `year`) supplies year-only
premieres. Conflicting date/year values fail validation before any review cases
are written. Old staging snapshots with `premiere_year` remain accepted.

Review under **Verwaltung → Datenabgleich & Duplikate → Weitere Prüffälle**.
Approve organizations and people, then texts, then productions. Match existing
records explicitly; creating a domain entity requires explicit confirmation.
Production credit IDs are kept as numeric source IDs, not inferred from names.
Profile slugs such as `vanessa-waldhart` are separate public profile identities;
they are not assumed to be the same as production-credit IDs. Missing organization
data stays unknown and requires manual review. Reimporting an unchanged snapshot
reuses the same cases. Export capture time is retained; legacy exports without a
timestamp remain undated. No automatic deletion or cleanup of legacy data occurs.

Theapolis organization/production URLs are **citations only**, never proposed
official website fields. Existing legacy Theapolis website values are displayed
as “Theapolis-Quelle”, not “Offizielle Seite”; no official theatre URL is erased.

Images are discovered independently from **all public people/organization
sitemaps**, not only people/theatres present in the production export. Downloads
use bounded parallel requests and retries for rate limits/transient errors.
Files are `people/<profile-slug>.<extension>` and
`organizations/<organization-slug>.<extension>`. Each manifest entry retains its
slug, source page, image URL, local path and download/no-image/failure status.
Missing photos are logged; other failures are recorded while remaining downloads
continue, then the command exits nonzero. Completed batches survive interruptions;
rerunning refreshes images. This downloads each public profile's main photo and
each organization's logo, not private media or entire photo galleries. Keep source
attribution and obtain appropriate rights before publishing downloaded images;
downloading does not grant a licence. Neither images nor snapshots are committed.

Bounded checks (do not run a full job for validation):

```bash
uv run python -m backend.ingestion.theapolis download --max-pages 1 --json data/staged/theapolis.de/smoke.json
uv run python -m backend.ingestion.theapolis images --limit 2 --output data/theapolis/smoke-images
uv run pytest -q tests/test_theapolis.py
```

`--max-pages` marks the source sidecar as `partial`; it never pretends to be a full
snapshot. `--limit` caps image profiles and reads only the first sitemap per kind.

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
        "born": "1980-01-02"
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

`premiere`, `born`, and `died` accept `YYYY`, `YYYY-MM`, `YYYY-MM-DD`,
`YYYY-MM-DDTHH`, or `YYYY-MM-DDTHH:MM`. Hour-only inputs become minute
precision with minutes set to `00`. Precision is inferred from the input.
Normalized output contains an interval-start datetime and `premiere_precision`,
`born_precision`, or `died_precision` (`year`, `month`, `day`, `minute`).
Snapshots created with the earlier `hour` precision must replace it with
`minute` and use `:00` minutes before upload.
Explicit precision must not be finer than the supplied date. A date represents a
half-open interval, not a claim that the event happened at its start; filters and
matching use overlap. Seconds and fractions in existing inputs are truncated to
minutes. Domain datetimes retain the source's local wall clock without a timezone;
any supplied UTC offset is discarded with a warning. Technical timestamps such
as capture and audit times remain timezone-aware. The manual upload sample at
`data/date-precision-example.json` illustrates each supported precision.
Generated scrape snapshots, page output (files and stdout), and page-check
baselines omit null-valued and empty-array fields recursively, including nested
memberships. Empty strings, empty objects, numeric zero and false are preserved;
input files with explicit nulls or empty arrays remain accepted. Array elements
and top-level arrays are not removed.

Extracted text removes discretionary soft hyphens (`U+00AD`, including HTML
`&shy;`), zero-width spaces and byte-order marks. Non-breaking spaces become
ordinary spaces. Titles, names, roles and other scalar text remain plain text.
Both bundled configs convert production descriptions and biographies to Markdown:
paragraphs, line breaks, emphasis, bold text, lists and links are retained.
Headings become ordinary paragraphs, never Markdown headings. Relative links
resolve against the page URL (or its HTML `<base>`); scripts and styles are
discarded. This cleanup applies to `scrape`, `page` and `check` extraction alike;
existing snapshots are not rewritten.

Mapping to current [domain models](../models.py):

| Staging | Domain / review behavior |
| --- | --- |
| `title` | `Production.name` and a proposed `Text.title` |
| `description`, `premiere`, `premiere_precision`, `duration`, `age_rating`, `tags`, `critics`, `url` | Production fields; date inputs support year through minute precision |
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
# Default: all configured productions and their linked profiles.
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

Scraping defaults to all productions; `--all` remains an explicit alias.
Use `--limit 1` or `page` to test selectors instead.
Pagination follows only the configured `next_selector` and fails if `max_pages`
would truncate a snapshot. Failures abort without saving partial data or
advancing scrape history.

Progress bars on stderr show listing pages fetched, production pages completed
out of the discovered total, and unique person pages completed out of their
total. `--no-people` omits the people stage. JSON printed by `page` stays on stdout.

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

The venue-specific Karlsruhe Python wrapper has been removed. Use
`python -m backend.ingestion.scrape scrape --config karlsruhe` instead, with
`--limit 1` for a bounded sample. To scrape a different Karlsruhe section, copy
the bundled YAML and change `listing.urls`.

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
  Text-empty credit rows (including non-breaking-space spacers) are ignored
  without resetting continuation roles. Non-empty rows missing names still fail.
- `person`: membership person-field extraction rules; `name` is required. Set
  biography `required: true` when absent content signals a broken selector.

Field rules:

| Key | Behavior |
| --- | --- |
| `selector` | CSS selector, relative to the document or credit row |
| `attribute` | Read an attribute instead of text, e.g. `href` |
| `match` / `exclude` | Case-insensitive regex filters |
| `extract` | First capture group (or full match) |
| `strip` | Remove regex matches, e.g. Stuttgart's trailing `*`; spans line breaks in Markdown fields |
| `join` | Join text blocks, e.g. `"\n\n"` for descriptions; otherwise first match |
| `many` | Return all matching text/URL values as an array, e.g. `tags` or `urls` |
| `split` | Split selected text by regex into an array, e.g. comma-separated extras |
| `min_length`, `limit` | Filter short text / cap matching blocks |
| `stop_tag` | Ignore sibling nodes after this tag, e.g. description after `hr` |
| `required` | Error if no value was extracted |
| `format` | `text`, `markdown` (rich text without headings), `date_de`, `duration`, `integer`, `author`, `url` |

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
uploaded through **Verwaltung -> Datenabgleich & Duplikate**. The theatre must
already exist (`--organization-name` or `--organization-id`). Conflict-free
same-title productions are merged, productions without a similar title (< 80 %)
in that theatre are created, the rest is queued under **Produktionskonflikte**.

## Tests

```bash
uv run pytest tests/test_configurable_ingestion.py tests/test_base_scraper.py tests/test_reconciliation.py tests/test_production_import.py
```

Tests use synthetic venue DOMs and a loopback HTTP server. They cover schema
generation/validation, current model fields reaching review, both venue parsers,
single-page/bounded requests, profile de-duplication, failures, baseline acceptance
and scoped snapshot diffs. They do not contact venue websites or run a full scrape.
