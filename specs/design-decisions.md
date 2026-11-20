# Design decisions

Short record of current design choices and constraints.

## Data integrity

- Keep each source record and its provenance. Apply unambiguous import changes;
  send ambiguous matches to review rather than guessing.
- Theapolis IDs keep director and creator namespaces separate. Its deliberate
  exact-name cross-role merge is an exception, not a general identity rule;
  conflicting fields require a human merge.
- Productions with different known premiere years are separate, even when
  titles match. Unknown years still use normal matching checks.
- Text versions are separate from works. Resolve lineage and translator
  metadata through shared title and identity checks.
- Reject invalid text-language codes, self-links, and lineage cycles rather
  than silently dropping data.

## Operations

- Alembic migrations own the PostgreSQL schema. Review generated migrations:
  autogeneration cannot infer renames, enum changes, or data migrations.
- Back up before schema changes. Reset is destructive and intended only for
  deliberate database recreation.
- Crawl imports, payload hashes, and successful run state commit together.
  HTTP cache pages commit separately and may remain after a failed crawl.

## Performance and UI

- Batch text-family roots and joined credits; build graphs per request rather
  than keeping a stale global ORM cache.
- Use `jsonschema` Draft 2020-12 for AI JSON, local references only, and reject
  non-finite numbers.
- Match credit separators before trimming whitespace. Share parsing logic to
  preserve Unicode behavior.
- Use native Streamlit tabs and run only the active review panel. Save edits
  before switching tabs; inactive widgets follow Streamlit cleanup.
- Combine homepage metrics into one SQL statement. Keep counts live instead
  of adding cache invalidation.
- Show all matching organizations on the homepage without pagination. Only
  organizations with valid coordinates appear on the map.
- Inherit text credits from ancestors in memory after one fetch. Do not scan
  all people to recover display spelling or treat same-name people as one.
- Reuse validated publisher metadata and identity lookups when saving, while
  keeping standalone saves validated too.
