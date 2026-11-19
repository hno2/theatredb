# Theatre data quality and season freshness

Status: implemented. Includes the current-Spielzeit indicator, independent
SCD2-audited assessments, and grouped assessment choices during merges.

## Goal

Let editors explicitly assess an organization's data and make that assessment
visible on the overview map, in the directory, and on the theatre profile.
Answer three different questions without inventing a misleading quality score:

1. Has the general information been checked?
2. For which period is the production history complete?
3. Has the programme for the current Spielzeit been checked?

An explicit "needs update" flag identifies known work independently of those
assessments. Missing assessments mean unknown, not incorrect.

## Product decisions

- Assess organizations, not individual productions or people, in this release.
- Verification is an editorial action, never a consequence of importing data,
  having many productions, or filling particular fields.
- General-information verification, history coverage, and season freshness are
  independent. History coverage can be recorded before general information is
  verified; that alone does not produce a green map marker.
- Production-history completeness means the list of productions is complete for
  the declared seasons, not that every cast, credit, or descriptive field is
  complete.
- "Current season checked" means the available programme was compared with a
  source as of a stated date. It does not promise that later announcements have
  already been captured.
- A season check can confirm that no productions were announced. A positive
  production count is not required.
- Source-record review remains separate. `EntitySource.reviewed` must not be
  reused as organization-level verification.
- No automatic verification expiry or automatic update flag in this release.
  An older check is not proof of incorrect data.

## Seasons

Represent a Spielzeit by its four-digit start year: `2026` means `2026/27`.
The end year is always start year plus one; format centrally, including century
boundaries such as `1999/00`.

Use one explicit application-wide current-season setting in
`backend/config.py`, initially `CURRENT_SEASON_START_YEAR = 2026`. This is a
deliberate editorial setting, not a calendar-year calculation: houses announce
programmes at different times. Changing this one setting constitutes rollover;
all views and filters must then derive freshness against the new season without
rewriting assessments. No scheduler, per-theatre season calendar, or settings
screen is needed.

Allow valid historical seasons and the current season to be assessed. Reject
future seasons for now. Store a validated four-digit start year whose following
year is also representable. Store integers, not free-text season strings.

## Persisted data

Use typed fields and database constraints following existing SQLModel patterns.
Do not store derived marker colours or freshness labels.

### Organization assessment

Add these nullable fields to `Organization`; null defaults keep existing
organizations unassessed:

| Field | Meaning |
| --- | --- |
| `general_verified_at` | UTC timestamp of the most recent explicit general-info check |
| `general_verified_by` | Internal reviewer identity, using the existing actor convention |
| `general_verification_evidence` | Source citation or explanatory evidence |
| `history_from_season` | Inclusive start season, represented by start year |
| `history_through_season` | Inclusive final season, represented by start year |
| `history_verified_at` | UTC timestamp of the explicit history assessment |
| `history_verified_by` | Internal reviewer identity |
| `history_verification_evidence` | Source/archive citation and relevant scope explanation |
| `update_scope` | Null, `general`, `productions`, or `both` |
| `update_reason` | Required nonblank reason while an update flag is active |
| `update_flagged_at` | UTC timestamp of the latest explicit flag change |
| `update_flagged_by` | Internal actor responsible for that flag change |

Use existing actor/audit mechanisms rather than introducing a new identity
system. Reviewer identities are internal metadata, not public contact details.
Verification and flag timestamps/actors are server-assigned, not editable
generic form fields.

General verification metadata must be present together or absent together.
History bounds and verification metadata must likewise be present together or
absent together. Require nonblank evidence and `from <= through`. An active
update flag requires its reason, timestamp, and actor; inactive flag fields are
null. Apply validation at the backend write boundary, not just in widgets.

### Season checks

Add an organization-owned `OrganizationSeasonCheck` table:

| Field | Meaning |
| --- | --- |
| `organization_id` | Foreign key to the organization |
| `season_start_year` | Start year of the checked season |
| `checked_at` | Server-assigned UTC timestamp |
| `checked_by` | Internal reviewer identity |
| `evidence` | Required nonblank source citation; may include a short note |

Use a UUID primary key and a unique organization/season constraint. The UUID
keeps the existing SCD2 entity identity and snapshot helpers reusable; a
composite-only primary key would require special-case audit handling.
Rechecking a season updates that season's record through the normal audited
write path. Previous seasons' checks remain separate records after rollover.
The existing audit mechanism preserves corrections/rechecks; do not build a
parallel event system.

Season checks have their own SCD2 versions: rechecking retains the check's
identity, removal closes its current version with a deletion event, and
organization deletion/merge must account for the child records too. Ordinary
organization snapshots continue to include the assessment fields. A season
check must not manufacture a general-information verification event.

Evidence can be a URL or a written archive/publication citation; no network
fetch is required to save it. Only render safe HTTP(S) values as clickable links.

## Editorial workflow

Add a dedicated **Datenqualität** section to existing organization management,
using the current management authorization rules and native Streamlit controls.
Keep assessment actions separate from the generic organization field editor;
exclude internal assessment columns from automatically generated forms.

Provide four small, explicit actions:

1. **Allgemeine Angaben prüfen**: show name, location/address, website, and
   organization type; require evidence and explicit confirmation. Unknown or
   inapplicable optional details need not be invented. Save reviewer/time.
2. **Produktionshistorie bestätigen**: select inclusive first/last seasons,
   provide evidence, and explicitly confirm completeness within that range.
   One continuous range per organization is sufficient for this release.
3. **Spielzeit prüfen**: default to the configured current season; allow
   historical seasons, require evidence, and confirm the available programme
   was checked. Display any existing check before replacing it.
4. **Aktualisierung nötig**: choose general information, productions, or both;
   enter a reason. Show a separate explicit action to resolve the flag.

Show saved assessments, dates, and internal reviewers to editors. Offer explicit
withdrawal of an incorrect general/history assessment and removal of an
incorrect season check, with confirmation and existing audit logging.

Each action validates before writing, commits atomically with its audit event,
and reports success only after persistence. Errors remain visible and preserve
form input. Never silently accept partially populated metadata.

Ordinary organization edits, successful imports, season checks, and history
confirmations neither clear flags nor renew other assessments. Resolving a flag
does not itself verify anything. If an editor withdraws general verification,
retain history coverage and season checks.

## Derived display states

### Map colour

Use the following precedence, shared by every display:

| Priority | Condition | German label | RGB |
| --- | --- | --- | --- |
| 1 | Any active update flag | Aktualisierung nötig | `217, 119, 6` |
| 2 | General verified and history coverage confirmed | Allgemeine Angaben und Historie geprüft | `0, 122, 94` |
| 3 | General verified | Allgemeine Angaben geprüft | `37, 99, 235` |
| 4 | Otherwise | Allgemeine Angaben noch ungeprüft | `107, 114, 128` |

Preserve the current map's opacity and selection/highlight behaviour. Marker
colour describes editorial coverage, not theatre quality or significance.
Green does not imply current-season freshness or lifetime coverage. Legend and
tooltip must make that distinction explicit.

Never use an unqualified "full history" or "complete data" label. Display
**Produktionshistorie vollständig: 1990/91–2025/26** for a confirmed range.
Even if the range starts at founding, show its explicit endpoints.

### Season freshness

Calculate separately from colour, using the configured current season:

| Condition | German label |
| --- | --- |
| Check exists for current season | Aktuelle Spielzeit geprüft |
| Checks exist, but none for current season | Nur frühere Spielzeiten geprüft |
| No season checks | Spielzeit noch nicht geprüft |

Always show the target season. For a current check show its check date; otherwise
show the latest checked season/date if available. The wording must remain
"checked", not "fully updated": later programme changes may exist.

An update flag may coexist with a current-season check. Show both rather than
hiding the flag or discarding the historical check.

## Public interface

### Overview map and selected-location cards

- Replace the fixed `ScatterplotLayer` fill colour with the derived per-record
  colour. Preserve layer ID `theaters`, coordinates, sizing, click behaviour,
  and links.
- Add a visible legend explaining all four colours and the separate season
  indicator. Include text labels; colour cannot be the only status cue.
- Tooltip shows theatre name/city, general-verification state/date, history
  range or unknown state, current-season check state/date, and any update reason.
- Selected-location cards show the same short quality and season labels.
- Keep all co-located organizations accessible; each card shows its own state.
  Do not aggregate conflicting assessments into one invented site assessment.
- Markers with identical coordinates can still overlap, as today; explain in
  the legend/help that a visible marker need not represent every organization
  at that location. Use deterministic layer ordering.

### Search, filters, and directory

Keep the existing name/place/country search. Add:

- A quality filter with "all" plus the four derived colour states.
- A **Aktuelle Spielzeit noch nicht geprüft** toggle, including both older-only
  and never-checked organizations.

Combine search and filters with AND. Apply them to one shared dataset before
deriving map-visible rows; the directory must still include matching
organizations without coordinates. Keep overall database metrics unchanged
and identify result counts as filtered.

Add readable quality, history-period, and current-season columns to the
directory, preserving detail links. Empty filtered results use the existing
empty-state pattern and must not break map construction.

### Theatre profile

Add a compact read-only **Datenqualität** summary near the profile heading:
general-verification date, confirmed history range, current Spielzeit/check
date, and any update reason. Show evidence in the summary or an adjacent
expandable section. Internal reviewer IDs/usernames must not be exposed on the
public page. Existing source citations remain separate.

For organizations with no assessments, show clear unknown states, not a warning
that suggests their data is necessarily wrong.

Example, with a matching explicit production-update flag:

> Allgemeine Angaben geprüft am 19.11.2026
>
> Produktionshistorie vollständig: 1990/91–2025/26
>
> Spielzeit 2026/27 noch nicht geprüft
>
> Aktualisierung nötig: Programm der aktuellen Spielzeit fehlt.

## Integration and preservation

Inspect these existing paths before implementing:

| Area | Existing entry point / requirement |
| --- | --- |
| Schema | `backend/models.py`; add fields and season checks with constraints |
| Season setting | `backend/config.py`; one source of truth |
| Domain writes and audit | `backend/entity_management.py`, `backend/scd2.py`; reuse existing transactions and actor conventions |
| Overview | `frontend/views/overview.py`; extend `organization_data`, `theater_map`, filters, cards, and directory |
| Profile data | `backend/entity_management.py` / `get_organization_profile`; explicitly include required public assessment data |
| Profile UI/cache | `frontend/views/organization.py`; invalidate `load_organization_profile` after assessment writes |
| Management | `frontend/management/organizations.py`, `frontend/management/uifactory.py`; focused assessment section, no internal-field editing |
| Existing evidence | `backend/import_status.py`; statistics support review but never prove completeness |
| Imports | `backend/production_import.py`, ingestion/reconciliation paths; do not overwrite or infer editorial assessments |
| Merges/deletion | Existing organization merge/delete paths; handle new metadata and season-check references explicitly |
| Tests | `tests/test_overview.py` and existing organization, profile, merge, audit, and migration tests |

Keep a single shared derivation of quality and freshness, reused by overview
and profile; do not duplicate precedence rules in UI code. Batch-load season
checks for directory results; avoid one query per organization. Do not fetch
full production histories simply to colour the map.

Organization merges must not silently broaden verified coverage or choose a
reviewer arbitrarily. Preserve identical assessments; require explicit choice
for differing populated assessment blocks and differing checks for the same
season. Carry distinct season checks to the surviving organization and retain
audit history. A present assessment versus an absent one can be carried over.
Differing update flags also require explicit resolution, not automatic clearing.
Show the resulting assessment before merge confirmation. Do not combine history
ranges to imply that their union has been verified.

Deletion must follow existing organization deletion behaviour without orphaning
season checks. Generic serialization/export must not accidentally expose
internal reviewer metadata on public pages.

## Migration and compatibility

- Generate and review an Alembic migration using the existing database workflow;
  support the repository's PostgreSQL and SQLite test setup.
- Existing rows receive null assessments and no season checks. Do not backfill
  from source review flags, imports, production counts, or timestamps.
- Preserve organization IDs, coordinates, productions, source records, and
  existing audit history.
- Refresh overview data on save and invalidate affected profile cache entries;
  assessment changes must be visible after returning to public views.
- Keep current-season derivation outside long-lived cached profile payloads,
  or include the configured season in cache keys, so rollover cannot leave stale
  labels.

## Acceptance tests

Implement a few focused cases in existing test files where practical; add a
small dedicated domain test file only when no suitable file exists.

1. An existing/unassessed organization is grey; all assessment fields remain
   unknown. Source review/import/production counts do not promote it.
2. General verification alone is blue. History alone remains grey but its
   period is displayed. Both together are green. An update flag overrides all
   combinations with amber without deleting their assessments.
3. Reject blank evidence/reasons, incomplete assessment blocks, reversed
   history ranges, invalid/future season years, and unauthorized writes.
   Failed validation leaves persistent data unchanged.
4. A season check is unique per organization/season, server-attributed, and
   survives reload. Rechecking is audited. Zero productions does not prevent
   recording a check.
5. Changing current season from 2026 to 2027 turns a 2026/27-only check into
   "Nur frühere Spielzeiten geprüft" without mutation. Adding a 2027/28 check
   restores current status and preserves 2026/27. Formatting handles 1999/00.
6. General edits/imports do not renew assessments or clear flags. Resolving a
   flag leaves checks unchanged. Explicit withdrawal/removal is audited and
   changes only the selected assessment.
7. Map layer retains ID `theaters`, uses row colours, and includes assessment
   tooltip data. Search and both filters agree between map and directory;
   missing coordinates, co-located organizations, and empty results still work.
8. Profile and directory show the same states, dates, and history period.
   Public output omits internal reviewer identities; unsafe evidence is never
   made an executable/clickable link.
9. Merge preserves distinct season checks, explicitly resolves assessment
   conflicts, and cannot silently widen history coverage or clear flags.
   Deletion cannot leave orphaned checks.
10. Migration preserves existing domain data. Assessment saves persist with
    audit events atomically; refreshing public views shows the saved state
    rather than stale cached values.

Run the smallest existing pytest selectors covering the change, then the
repository's configured type/lint checks for touched code. Verify management
actions and public views in a local running app with a disposable database;
do not write test assessments into production data.

## Out of scope

- Numeric quality scores, inferred completeness, automatic verification.
- Per-production/person quality, crowdsourced public editing, new permissions.
- Automatic stale-age thresholds, reminders, scheduled expiry, crawler triggers.
- A season-management UI, per-theatre season calendars, or future-season checks.
- Multiple disjoint history ranges, gap inference, completeness percentages.
- New map components, clustering, or redesign of existing source-review tools.

## Editor quick start

1. Open **Verwaltung > Organisationen**, search for the theatre, and select its
   row. The **Datenqualität** section appears above ordinary editing.
2. The default action is the current Spielzeit. The programme URL, or otherwise
   the theatre URL, is prefilled as evidence. Compare the available programme
   with the database, then explicitly confirm the season check. No date or
   reviewer needs to be entered.
3. Use the other segments to confirm general information or a historical
   coverage range, or to leave a short public update request. These are
   independent actions; confirming one does not silently change the others.
4. To work through unreviewed current-season entries, use the overview's
   **Aktuelle Spielzeit noch nicht geprüft** filter.
5. To correct an erroneous check, select the relevant action/season and confirm
   withdrawal. The audit trail retains the earlier assessment.

At season rollover, change `CURRENT_SEASON_START_YEAR` in `backend/config.py`
once for the application. Older season checks remain intact and automatically
stop matching the current-season filter. No annual data migration is needed.
