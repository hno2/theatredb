# Product specification

## Purpose

TheatreDB brings theatre organizations, productions, people, texts, and source
records together for research. The interface provides public browsing and
administrative tools for imports, review, and data maintenance.

## Main workflows

- Browse theatre and production details, with source links.
  Production details link back using the theatre name alone, show description
  and cast together, and preview up to five related productions per group (same
  text, theatre, or director). The same-text list expands when more are available.
  Text credits sit under the title. Related rows open
  individual productions; separate links open the theatre or director’s
  production list. The same-text and same-director cards include premiere
  counts and charts below their lists, with production titles on hover.
  Directly linked translations and adaptations have a matching card grouped
  by publication year. Sources appear at the bottom.
- Review theatre general information, scoped production-history coverage, and
  the current Spielzeit; browse assessment colours and filters on the overview.
- Search and filter productions across the full dataset.
- Upload theatre production JSON. Conflict-free changes can be applied
  automatically; ambiguous matches wait for review.
- Import Theapolis data incrementally. Existing conflicting values are kept,
  missing details and credits are added, and absent export records are not
  deleted.
- Review other-source imports and reported duplicates before accepting them.
- Manage and merge organizations, people, productions, texts, awards, and
  sources. Conflicting person or text fields require an explicit choice.
- Run source-owned crawls from **Verwaltung → Quellen → Crawler**.

## Ingestion

- Standalone theatre scrapes write staged snapshots, never directly to the
  database. Validate and compare snapshots before uploading.
- Theatre uploads match within the selected organization, validate the full
  file before writes, and are idempotent. Ambiguous production or text matches
  wait for review; missing credits do not delete existing data.
- Publisher text metadata may come from supplied records or HTML. No live
  publisher crawling is bundled. Known identities may fill blanks; changed
  non-empty metadata waits for review.
- Theapolis imports use source IDs prefixed by `directors:` or `creators:`.
  Default import includes German, Austrian, or Swiss organizations with at
  least two distinct productions in the snapshot; `--all` selects the full
  export. The full snapshot and source URLs are validated before writes. Imports
  run in batches of up to 500 organizations, people, then productions. Committed
  batches survive failures; rerun to resume without duplicates.
- Imports fill missing values and credits, preserve conflicting values in source
  observations, and never delete records missing from an export. Unchanged
  observations do not rewrite entities or credits; newer snapshots still
  advance observation timestamps. Embedded NULs are removed from text values
  with warnings, but NULs in source IDs reject the import.
- Theapolis intentionally merges director and creator records with exactly
  matching names, including records from earlier imports. This can merge
  different people who share a name; conflicting fields stop import for manual
  resolution. Compatible Theapolis-only groups merge in bounded transactions
  with references, source identities, redirects, decisions, and audit history
  preserved; groups involving other sources retain the reviewed merge path.
  Other-source matching uses its own review workflow.
- Theapolis premiere types (including `UA`) become production tags.
  Organization directory descriptions and public person biographies are
  downloaded and fill empty descriptions; supplied production descriptions
  follow the same rule. Missing descriptions are not invented.

## Managed crawls

- Each `Source` owns its crawler configuration and run history. At most one
  run per source can be active.
- Theatre crawls use the theatre import workflow; generic source crawls create
  review proposals. Unchanged payloads are skipped.
- Requests stay on the configured origin and are bounded and rate-limited.
  Successful imports, payload hashes, and run success commit together.
- Failed or cancelled runs do not publish partial domain data. HTTP cache pages
  may still be saved; crawl artifacts live under `data/crawls/`.

## Texts and works

- A **Text** is a readable or licensable version. Alternative titles and
  languages belong to that version; its display title is searchable too.
- `languages` is an ordered list: first language is primary, `[]` means
  unknown, and `mul` means multilingual or unspecified. Alternative titles may
  include a two-letter language code. Genres are `play`, `musical`, `opera`,
  `novel`, `film`, or `other`.
- Translations and published adaptations form a work family.
  **Vorlage / nach** links separate works, such as a novel and its musical.
- Self-links, duplicate links, and cycles are rejected. Director-specific
  production adaptations remain credits unless published as their own text.
- Text management shows credits, related works, versions, and productions.
  Ambiguous imported matches wait for review.

## Optional integrations

- Wikidata lookup is read-only. Choosing an item creates an explicitly
  identified stub; enrichment adds supported metadata.
- AI is off unless `AI_BASE_URL`, `AI_API_KEY`, and `AI_MODEL` are all set.
  Each AI action requires opt-in. Suggestions go through review and cannot
  create candidate IDs. Suggested titles stay unverified until a database or
  Wikidata label confirms them.
- Publisher extraction accepts supplied HTML and CSS rules. Site-specific
  crawling rules need verified fixtures.

## Data-quality assessments

The [data-quality specification](data-quality.md) defines editorial
verification, scoped production-history coverage, current-Spielzeit checks,
and overview map colours. Assessments are explicit, independently dated actions
with SCD2 history, not scores inferred from imports. Organization management
defaults to the current Spielzeit and reuses the theatre's programme URL as
evidence. Conflicting assessments require grouped choices during merges.

See the [ingestion guide](../backend/ingestion/README.md) and
[crawler guide](../backend/crawling/README.md) for operational details.
