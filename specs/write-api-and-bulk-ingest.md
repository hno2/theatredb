# Write API and bulk ingestion

Status: API and jobs proposed; import/reconciliation persistence boundary extracted.

## Extracted write boundary

[EntityWriter](../backend/writes.py) binds validation and SCD2 history to a caller's
session. Commands create or update rows through it, track direct domain mutations,
and finish only after relationship changes and ORM normalization. History is batched
once per changed identity, including person-to-text merge cascades; true no-ops
produce no update event. No command commits.

Session-bound entry points:

- [approve_case_in_session and merge_entities_in_session](../backend/reconciliation.py)
  retain reviewed-field choices, stale-preview checks, source collision checks,
  organization assessments, graph rewiring and redirects.
- [apply_production and resolve_production_case_in_session](../backend/production_import.py)
  retain theatre-scoped matching, fill-gaps/list-union policy and explicit conflict
  choices. `apply_production` consumes the existing prepared staging contract.
- [SourceObservation / record_source](../backend/writes.py) share accepted source
  persistence. Reconciliation rejects identity reassignment; theatre production
  application explicitly permits it. Older or undated observations cannot replace
  dated evidence or regress source timestamps. New observations retain archived
  merge provenance.

Existing approval/merge wrappers still own sessions and commit for current callers.
`ingest_records(session=...)` still leaves the entire import and `ImportRun` in the
caller's transaction, including crawler checkpoints. A caller must roll back the
whole action on a command failure; flushed changes are not a successful commit.

These are trusted in-process commands, not public CRUD or authorization boundaries.
Verified principals, resource-specific API schemas, row revisions, cross-writer
locking and durable import jobs remain prerequisites before exposing HTTP writes.

## Recommendation

Move write rules and transaction boundaries out of Streamlit, **not necessarily across HTTP**. Streamlit runs Python on the server: let it call the same backend commands as a thin FastAPI adapter, in process. Keep forms, field hints, previews, selection, and confirmation in Streamlit. Expose HTTP for scripts and integrations; keep CLI and workers calling Python directly. Move Streamlit to HTTP only if the UI must run without database credentials or be deployed independently of the write backend. Do not migrate discovery/read screens just to introduce writes.

Treat editor writes and ingest differently. An editor explicitly says what to change. An importer submits evidence that may match existing records, conflict with curated data, or need review. Sharing normalization, persistence, and history is good; sharing overwrite policy is not. Do not generate unrestricted CRUD, bulk upsert, or permission rules from every SQLModel table.

## What exists today

| Existing path | Reuse or change |
|---|---|
| [Entity management](../backend/entity_management.py#L403) validates rows and writes SCD2 history, but generic mutations open and commit their own sessions. [Streamlit's UI factory](../frontend/management/uifactory.py#L366) composes those calls for inline creation and relationship editing. | Extract session-bound commands. Make one editor action one transaction, including intentionally created dependencies, links, and audit history. Preserve UI behavior while replacing name-only “get or create” with explicit selection/creation or a proven unique identity. |
| [Text editor](../frontend/management/texts.py#L111) also flushes/commits directly. [Text services](../backend/texts.py) and [model hooks](../backend/models.py#L738) already handle title, credit, and lineage rules. | Move write orchestration (including text-family and production-link changes) into backend commands; keep hooks active and test resulting graph and history. |
| [Theatre import](../backend/production_import.py#L794) is shared by the [upload UI](../frontend/management/reconciliation.py#L135), [staged CLI](../backend/ingestion/staged_to_db.py#L40), and [crawl runner](../backend/crawling/runner.py#L319). It accepts a caller's session, records an `ImportRun`, and distinguishes created, merged, unchanged and review outcomes. | Use this as the first import adapter. Preserve scoped matching, source timestamps, fill-gaps behavior and review cases. Keep crawl fingerprints and import changes in the same success transaction. |
| [Generic source reconciliation](../backend/reconciliation.py#L453) already queues typed proposals with stable source identities; the crawl runner uses it for non-theatre records. [Theapolis](../backend/ingestion/theapolis.py#L1242) has specialized batch/checkpoint logic. | Reuse proposal handling for sourced bulk data outside theatre snapshots. Keep source-specific Theapolis behavior and checkpointing; do not make a generic HTTP endpoint for that pipeline or reimplement its matching. |

## Backend boundary

- **Commands** take a typed input, a verified principal, and a session. They validate domain state and permissions, resolve IDs, apply changes, and record provenance/history. They may flush, never commit. Reuse existing backend modules where they own the rules; use small shared helpers only for real duplication.
- **Transaction owners** are HTTP request handlers, Streamlit action wrappers, CLI entry points, and workers. A compatibility wrapper can keep existing call signatures during migration; never call a self-committing helper from inside a compound command. Materialize response DTOs before closing the session and return success only after commit.
- **Contracts** use resource-specific create, patch, and response schemas, separate from ORM rows. Share validation with the in-process callers; keep request/response formatting and HTTP error mapping in FastAPI. Reuse [date precision](../backend/date_precision.py), role/location normalization, [text rules](../backend/texts.py), and [quality actions](../backend/data_quality.py#L359). Reject unknown or protected fields; don't accept `actor_id`, audit data, quality flags, or ORM relationship attributes as editable fields.
- **History and concurrency:** use one authoritative write protocol for edits, import, and review. For HTTP patches, require a strong `If-Match` from an item `GET`; Streamlit passes the same expected revision from form load. Use a DB-maintained monotonic row revision for editable API resources so older ORM paths and bulk SQL also invalidate stale tokens; add this per resource before exposing `PATCH`. The token covers row fields only: child/link commands have their own revision or lock the parent and check an explicit expected relationship state for a collection replacement. Lock before read/compare/write, including conflict-sensitive importer and reconciliation paths. Audit in the same transaction, after normalization; avoid history entries for true no-ops. Review SCD2 data before adding uniqueness for `(entity_name, entity_id, version)` and one current event; constraints backstop locking, not replace it.

## API v1

| Route | Contract |
|---|---|
| `POST /api/v1/{collection}` | Typed create; `201` with ID, `Location` and `ETag`. Accept an idempotency key for retried creates (required for clients that retry automatically). |
| `GET /api/v1/{collection}/{id}` | Return editable representation and `ETag`. Add a bounded, cursor-paginated lookup only where clients need IDs; don't serialize ORM graphs. |
| `PATCH /api/v1/{collection}/{id}` | Omitted fields unchanged; explicit `null` clears nullable fields; supplied lists replace the entire field value. No implicit nested create or relation rewrites. `200` with new representation and `ETag`; `428` if precondition missing, `412` if stale. Use canonical IDs on writes; report merged IDs as a conflict with the replacement ID when authorized, not a silent write redirect. |
| `POST /api/v1/organizations/{id}/production-imports` | Submit a theatre production snapshot/delta for an **existing organization and registered source**. Request supplies `source_id`, `captured_at` with timezone if known, scope, and `records` in the existing [staging schema](../backend/ingestion/staging.schema.json). `202` with job ID and status URL; acceptance isn't application. Omission from a snapshot never implies deletion. |
| `POST /api/v1/record-imports` | One source and one supported resource kind per submission, with stable source record keys, optional observation time, and typed payloads. Queue proposals for canonical entities, not direct overwrites. Resolve references by explicit UUID or stable source identity; unresolved/ambiguous references go to review where supported, otherwise fail with a row error. |
| `GET /api/v1/imports/{id}` and `GET /api/v1/imports/{id}/items` | Status and committed outcome totals; cursor-paginated results with input record key/index, outcome, and permitted entity/review IDs. Errors include stable codes and field paths. Return summaries, not raw payloads or unbounded title arrays. |

`{collection}` denotes explicitly registered, typed routes, not an unrestricted runtime model lookup. Start with `organizations`, then `persons`, `texts`, `productions`, and `awards`. Theatre identity is `Organization.id`; production imports belong to one organization rather than inventing a parallel theatre model. Use documented Problem Details responses with field/row issues where useful: `401`/`403` for identity/scope, `404` for inaccessible or missing scoped resources, `409` for identity/state conflicts or key reuse with a different payload, `413` for size limits, `422` for invalid input, and `429` for quotas. No public `DELETE`, bulk destructive sync, or merge endpoint in the first version.

### Coverage beyond theatre productions

| Data | Add/edit | Bulk semantics |
|---|---|---|
| Organization, Person, Text, Production, Award | Explicit resource schemas. Reuse normalization and validate foreign references before writing. Display names/titles are not unique IDs. | `record-imports` uses `propose_record` for external evidence. Theatre production snapshots use the specialized endpoint even though standalone production proposals also exist. |
| Production and organization memberships, text contributions, titles and lineage, award nominations | Use typed parent-scoped commands/routes (e.g. `/productions/{id}/memberships`); validate link ownership, role, dates, title rules and lineage cycles. Keep individual edits separate from explicit collection replacement. | Accept bounded parent-scoped batches of explicit link operations once each relationship has rules for stable identity, duplicates and audit. Reuse [link identity definitions](../backend/config.py#L102) as a starting point, not proof of database uniqueness. No implicit creation of referenced people or texts. External link evidence without an unambiguous supported mapping stays in review or is rejected. |
| Source and provenance | Source registration/editing is admin-only and can follow core CRUD. Source claims and links are written through ingest/review commands, not arbitrary `EntitySource` or `SourceRecord` writes. | Start with registered sources. Defer bulk source provisioning; source registration and crawl authorization are separate. |
| Organization assessments and season checks, redirects, reconciliation decisions, import/crawl state, users and history | Keep dedicated workflows. No raw table mutation endpoints. | No generic import. Add workflow-specific batch actions only if a real use case and its permissions/invariants are defined. |

For curated spreadsheets that need bulk **edits** rather than evidence import, start with bounded batches of explicit relationship operations above. Add an equivalent typed editor batch for primary entities only if needed: each operation is create or patch-by-ID with expected revision, never upsert-by-name. Keep CSV parsing/mapping client-side initially; use the same validated JSON contracts for uploads and scripts.

## Ingest reliability and scale

1. **Submit durably.** Validate authenticated scope, envelope/schema, duplicate keys within a submission, and limits before accepting. Insert an import job, its immutable input reference/payload and an existing Procrastinate queue entry in one PostgreSQL transaction, following the [crawl queue pattern](../backend/crawling/queue.py#L36). Add a dedicated import task/queue, not fake `CrawlRun` records or in-process background callbacks. Keep `ImportRun` as theatre import history and link to it from the job. For bounded v1 payloads, store input with the job; choose external artifact storage only when measured sizes justify it.
2. **Distinguish two kinds of retry.** Require `Idempotency-Key` on job submission. Uniquely scope it by principal and operation/target; compare a canonical validated request digest. Same request gets the same job; different request gets `409`. Use this pattern for keyed manual creates too. Separately, use `(source, kind, stable record key)` for source identity, as in [SourceRecord](../backend/models.py#L261); use content fingerprints/effective observation metadata for unchanged detection without discarding meaningful timestamp changes. Never key by row position or normalized name. If a provider's IDs are only unique within a theatre or subgroup, qualify the key consistently and test collisions.
3. **Apply atomically per bounded submission.** Claim jobs with a lease; handle expiry/retry and terminal failure explicitly. Commit entity changes, history, review cases, outcome rows and job success together. A retried worker sees committed success and does not apply twice. Roll back the entire submission on a blocking error; `review` is a valid committed outcome, not a failure. Serialize conflicting import jobs for a source and theatre matching for an organization across sources; use consistent lock order and uniqueness for source identities. Include crawler and other overlapping writers in the relevant locking protocol before enabling concurrent API ingest. Revalidate references and authorization at execution; committed counts change only with commit.
4. **Bound work before optimizing.** Proposed pilot ceilings: 500 records and 5 MiB decoded JSON per job, with per-principal rate/queue quotas and separately bounded import worker concurrency. These are configuration starting points, not measured capacity. Larger datasets can submit multiple bounded jobs, labelled as independent commits, not a global atomic snapshot. If that volume becomes routine, add staged/streamed uploads and checkpointed chunks with explicit partial outcomes and resumability. If preview is added, run read-only matching and label it advisory; changes between preview and apply can change outcomes.
5. **Measure likely hot spots.** [Theatre matching](../backend/production_import.py#L728) scans candidate lists and can compare titles and query credits/text/source links per item. Batch-fetch referenced IDs, source keys and comparison data; index candidate lookups by organization, year and normalized exact title while retaining undated candidates and matching behavior. Reuse [batched SCD2 writes](../backend/scd2.py#L28) where appropriate; dedupe multiple changes to one identity in a transaction and don't replace ORM writes with raw bulk SQL if they depend on validation hooks. Use PostgreSQL query plans and before/after parity tests before changing fuzzy candidate selection. Budget connection pools across UI, API and workers; with synchronous SQLModel use synchronous request handlers or explicit thread offload, never block the ASGI loop.

## Access and deployment

- **Do not expose writes with the current mock identity fallback.** [App identity](../frontend/app.py#L7) supplies a mock for an unauthenticated user, and the [form actor helper](../frontend/management/uifactory.py#L113) extracts identity rather than enforcing authorization. Restrict mock mode to explicit local development. Apply the same deny-by-default authorization to Streamlit actions and API commands; derive the actor from verified user or service identity, not body fields. Use scoped bearer credentials for integrations (editor versus importer, narrowed by source/theatre where applicable). An importer cannot approve its own proposals through import endpoints. Limit status/result visibility to authorized operators. Do not log tokens or raw payloads; redact errors and set payload/result retention.
- Run FastAPI as a separate process/service in the same image and release, behind existing Traefik routing with an explicit `/api/v1` prefix and priority. Keep API private until auth and parity gates pass. This adds one service but isolates HTTP capacity and health from Streamlit. Mounting FastAPI with `st.App` is viable if single-process deployment is a hard requirement; it does not remove any auth, transaction or queue requirements.
- Update [dependencies](../pyproject.toml), lockfile, [Docker Compose](../docker-compose.yml), [production override](../deploy/compose.production.yml), and [release script](../deploy/deploy-production) so API/worker roll forward with the same digest, health checks and explicit migration order. Apply Alembic migrations once before serving new writes rather than letting new processes race schema setup. Readiness checks database/schema and required queue setup; disable API routing first on rollback and drain/pause import consumers before rolling back incompatible code. Prefer additive migrations and keep old input formats compatible during rollout.
- Leave Streamlit reads in place. Invalidate affected cached queries after in-process saves; for API/worker writes, use short TTLs initially and show refresh/status explicitly. Only add shared invalidation if observed staleness needs it. Poll import status from a Streamlit fragment rather than a blocking UI loop.

## Delivery order and acceptance

1. **Extract and test the write boundary.** Start with organization editing and theatre upload; preserve current wrappers for other callers. Add typed backend commands, auth checks and transaction scopes; migrate the UI factory's compound writes and text editor mutations as their resources move over. Characterize existing imports before changing result shape; keep title-list compatibility for current consumers.
2. **Pilot the vertical slice.** Enable private organization create/get/patch and theatre production jobs for one registered source. Validate same edit through UI/API, same production matching through upload/CLI/API, stale update rejection after a non-API write, and preservation of crawler checkpoints. Include a failed compound edit that leaves neither orphan rows nor audit rows.
3. **Expand by resource.** Add primary-resource schemas, source proposal jobs, then relationship-specific commands and batches. Cover text lineage cycles, partial-date round trips, duplicate link identity (including nullable components), reviewed-source protections, older snapshots, namesakes, and forbidden cross-scope references. Do not add a generic fallback endpoint for unsupported kinds.
4. **Verify on PostgreSQL before expanding exposure.** Test simultaneous edits, submissions and worker claims, crash before/after commit, same-key retry vs different body, transaction rollback, and unchanged reimports. Benchmark batches of 50 and 500 rows on small and large theatre catalogs with concurrent editor traffic. Record p95 editor latency, query count per job, memory, queue wait and failure rates; compare with the current direct-call baseline. Set rollout thresholds from those measurements; load limits and worker count are provisional until then. Instrument request IDs, job IDs, durations, outcome counts and retry rates.

Decisions before implementation: confirm who needs external access and source/theatre scopes; confirm typical and worst-case upload sizes; confirm whether curated spreadsheet edits are required in the first release or whether review-first ingest covers the need. Defaults in this plan are private access, bounded jobs, and review-first external data.
