# Personal library, theatre diary, and sign-in

Status: first release implemented (`backend/library.py`, `frontend/auth.py`,
`frontend/views/personal.py`, `frontend/views/collection.py`,
`frontend/views/text.py`, migration `0011_user_library`). The sections below
are the full design target; the first release deliberately simplifies:

- Tables are `LibraryEntry`, `LibraryVisit`, `Playlist`, `PlaylistItem`. The
  OIDC identity is stored as `oidc:<iss>|<sub>` in `User.username`; no separate
  `UserIdentity` table, never matched by email.
- Wishes on a text always cover its work family; no exact/family scope switch.
- Visits store an optional production and day; no venue, date precision or
  submission-UUID dedup.
- Playlist names are unique per user after whitespace normalization (case
  sensitive); no reorder UI.
- Dev access is one flag, `THEATERDB_DEV_AUTH=1`, which grants a local admin
  and is refused with `THEATERDB_ENV=production`. No separate dev-admin flag.
- Admin gating is navigation-level (management pages are not registered for
  non-admins); backend service functions are not individually guarded.
- On catalogue merge, conflicting ratings keep the survivor's rating; different
  reviews are concatenated.
- Not built yet: data export, account deletion, active-state checks on callbacks,
  token `exp` rechecks, optimistic concurrency, confirmed performance dates.

## 1. Recommendation

Build a small private library inside the existing Streamlit app:

- Mark a text as read, a text or production as watched, and either as wanted.
- Record repeat readings and theatre visits, with optional dates and venues.
- Rate texts and productions separately with one to five stars and write a
  private review (Rezension).
- Create named, ordered playlists mixing texts and productions, such as
  **Must see**, **Mit Freunden**, or **Noch einmal**.
- For a wanted text, show productions of that text and, by default, its known
  translations/adaptations. Label upcoming and recent premieres honestly.
- Keep browsing public. Sign-in is needed only to save personal data or edit
  the shared catalogue.

Use native Streamlit OIDC authentication and widgets, existing SQLModel sessions,
Alembic migrations, date precision, entity redirects, and text-family helpers.
Do not add an API server, auth framework, recommendation service, or frontend
component package.

**Important boundary:** the database currently records premieres, not individual
performances or production runs. A recent premiere is not proof that a theatre
is still playing a production. The first release must not say otherwise.
Section 6 defines the separate, evidence-backed performance extension needed
before promising actual future show dates.

## 2. Existing code and required reach

| Existing resource | Reuse or necessary change |
| --- | --- |
| `frontend/app.py` | Replace unconditional mock identity; add login/account area, personal navigation, and admin-only management navigation |
| `backend/models.py` | Reuse `User`, `Text`, `Production`, `Organization`; add personal tables and real target foreign keys |
| `backend/user_management.py` | Keep database/session infrastructure; despite its name it does not implement user authentication |
| `backend/texts.py` | Reuse `work_family`, `family_roots_many`, title and credit logic; never invent a canonical work ID |
| `backend/date_precision.py` | Reuse calendar dates, precision, intervals, and formatting for diary dates and premiere classification |
| `backend/reconciliation.py` | Reuse redirects; extend merge preparation and separate personal references from editorial previews/audit |
| `backend/entity_management.py` | Extend text browsing and block catalogue deletion that would destroy personal data |
| `backend/config.py` | Add public text discovery; do not conflate discovery configuration with permissions |
| `frontend/views/detail.py` | Add explicit text dispatch; its existing final branch renders a person |
| `frontend/views/production.py` | Add compact personal actions; turn text foundation into a link to public text details |
| `frontend/views/organization.py` | Reuse production cards, venue context, source links, and date labels |
| `frontend/views/discovery.py`, `frontend/views/helpers.py` | Reuse pagination, entity links, escaping, and result cards |
| `frontend/management/*` | Guard every editor entry point and interactive mutation, not just navigation |
| `backend/migrations`, `backend/db.py` | Add reviewed migrations, including identity compatibility and constraints |
| `tests/test_detail_discovery.py`, `tests/test_production_person_views.py`, `tests/test_text_detail.py`, `tests/test_reconciliation.py` | Extend existing behavior tests where affected |
| `docker-compose.yml`, `deploy/compose.production.yml`, `.gitignore`, `specs/development.md` | Document/mount runtime OIDC secrets; enforce production dev-access prohibition |

Add only a small auth boundary, a personal-library service, a reusable personal
actions renderer, and public text/personal-library views where existing modules
do not cover them. Keep catalogue import workers and CLI workflows operational.

Two current gaps are release blockers, not patterns to retain:

1. Every anonymous visitor currently gets the same mock identity without an env
   flag. That must become a real anonymous state.
2. Management pages currently appear for everyone. Signing in must not make an
   ordinary reader an editor.

## 3. Text, work family, production, and experience

### Identity rules

- A **text** is the existing readable/licensable version, including its language
  and translation/adaptation identity.
- A **work family** is computed from `translation` and `adaptation` edges using
  `work_family`. `based_on` remains a different work. A family can have multiple
  roots; a root ID is not a permanent family ID.
- A **production** is the existing staging, with its own theatre, cast, and
  premiere. It is not an individual performance.
- An **experience** is the reader's reading or viewing. It is personal evidence,
  not a shared programme announcement.

Never match personal records or recommendations by title alone. A user selects
an existing catalogue text/production, disambiguated by author, language,
theatre, director, and premiere where available. Recording a visit without a
known production must not create a guessed production.

### What each action means

| Action | Text | Production |
| --- | --- | --- |
| Want to see | Interest anchored to this text; default discovery scope is its work family | Interest in this particular staging |
| Read | Reading of this exact text version | Not offered; follow its text link |
| Watched | Viewing without a known production, or a summary of linked production visits | Viewing of this staging |
| Stars/review | Opinion of this exact text version | Opinion of this staging |
| Add to playlist | Add this text | Add this production |

Read and watched are independent facts, not mutually exclusive statuses.
Want-to-see is independent too: a person may want to see something again.
Saving a reading, visit, rating, or review does not silently clear a wish,
remove a playlist item, or infer another action. The form may offer an explicit
unchecked **Von Merkliste entfernen** option.

The text page distinguishes **Diese Fassung gelesen**, **Diese Fassung gesehen**,
and **Andere Fassung gesehen**. A production visit can derive a viewed badge
through its current `text_id`, but never writes a read marker or text rating.
Family summaries may report related viewing; exact-version reviews remain
separate. Removing the last qualifying experience removes the derived badge.

When a known production is selected from a text's watched action, save the
experience against that production's entry. If its text is another family
member, identify that version before saving. If no production is known, save
against the selected text, with optional venue context. Unrelated productions
cannot be silently attached to the text being viewed.

Family membership is computed from current catalogue data. Changing lineage
changes discovery and family summaries, not the stored reading/review target.
Offer **Nur diese Fassung** versus **Auch Übersetzungen/Bearbeitungen** for a
text wish. Do not copy the wish into every current family member.

### Experience details

- One watched/read action opens a small native form. Date is optional; do not
  default an unknown historical visit to today.
- Accept unknown, year, month, day, or minute precision using existing helpers.
  Display the precision actually supplied, not filled-in January 1 or midnight.
- Reject dates wholly in the future for completed experiences. A year/month
  overlapping today is valid; do not reject a whole current year as future.
- On a production, preselect its organization as the venue when known, but
  allow another existing theatre or a private free-text venue for touring.
- A text-only viewing may select an existing theatre or a private venue label.
  Reading has no theatre field.
- Venue name and production are optional. An unknown visit remains useful.
- Show repeat experiences chronologically, unknown dates last. **Weiterer
  Besuch** / **Erneut gelesen** explicitly creates another experience.
- Editing/removing one experience must not remove others or its entry's review.
  Removing all personal data for an entry requires separate confirmation.

## 4. Private data model

Use UUID primary keys and server-assigned UTC `created_at`/`updated_at` timestamps.
Calendar-domain dates retain source-local wall time as the existing date module
does; do not treat them as UTC audit timestamps. All new fields are validated
at backend boundaries and, where possible, by database constraints.

### `User` and `UserIdentity`

Reuse `User.id`, `display_name`, `is_active`, and `is_admin`.

- Make `password_hash` nullable for SSO users. Preserve existing hashes; there
  is no local-password login, reset flow, or dummy hash.
- Keep `username` as an internal unique handle for compatibility, generated
  from the internal UUID for new users. Do not require usernames at login.
- Existing unique `email` is optional. Do not require email for authentication
  and do not populate it automatically on new SSO accounts: duplicate provider
  emails must not cause account collisions or account linking. Existing values
  are preserved, and contact-email management is outside this release.
- New `UserIdentity`: `id`, `user_id` FK, `issuer`, `subject`, timestamps;
  unique `(issuer, subject)`, with nonblank issuer and subject.
- The stable identity is the validated OIDC `(iss, sub)` pair, not an email,
  display name, provider button label, or incoming user ID.
- First login transactionally creates one user and identity. Concurrent first
  logins resolve to the same identity, not two accounts.
- Protected operations only resolve an existing account; they never provision
  one. If an account disappears while another tab is open, invalidate that
  tab's bound principal and require an explicit new login, not automatic
  account recreation from its stale identity cookie.
- New users are active non-admins. Returning inactive users cannot access
  personal data or mutate anything.
- Existing users require an operator-reviewed identity attachment. Never link
  legacy users by matching email. Multiple provider identities may reference
  one user only after explicit operator verification; no self-service linking
  in the first release.

### `UserEntry`

One person's durable personal overlay for one catalogue target:

| Field | Contract |
| --- | --- |
| `id`, `user_id` | UUID identity; owner FK |
| `text_id`, `production_id` | Exactly one nonnull FK |
| `want_to_see` | Boolean, default false |
| `wish_scope` | `exact` or `family`; production entries must use `exact` |
| `rating` | Nullable integer, 1 through 5 |
| `review` | Nullable plain text, max 10,000 characters; blank normalizes to null |
| `created_at`, `updated_at` | Server timestamps |

Use partial unique indexes on `(user_id, text_id)` and
`(user_id, production_id)` where the corresponding target is nonnull.
Explicit XOR and rating/scope checks work on PostgreSQL and SQLite.
Do not use a generic `entity_type/entity_id` pair without FKs, JSON preferences,
duplicated titles, or a separate stored work entity.

Create an entry only on an explicit personal save. An entry may remain after
its last wish/review/experience is removed; it is not itself a visible mark.
No cleanup job is needed. Never expose entry IDs through public entity graphs.

### `UserExperience`

| Field | Contract |
| --- | --- |
| `id`, `entry_id` | UUID; FK to personal entry |
| `kind` | `read` or `watched` |
| `occurred_at`, `occurred_at_precision` | Optional validated calendar date pair |
| `venue_id`, `venue_label` | Optional organization FK or private text, not both |
| timestamps | Server-assigned |

Read experiences require a text entry and no venue; watched experiences may
refer to either kind of entry. Enforce cross-table rules in the service.
Limit private venue labels to 200 characters. Never rewrite an old venue just
because a production's owning theatre changes; it describes that visit.
Do not make `(entry, date)` unique: two performances on the same day are valid.
Native form submissions use a session-scoped submission UUID as the experience
ID, retained until commit, so retrying the same save cannot create another visit.
An explicit new experience gets a new UUID.

Read/watched badges are derived from experiences. There are no duplicate
`has_read`/`has_watched` columns to drift out of sync.

### `Playlist` and `PlaylistItem`

- `Playlist`: `id`, `user_id`, `name`, timestamps. Require a trimmed name of
  1-100 characters, unique per owner after case-insensitive normalization using
  the existing normalization helper. Reject an empty normalized name and store
  the normalized name for uniqueness.
- `PlaylistItem`: `id`, `playlist_id`, `entry_id`, nonnegative integer
  `position`, timestamps; unique `(playlist_id, entry_id)`.
- An item and its playlist must belong to the same user; enforce under a lock
  in the service. Index `(playlist_id, position, id)` for stable ordered reads.
- Add appends; adding the same target again is idempotent. Reordering uses
  native up/down controls, locks the playlist, and renumbers in one transaction.
  No drag-and-drop dependency or order uniqueness is needed.
- Membership does not set `want_to_see`; a **Favourites** playlist is not a
  wish list. Removing an item does not delete its review, visits, or entry.
- **Merkliste** and **Tagebuch** are computed views, not special seeded
  playlists. Do not create a default **Must see** list for everyone.

Every owner-scoped read starts with the authenticated user's ID. Reads and
writes verify ownership of indirect IDs too; a foreign playlist/entry/
experience ID is never sufficient permission. Owner/identity fields are not
editable widget fields.

## 5. Stars and reviews

- Use `st.feedback("stars")`, mapping widget `0..4` to stored `1..5`.
  `None` means unrated; never interpret zero as absent.
- Put stars and review in one `st.form` with an explicit save. Load existing
  values before rendering; do not save on rerender or every keystroke.
- A rating may exist without a review, and a review without a rating.
  Neither implies having read or watched.
- Clearing stars clears only the rating. Clearing review clears only review.
- One current rating/review per user and exact target is enough. Multiple
  visits do not create multiple votes; editing replaces the current opinion.
- Label text versus production reviews clearly. Do not average them together
  or put user reviews into `Production.critics`, which is catalogue/source data.
- Reviews, stars, diaries, and playlists are private in this release. Render
  reviews as plain text; no raw HTML, uploads, or rich-text editor.
- Do not publish community averages or reviews under an ambiguous privacy
  default. A later public-review release needs explicit opt-in, pseudonyms,
  moderation/reporting, withdrawal, and aggregation rules first.

## 6. Finding theatres for a wanted text

### First release: honest discovery using existing data

On the public text page and its personal wish detail:

1. Resolve the anchor through existing entity redirects.
2. Compute selected exact/family text IDs using existing helpers.
3. Query linked productions and organizations once for the selected page;
   deduplicate by production ID, not title.
4. Display title, text version/language, theatre/city, premiere precision, and
   the existing safe production/programme/source links.
5. Classify a premiere's half-open date interval relative to the current
   calendar day in `Europe/Berlin`, with time injectable in tests:
   - **Kommende Premiere**: interval starts on/after tomorrow.
   - **Kürzlich Premiere**: only if the complete interval is between
     `today - 180 days` and tomorrow. This includes a known premiere today.
   - **Datum ungenau**: interval overlaps either boundary without being wholly
     classifiable. A year-only date must not become a precise booking promise.
   - **Frühere Produktion**: complete interval ends at/before the recent window.
   - **Datum unbekannt**: no premiere.
6. Show upcoming first ascending, recent next descending, then imprecise/
   historical/unknown through an expander/filter. Break ties by stable UUID.
7. Show **Premierendaten, keine bestätigten Aufführungstermine. Spielplan beim
   Theater prüfen.** near these results, not only behind a tooltip.

Call the section **Wo wird der Text inszeniert?**, not **Jetzt im Spielplan**.
Offer existing city/theatre filtering and native pagination, 12 results per
page. Missing dates or links remain visible as unknown; no results means
**Keine passenden Produktionen erfasst**, not **Wird nirgends gespielt**.
User venue notes do not become public programme evidence.

Do not use the current season check, editorial verification, premiere, recent
crawl timestamp, or absence from an export as proof of a future performance.
Do not fetch theatre sites on page render.

### Follow-up required for actual future/recent performances

Before displaying **Nächste Aufführung** or **Spielt aktuell**, add a shared
`Performance` record only when a verified source/import provides a show:

- UUID; production FK; source FK and stable source event key, unique together.
- `starts_at` as timezone-aware UTC, plus IANA display timezone. Require an
  actual dated time with known timezone; do not pass this through the existing
  offset-discarding calendar-date parser.
- Optional actual venue organization; safe source/ticket URL.
- State `scheduled`, `cancelled`, or `postponed`; source observation timestamp.
  Postponement does not imply a new date until the source supplies one.
- Keep date-only programme announcements as dated evidence until the importer
  can support them explicitly; never fill midnight as a show time.
- Stage and review conflicts using existing ingestion/reconciliation patterns.
  Enrich one verified theatre adapter and fixture before generalizing.
  Preserve records missing from later crawls; do not infer cancellation.
- Future results require scheduled performances with `starts_at >= now`;
  recent results use the prior 180 days, excluding cancelled/postponed shows.
  A past scheduled record means **Vergangener angekündigter Termin**, not proof
  that it happened. Always show source link and checked-at date.
- Multiple shows deduplicate production cards, showing their next qualifying
  performance. Premiere hints remain a separately labelled fallback.

This extension is not a dependency for saving wishes and recording personal
visits. It is a dependency for the stronger claim that theatres will actually
play a wanted text on specified dates. It needs its own reviewed migration,
source fixture, and import/UI acceptance tests.

## 7. Authentication proposal

### Use Streamlit's existing OIDC support

Recommend Google OIDC for the first public provider, configurable through
Streamlit secrets rather than provider-specific application code. It removes
password storage/reset work and uses already installed `streamlit` and
`authlib`. Use `st.login`, `st.user`, and `st.logout` directly.

An existing organizational OIDC provider can replace Google with the same
boundary. If broader social/password/passkey choices are needed later, connect
one managed OIDC broker such as Auth0 rather than implement those flows.
Self-hosted Keycloak adds operational work; do not introduce it just for this
feature. GitHub OAuth is not interchangeable with OIDC and is not a drop-in
Streamlit provider.

Do not decide production provider accounts, credentials, costs, consent/privacy
terms, or migration bindings on the user's behalf. Google is a proposal pending
operator approval. Personal features remain unavailable until auth is configured;
public browsing remains available with a concise explanatory message.

### Configuration contract

Example only, with placeholders, in the ignored runtime
`.streamlit/secrets.toml`:

```toml
[auth]
redirect_uri = "http://localhost:8501/oauth2callback"
cookie_secret = "REPLACE_WITH_A_STRONG_RANDOM_SECRET"

[auth.google]
client_id = "REPLACE_WITH_CLIENT_ID"
client_secret = "REPLACE_WITH_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

Use separate local and production OAuth clients. Register exact callback URLs;
production uses HTTPS and the production origin. Keep secrets out of the image,
repository, examples with real values, and logs. Mount a read-only secret file
into the app container; the worker does not need it. The current Docker image
does not include runtime auth secrets.

Accept only configured, library-validated issuers and nonblank subjects.
Streamlit/Authlib handles token validation, state, and nonce; do not write a JWT
parser or custom cookie. Do not expose access/ID tokens or request API scopes.
If required claims are missing, show an explicit authentication error rather
than guessing identity. Provider failures never activate development access.

Streamlit's identity cookie lasts 30 days and is separate from token expiry.
Require an unexpired validated `exp` claim for personal/editor actions; an
expired session asks for login again. Recheck user active/admin state on each
protected operation, not just at login. Do not claim immediate cross-tab logout:
other existing Streamlit sessions can remain logged in. Logout clears the
current personal widget/draft state and uses native logout; it may not log out
of the provider, depending on provider metadata.

### Authorization is not SSO

| Visitor | Public browse | Own library | Shared catalogue management |
| --- | --- | --- | --- |
| Anonymous | Yes | Login nudge only | No |
| Active reader | Yes | Yes | No |
| Active admin | Yes | Yes | Yes |
| Inactive account | Yes, without personal identity | No | No |
| Explicit dev reader/admin | Yes | Own local test account | Admin only when explicitly enabled |

Use one small trusted principal carrying internal user ID and auth context.
Build it server-side from validated claims or explicitly guarded dev mode.
User IDs, role claims, query parameters, and session widget values supplied by
the browser cannot select an account or grant admin rights.

Hide management navigation for non-admins and guard each page before it reads
editorial data or executes actions. At the interactive backend service boundary,
require an active admin for create/update/delete/merge/review/upload/crawl
enqueue/cancel and quality actions. Hiding buttons is not authorization.
Pass `str(User.id)` through existing actor/audit conventions; `actor_id` itself
does not grant authority.

Catalogue primitives remain reusable by trusted CLI ingestion and workers.
Explicit interactive authorization wrappers may call them; do not break workers
by requiring Streamlit state inside backend SQL/import helpers, and do not treat
`actor_id=None` as an interactive permission bypass.

## 8. Development access, explicitly guarded

New env contract:

```dotenv
THEATERDB_ENV=development
THEATERDB_DEV_AUTH=1
THEATERDB_DEV_ADMIN=0
```

- Default environment is `production`; both dev flags default off.
- Accept only documented environment values (`development`, `test`,
  `production`) and flag values (`0`, `1`). Invalid configuration is an explicit
  startup error, not a truthy fallback.
- `THEATERDB_DEV_AUTH=1` is permitted only in `development` or `test`.
  Production plus either enabled dev flag must fail startup.
- Admin development mode additionally requires `THEATERDB_DEV_AUTH=1` and
  `THEATERDB_DEV_ADMIN=1`; invalid combinations fail.
- The production Compose override must explicitly set `THEATERDB_ENV=production`
  and both dev flags to `"0"`, overriding accidental root `.env` values.
- Use an isolated development database. Dev identity has a reserved local
  issuer/subject with no external email and one stable local user UUID after
  first creation. It must not attach to any real SSO identity or legacy account.
- Real OIDC identity wins when present. Logout in dev mode shows an explicit
  **Dev-Zugang aktiv** indicator because fallback access is still enabled.
- Display a persistent, visible development banner and reader/admin role.
  No personal name/email hardcoded in the app.
- Never turn dev access on because secrets are missing, login fails, or a
  request host/query parameter says localhost. Env settings are trusted operator
  configuration, not a browser-controlled bypass.
- Bind local no-SSO development to loopback. The flags do not prove that the DB
  or network is safe; do not run this mode on an internet-exposed deployment.

## 9. Streamlit UX and gentle login nudge

Use native navigation, containers, tabs, forms, dialogs/popovers, selectboxes,
date inputs, pagination, `st.feedback`, toasts, and Material icons. No custom
CSS/theme overhaul.

### Public detail pages

- Keep catalogue content visible without login.
- A compact action row shows want-to-see, read where applicable, watched, stars/
  review, and add-to-playlist. Selected actions have a visible selected state.
- Icons have meaningful German accessible names, not emoji-only/empty labels.
  Suggested labels: **Merken**, **Gelesen**, **Gesehen**, **Bewerten**,
  **Zur Liste**. Use `help` for extra context and text labels on small buttons
  where Streamlit cannot supply an equivalent accessible name.
- Tooltips are not substitutes for accessible names. Decorative Material icons
  do not need image alt text; actual images do.
- Anonymous personal actions open one compact native sign-in prompt:
  **Anmelden, damit Merkliste, Sterne und Rezensionen erhalten bleiben.**
  Do not block browsing, auto-redirect, repeat banners, or show fake success.
- Sidebar has one small **Anmelden** button/caption. Logged-in sidebar shows
  display name, **Meine Sammlung**, and logout, not email.

Native login starts a new session and returns to the homepage. Do not promise
that guest drafts or pending actions survive that redirect. The smallest first
release asks for login before editing; no guest library or automatic action
replay. If context restoration is added later, use only allowlisted internal
routes and an explicit verified cross-session mechanism.

### Personal library

One **Meine Sammlung** page with native tabs:

- **Merkliste**: wanted entries, exact/family indicator, and links to matching
  productions. A wish stays visible if no theatre is found.
- **Tagebuch**: readings/visits with dates and venues, filters for read/watched,
  and repeat-visit editing.
- **Rezensionen**: own rated/reviewed entries with target type and stars.
- **Listen**: choose/create/rename/delete playlist, add/remove items, reorder
  through native up/down buttons. Confirm list deletion; underlying entries stay.

Use stable widget keys including user UUID, target UUID, and form purpose.
Initialize from committed data; scope session drafts to that user and clear them
on account change/logout. Paginate diary/library results; do not fetch everyone's
data then filter in Python. After commit show one existing-style toast. On error
retain the form and show the error; never toast success before commit.

### Public text page

Add text discovery and an explicit read-only text renderer, reusing existing
title/credit/family helpers rather than linking readers to management. Show
version/language/author, related versions with clear lineage labels, production
links, source evidence, and the personal row. Ratings belong to the exact
displayed version. Reuse production cards for theatre discovery.

## 10. Integrity, privacy, merges, and deletion

- Personal overlays never modify `Text`, `Production`, credits, critics, sources,
  or editorial quality assessments.
- Do not add personal back-references to public catalogue relationships.
  Existing generic graph serialization must never traverse or return users,
  entries, reviews, diary rows, or playlist membership.
- Real FKs mean existing `_references` discovers personal rows. Its current
  `_state` snapshots and generic merge audit would expose/copy private data.
  Separate personal-reference handling before introducing the tables.
- Editorial previews may show aggregate impacted-row counts, not owner IDs,
  venue labels, reviews, stars, or personal snapshots. Keep a server-only
  revision fingerprint and locks to detect changed personal references between
  preview and apply without exposing their content.
- Do not write personal content or OIDC claims into shared SCD2 snapshots,
  reconciliation contexts, crawl artifacts, or logs. Reuse catalogue audit
  events for catalogue changes; personal edits use current rows/timestamps, not
  permanent review-history copies.

### Catalogue merge

Resolve redirected IDs on personal reads and writes. In the same existing merge
transaction, prepare personal collisions before generic FK reassignment:

1. For the same owner and merged target, coalesce entries if opinions are
   compatible: equal ratings/reviews, or one side absent. Keep the nonempty
   value. OR `want_to_see`; retain `family` if either text wish had that scope.
2. If both nonnull ratings differ or both nonblank reviews differ, block the
   entire catalogue merge with an aggregate conflict message. An editor must
   not choose, overwrite, or read another person's opinion. The owner can
   reconcile their existing entries using ordinary edit/remove actions before
   the editor retries. Never resolve this with "latest wins".
3. Move all experiences to the surviving entry, preserving IDs and repeat visits.
4. Move playlist items; same playlist/entry collisions keep one at the earliest
   position and compact ordering transactionally.
5. Reassign venue FKs during organization merges. Private venue labels are not
   guessed/replaced.
6. Preserve established redirects and catalogue audit behavior. Skip generic
   personal snapshots/audit when moving personal references.

No partial merge on conflict. Stable locking and unique indexes protect concurrent
personal edits. More advanced opinion conflict archives are deliberately deferred.

### Deletion and user control

- Catalogue deletion with personal target/venue references is restricted and
  explains that data is referenced, without exposing owners/content. Offer the
  existing reviewed catalogue merge path, not cascading loss of visits/reviews.
- Deleting a playlist removes only its items.
- Removing one experience affects only that experience.
- Clearing an entire personal entry explicitly removes its experiences, review,
  wish, and playlist memberships after confirmation.
- Provide **Meine Daten exportieren** with native JSON download for the owner's
  entries, precise dates, venues, playlists/order, and catalogue target IDs.
  Do not export credentials, provider tokens, or other users.
- Provide confirmed **Konto und persönliche Daten löschen**: remove own identities
  and all owned personal rows in one transaction, then native logout. It must not
  delete catalogue data or erase historical editorial actor IDs. Explain that
  signing in again creates an empty account and that dev mode can recreate its
  empty local account.
- Backups may retain deleted data until retention expires. Production rollout
  requires a documented retention period and privacy notice; do not claim instant
  backup erasure. Shared historic editor audits are a separate retention concern.

## 11. Error handling and concurrency

Use current explicit exceptions and Streamlit error/toast patterns:

- Invalid targets, forbidden ownership, invalid dates/ratings, inactive accounts,
  duplicate playlist names, and unavailable auth produce visible failures.
- A write transaction either commits the whole action or rolls back. Form state
  survives recoverable validation/database errors; secrets and private text do
  not appear in logs.
- Wish controls submit the desired boolean state, not an invert operation;
  retries, entry creation, and playlist adds are idempotent. Experience
  submission UUIDs make retries idempotent without forbidding repeat visits.
- Lock the entry/playlist for writes. Forms carry the committed `updated_at`;
  a stale save reports **Zwischenzeitlich geändert. Bitte neu laden.** rather
  than silently replacing another tab's edit. Timestamps come from the server.
- Include owner ID in every private query/cache key. Prefer no shared cache for
  personal data and no global cached ORM instances.
- Load family IDs once and bulk-load list-page context; avoid one family scan or
  personal status query per production card.

## 12. Delivery order and acceptance

### A. Authentication and public/editor separation

Remove unconditional mock; configure native OIDC, identity mapping, guarded dev
flags, reader/admin checks, and secret mounts/docs. Preserve public browsing and
trusted CLI/worker behavior. Establish personal-data-safe reference handling.

### B. Library and diary

Add reviewed schema migration, exact/family wishes, reading/visit forms, public
text discovery/details, private library, stars/reviews, playlists, export/delete,
and merge/delete protections. No feature is complete until its UI and backend
ownership boundary are both tested.

### C. Honest theatre discovery

Add precision-aware upcoming/recent premiere discovery using existing catalogue
data. Do not enable actual-performance language at this stage.

### D. Confirmed show dates, separately approved

Implement the performance extension only with a real supported programme source
and fixture. This is not a generic crawler rewrite.

### Focused automated checks

Use pytest, existing SQLite SQLModel fixtures, and Streamlit `AppTest`. Add
behavior tests rather than widget constants or generated accessor tests:

1. Anonymous browsing works; personal action nudges, writes nothing; anonymous/
   readers cannot invoke management mutations through direct routes.
2. Same validated issuer/subject returns the same user; equal emails at different
   identities do not link. New users are not admins; inactive/expired/malformed
   identities cannot save.
3. Dev flags off mean anonymous; production rejects enabled dev flags; explicit
   reader/admin development works; auth failures do not enable dev access.
4. One user's entry/playlist/experience IDs cannot be read or mutated by another.
   Public graphs and editorial previews contain no personal data.
5. Read, watched, and wanted remain independent; repeat visits preserve dates/
   venues; production viewing derives a text badge, never a text read/rating.
   Another version is labelled, not marked as that exact version.
6. Native star zero stores one, four stores five, and `None` clears rating;
   review-only/rating-only edits and cleared values persist after a new session.
7. Mixed playlists add idempotently, reject foreign entries, reorder persistently,
   and can be deleted without losing reviews or diary records.
8. Future, today, 180-day boundary, old, unknown, and coarse premiere intervals
   get the exact promised classification. Family includes translations/
   adaptations but excludes `based_on`; exact scope excludes other versions.
   Recent premiere never renders as a confirmed current performance.
9. Compatible merges retain experiences, opinions, wishes, and playlist order;
   opinion conflicts block atomically. Catalogue deletion cannot cascade away
   private data; user deletion/export touch only that user's rows.
10. Failed commits never show success; retrying one experience submission creates
    one row; explicit repeated visit creates another; stale edits are rejected.

Run affected existing view/text/reconciliation tests plus focused new auth/library
tests first. Then lint/type-check affected Python files with existing tooling.
Exercise the real migration on PostgreSQL: SQLite `create_all` does not validate
Alembic upgrades, partial-index migrations, locking, or existing-user migration.
Test two concurrent sessions against PostgreSQL for entry creation, playlist
reorder, identity creation, and catalogue merges.

Native OIDC redirects/cookies require a manual/browser smoke test against the
configured provider and deployed callback; `AppTest` is not a substitute. Verify
keyboard/screen-reader names and mobile layout for icon controls, not just their
visual appearance.

### Review decisions before implementation

- Approve first OIDC provider and operating/privacy terms; Google is recommended,
  not yet selected or provisioned.
- Approve private-only reviews/playlists for the first release.
- Approve premiere-based discovery as a clearly labelled first release; select
  a real theatre programme source before adding confirmed show dates.
- Set production backup retention and identify legacy-user/admin bindings.

## 13. Deliberately not built

No password handling, new auth dependencies, public social network, shared/
collaborative playlists, follower graph, AI recommendations, email reminders,
calendar/ticketing integration, automatic guest-draft migration, work table,
automatic email account linking, or generic performance crawler.

No arbitrary user-entered catalogue text creation. "General text" means selecting
an existing text rather than a specific staging; unknown visit venues can remain
private free text. A missing work follows the existing catalogue import/editor
workflow.

## Sources

Repository resources in section 2 describe the inspected implementation.
Native API behavior checked against the current official documentation:

- [Streamlit authentication](https://docs.streamlit.io/develop/concepts/connections/authentication)
- [`st.login`](https://docs.streamlit.io/develop/api-reference/user/st.login)
- [`st.feedback`](https://docs.streamlit.io/develop/api-reference/widgets/st.feedback)

Implementation must verify API behavior against the installed/locked Streamlit
release, especially identity expiry, logout, and accessible widget names.
