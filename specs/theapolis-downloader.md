# Theapolis downloader: findings and implementation plan

Status: resumable archive, database delta import, biography display, and API
cast/crew download implemented. The verified participation endpoint is
`/api/de/productions/{id}/participations`; this is separate from the
director/creator-only production detail endpoint. No full live backfill run.

Offline benchmark on 12,000 saved production rows: equivalent legacy JSON
pair 65.5 MB; seeded archive 17.9 MB (73% smaller). Second identical
snapshot added 1.86 MB of run/index metadata, and the unchanged-delta query
returned zero rows in 11 ms. This measures archive operations, not network
download or PostgreSQL import throughput.

## What exists today

Read-only audit on 2026-11-19: newest complete local snapshot was captured on
2026-11-18; newest Theapolis person and organization observations in local
Postgres were captured on 2026-11-07. Counts below describe those scopes, not
every entity in the DB.

| Data | Nov 18 export | Local Postgres and display |
| --- | --- | --- |
| People | 1,519 distinct matched profile slugs with a biography; some people have no matched profile or bio | 0 of 44,256 Theapolis-linked people have a description in either their canonical row or a current Theapolis source observation. The person detail page never renders `description`. |
| Organizations | 9,142 of 12,984 distinct referenced IDs have a nonempty raw description | 0 of 5,041 Theapolis-linked organizations have a description in either their canonical row or a current Theapolis source observation. The organization page already renders it when present. |
| Production descriptions | 0 of 126,774 list records have one | Import supports the field when supplied; cast backfill must not promise synopses. |
| Genre | 66,666 productions have a source genre label | Source genre is a `Production.tags` entry, not `Text.genre`. All 113,950 Theapolis-linked text rows have `genre` unset. |
| Credits | `directors` and `creators`, not the complete cast or crew | Theapolis-linked productions have 139,012 director and 143,194 author membership rows; none have a character. |

[Download and preparation](../backend/ingestion/theapolis.py#L175) convert source
HTML descriptions to Markdown. The importer can fill blank descriptions and
preserves nonempty canonical values. It gets organization descriptions from
directory pages and person bios from `biographyText` and `extraBiographyText`
on matched profiles. See [person header](../frontend/views/person.py#L30) and
[organization location section](../frontend/views/organization.py#L179).

Production tags come from `genre.name` (or a string), premiere `type.pill`
(fallback `type.name`), any source tags, and `Freie Darstellende Künste` for
`independent: true`. Exact duplicate labels are removed. A tag such as
`Schauspiel` describes the production; `UA`/`DEA` describe premiere type.
The [text import](../backend/ingestion/theapolis.py#L855) supplies only the work
title as a text field. Do not copy production classifications to work genre
without separate work-level evidence.

Indexed public pages for [Stereophonic](https://www2.theapolis.de/de/produktionen/138859-stereophonic)
and [LEGENDE](https://www2.theapolis.de/de/produktionen/108657-legende-1) show
cast and additional crew. A direct fetch of Stereophonic returned HTTP 403
during review; no bulk detail route or response schema was verified. In the
local list, 28,752 productions report more `numberOfParticipants` than the
number of listed director and creator entries. Treat this as a signal, not a
cast count or proof that other productions have complete credits.

The Nov 18 v2 pair totals **524,364,166 bytes** (~500 MiB). Its source file
repeats organization descriptions 117,550 times across productions; its
generic staged file repeats projected fields and bios already in the source.
The [reader](../backend/ingestion/theapolis.py#L775) loads both, rebuilds the
staged array for comparison, then prepares the import. The
[downloader](../backend/ingestion/theapolis.py#L463) rewrites both at
checkpoints rather than resuming from committed units.

## Implementation tickets

### 1. Recover existing descriptions and show person bios

- Add a `Biografie` section in [person detail](../frontend/views/person.py#L30)
  for nonblank `description` using native Markdown with unsafe HTML disabled.
  Leave organization rendering in place; test that a stored description reaches it.
- Test the Nov 18 v2 input on a disposable DB copy before any live backfill.
  Report in-scope descriptions, new fills, retained editorial values, missing
  profile matches and unresolved merge conflicts. Dry-run with the intended
  scope explicitly: default DACH excludes organizations outside DACH or
  with only one production, plus dependent records. Use `--all` only if that
  matches the intended dataset. Never clear a description on missing input.
- **Acceptance:** legacy replay fills blank descriptions, preserves manual
  descriptions, and both detail views render supplied Markdown. No request to
  Theapolis is needed for this backfill. Resolve or report any legacy
  name-merge conflicts rather than overriding them.

### 2. Verify and parse a permitted production-detail source

- Check provider terms and supported access first. Try a small authorized sample
  covering cast with characters, multiple roles for one person, repeat names,
  crew, and an explicitly empty cast. Prefer documented structured responses;
  use public HTML only if permitted and stable. Stop on access denial; ask
  the provider for a supported route or export rather than bypassing it.
- Save small synthetic/redacted fixtures. Verify the list ID matches detail
  identity. Record whether cast and crew were fetched separately from whether they
  were parsed successfully. Distinguish `not_requested`, `parsed`, `confirmed_empty`,
  `unavailable`, `fetch_failed` and `parse_failed`; missing sections or unknown
  schemas are not `confirmed_empty`. Store fetch time, source URL and parser version.
- **Acceptance:** fixtures produce exact credits, raw roles and characters, or
  an explicit failure state. No unverified URL guess in production code.
  If no permitted source is found, ship tickets 1 and 3 without claiming cast coverage.

### 3. Replace paired JSON with one resumable archive

- Implement one versioned SQLite archive under [ingestion](../backend/ingestion/),
  separate from application Postgres. Use transactional checkpoint segments.
  Keep an immutable `run` manifest (scope/query, start/end, parser version,
  expected/completed pages and detail coverage) and request checkpoints
  (request key, status, timestamps, HTTP validators, payload reference).
  Store compressed JSON values by digest of deterministic JSON; verify hashes
  on replay. Keep projections or indexes small: IDs, references and statuses,
  not copies of descriptions or page bodies.
- Store production-specific fields once per distinct observation; replace
  nested organizations, directors and creators with references to exact
  source-object versions. Store directory objects and profile payloads
  separately, with fetch state. Keep page-level fields and ordered record
  references so the original JSON values can be reconstructed, including
  unknown fields, null versus absent keys, repeated occurrences and conflicting
  values for one ID. Do not collapse versions just because IDs match. Apply
  Markdown conversion and DB-only cleaning at projection time. Scope raw
  detail retention to permitted production data; keep archives local and out of Git.
- Resume the same run from committed checkpoint keys only when scope and
  format match; retry failures, not successful units. Validate page coverage
  and apparent pagination drift before sealing. Promote only sealed,
  full-scope runs as the default import candidate; record coverage separately
  for optional details. A sample or interrupted run never becomes default.
- Read v1/v2 and v3 through one import boundary. Keep existing paired files
  read-only and preserve their validation rules and known legacy differences.
  Generate generic staged JSON only on explicit export for interchange; never
  use it as another maintained copy. No automatic archive deletion or v2
  rewrite. Update [ingestion CLI docs](../backend/ingestion/README.md#L140).
- **Acceptance:** stop/resume produces the same projections as a clean run
  against fixed fixtures; failed or interrupted runs cannot look complete.
  A v2 replay and equivalent v3 replay yield identical prepared records
  before adding new fields. Round-trip test covers unknown/null fields and
  conflicting nested versions. An unchanged run adds references and metadata,
  not another copy of every body.

### 4. Import complete credits without unsafe merges

- Normalize one credit shape in [Theapolis preparation](../backend/ingestion/theapolis.py#L665):
  production source ID, scoped person identity (or unresolved observation),
  raw and canonical role, optional character, and evidence reference. Use
  existing `actor` role and `ProductionMembership.character`. Use shared
  role normalization; do not turn crew into actors. Keep unrecognized labels
  in evidence and report them. Use list directors/creators as fallback
  evidence, deduplicating only verified overlaps with detail credits.
- Keep existing `directors:<id>` and `creators:<id>` source mappings. Add
  new aliases only with source-backed identity evidence. Never match new
  credits by display name, shared profile URL, or reused numeric ID alone.
  Exclude cast identities from legacy name merging; queue/report ambiguous
  cross-role links without dropping the raw credit. Keep snapshots replayable
  after a reviewer resolves a link.
- In [batch credit insertion](../backend/ingestion/theapolis_import.py#L556),
  compare `(production_id, person_id, canonical_role, character)` using
  consistent character normalization and retain distinct characters. Keep
  text contributions restricted to evidenced work credits; cast and crew
  belong to the production. Keep credit provenance in source context and raw
  evidence in the archive. Use existing audit and transaction paths. Never
  delete manual or previous credits on absence, errors or incomplete detail;
  report discrepancies for review. Test stale observations and replay after
  a partially committed import.
- Verify [production display](../frontend/views/production.py#L15) shows
  person, role and character. Expose incomplete coverage separately from
  “no cast”; reuse existing source UI rather than inventing an editorial
  verification flag. Preserve the existing genre tags. Defer typed production
  genre/filtering until there is a specific query or UI requirement.
- **Acceptance:** same person with two characters retains two credits; same
  credit twice yields one membership; namesakes stay separate; crew does not
  become cast or text authors. Replaying a snapshot adds no duplicate
  domain rows, links or audit events. Older/incomplete input never downgrades
  known credits or replaces editor values.

### 5. Benchmark and release

- Keep bounded concurrency, one HTTP session, provider rate budgets and
  retry/backoff with `Retry-After`. Checkpoint fetched items in batches, not
  by serializing the whole run. Stream archive reads and retain the batched DB
  importer. Build detail request keys from verified stable production IDs.
  Use HTTP validators where supported; do not assume production-list
  `updatedAt` tracks cast changes. Expire negative/ambiguous profile matches
  separately from transient failures. Match once per verified source ID,
  not once per credit.
- Start detail fetching with a small explicit scope. Prioritize newly seen
  productions and records with unexplained participant counts, then
  eventually check the remainder because the counter is not a completeness
  guarantee. Give detail requests their own budget; a normal list refresh
  must not trigger 126,774 detail requests. Report counts for requests,
  cache hits, failures, coverage and unresolved credits.
- Benchmark v2 vs v3 on the same list-only data and DB scope on a disposable
  copy. Record wall time, peak RSS, new disk bytes, decoding time,
  committed rows and HTTP request count. Initial release gates: archive
  size at most 50% of the v2 pair on equivalent data; no slower list-only
  import or higher peak RSS at median of three comparable runs; zero
  repeat detail requests for unchanged entries with valid cached details.
  Report first detail-backfill cost separately. Fix regressions or document
  measured trade-offs before switching the default.
- Order: ship biography display and test offline legacy replay first; verify
  detail access in parallel with v3 archive work. Then run a bounded detail
  backfill, compare list-only output and detailed credits on the DB copy,
  and review coverage/conflicts. Before live rollout, back up Postgres,
  confirm scope, and keep the old archives and reader. Rolling back the
  reader does not roll back committed credit or description changes; use
  the backup or a reviewed audited correction if needed.
