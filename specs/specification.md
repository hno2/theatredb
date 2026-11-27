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
  Text credits sit under the title unless grouped with the same person's roles
  in the cast list. Related rows open
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
- Theatre uploads accept staged JSON or Open Repertoire v2 documents
  ([open-repertoire.md](open-repertoire.md)), match within the selected
  organization, validate the full file before writes, and are idempotent. Ambiguous production or text matches
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
- Theapolis intentionally merges people with exactly matching names,
  including records from earlier imports, unless the group has several distinct
  Theapolis profiles. This can merge different people who share a name. Texts
  merge on normalized title plus identical non-empty author set. Groups with
  conflicting fields are logged and left for manual resolution. Compatible Theapolis-only groups merge in bounded transactions
  with references, source identities, redirects, decisions, and audit history
  preserved; groups involving other sources retain the reviewed merge path.
  Other-source matching uses its own review workflow.
- Reviewed person merges also consolidate affected texts when normalized titles
  and complete, non-empty author sets match. Heading-case titles are preferred;
  productions, other credits, sources, redirects and audit history are retained
  in the same transaction. Conflicting text metadata is logged and left for
  manual resolution; different coauthors or non-author credits alone never
  establish a match.
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
- A text may have a `description` (blurb/synopsis). Imports fill it only when
  empty; it shows on the text detail page.

## Production taxonomy

- Controlled vocabulary in `backend/taxonomy.py`, synced into `Tag` on every
  migrate. Two layers: **Profil** (form, premiere, practice, audience, context)
  and **Inhalt** (theme, period, setting, reference). Inactive codes stay for
  history.
- `ProductionTag` stores one row per production and code with status
  (`published`, `suggested`, `rejected`), origin (`manual`, `source`, `model`),
  evidence, and audit fields. Legacy `Production.tags` are mapped through
  vocabulary aliases on flush; unmapped strings stay legacy-only.
- Precedence: manual decisions are never overwritten or deleted by source
  mapping; source rows follow the legacy tags; model rows are suggestions until
  reviewed. Merges keep the manual row when both productions have the code.
- Public filters and badges only use `published` rows.
- Codes whose meaning matches a GND subject heading carry its URI in `Tag.uri`
  (`GND_CONCEPTS`); codes without an exact equivalent stay local.

## Optional integrations

- GND (`backend/gnd.py`, lobid-gnd API, CC0) and Wikidata (`backend/wikidata.py`)
  are the authorities for people, texts and theatres, behind one provider
  interface (`backend/authority_match.py`). Selecting a person, theatre or
  family-root text in management looks both up automatically (linked record,
  else name search, cached for a day) and shows them side by side: a selectbox
  per authority (with "— nicht übernehmen —") and an "Eintrag öffnen" link. The
  chosen GND record's Wikidata link preselects that item. If the two records
  name each other they are shown as matching; if one names a different ID,
  applying is blocked. One click ("Bestätigen & übernehmen") links both IDs as
  reviewed `EntitySource`s in one transaction and fills blank fields only: person
  life dates, gender and up to 10 Latin-script same-surname variants; text
  creation/publication dates, genre (GND work form), titles, authors/composers
  and selected direct predecessors (`based_on`, one hop, one list for both);
  theatre founding and closing dates (GND predecessor/successor bodies are only
  reported); a blank description from Wikidata. Dates keep the most precise
  agreeing value whatever the source order (1749 and 1749-08-28 give the day);
  other differences are reported as conflicts. Same-name local records are reported, never merged.
- High confidence needs evidence beyond the name: a GND↔Wikidata cross-link
  (GND `sameAs`, Wikidata P227), or an exact name/title plus a matching life
  year, an author already linked to the same authority, or the theatre's city
  as GND place of business — and exactly one hit may pass. The panel marks
  such a hit; **Normdaten-Abgleich** runs this in bulk (batches, resumable),
  lets editors untick proposals, and applies them without lineage. Run people
  first so texts can use linked authors.
- The lineage form can still create a text stub from a Wikidata search hit.
- `Person.gender` is `m`, `f`, `none` (explicitly none), `div` or empty
  (unknown).
- AI is off unless `AI_BASE_URL`, `AI_API_KEY`, and `AI_MODEL` are all set.
  Each AI action requires opt-in. Suggestions go through review and cannot
  create candidate IDs. Suggested titles stay unverified until a database or
  Wikidata label confirms them.
- Publisher extraction accepts supplied HTML and CSS rules. Site-specific
  crawling rules need verified fixtures.

## Personal library and authentication

Signed-in users keep a private library: **Merken** (wish list), **Gelesen**
(texts), **Gesehen** (visits, optionally with production and date), 1–5 stars
with a private Rezension, and named lists such as **Must see**. Actions sit as
an icon row on text and production pages; **Meine Sammlung** shows Merkliste,
Tagebuch, Bewertungen and Listen. Anonymous visitors browse freely and get a
gentle login nudge. Sign-in uses native Streamlit OIDC; `THEATERDB_DEV_AUTH=1`
gives local admin access. Catalogue management is admin-only.

A wanted text lists productions of its work family (translations/adaptations)
grouped as upcoming, recent or past premieres. These are premiere dates, not
confirmed performances; real show discovery needs a source-backed performance
extension. Full design and deviations: [personal-library specification](user-library.md).

## Data-quality assessments

The [data-quality specification](data-quality.md) defines editorial
verification, scoped production-history coverage, current-Spielzeit checks,
and overview map colours. Assessments are explicit, independently dated actions
with SCD2 history, not scores inferred from imports. Organization management
defaults to the current Spielzeit and reuses the theatre's programme URL as
evidence. Conflicting assessments require grouped choices during merges.

See the [ingestion guide](../backend/ingestion/README.md) and
[crawler guide](../backend/crawling/README.md) for operational details.
