# Open Repertoire (ORIF v2)

TheatreDB exports and imports the
[Open Repertoire Interchange Format](https://buehnenverein.github.io/open-repertoire/)
v2 from the Deutscher Bühnenverein. The mapping lives in `backend/orif.py`.

## Renamed fields

Database columns and staged JSON keys now use the ORIF / schema.org names.
Migration `0012` renames the columns and rewrites stored review payloads and
crawl definitions. Staged uploads still accept the old keys. A record that sets
an old key and its new key to different values is rejected.

| Model                  | Old           | New (ORIF)                                |
| ---------------------- | ------------- | ----------------------------------------- |
| Organization           | `street`      | `street_address` (`streetAddress`)        |
| Organization           | `zip_code`    | `postal_code` (`postalCode`)              |
| Organization           | `city`        | `address_locality` (`addressLocality`)    |
| Organization           | `country`     | `address_country` (`addressCountry`)      |
| Organization           | `lat` / `lon` | `latitude` / `longitude`                  |
| Production             | `age_rating`  | `suggested_min_age` (`suggestedMinAge`)   |
| ProductionMembership   | `role`        | `role_name` (`roleName`)                  |
| ProductionMembership   | `character`   | `character_name` (`characterName`)        |

`TextContribution.role` and `OrganizationMembership.role` keep their names
because ORIF has no matching field for them.

## Export

`GET /api/organizations/{id}/orif` and `GET /api/productions/{id}/orif` return
one document. `GET /api/people/{id}/orif` returns an array with one document per
theatre. Responses use the `application/ld+json` media type. The same documents
can be downloaded from **Technische Angaben** on theatre, production, and person
pages.

- Organization: `identifier`, `name`, and `address`. `address` is included only
  when both postal code and locality are known, because ORIF requires both.
- Production:
  - `@type` is `Play` for play texts and `CreativeWork` otherwise.
  - `identifier` is the UUID.
  - The text becomes `isBasedOn`. Opera and musical texts become a
    `MusicComposition` with composers and lyricists; text authors are exported
    as lyricists. Other texts become a `Play` or `CreativeWork` with authors and
    translators.
  - `inLanguage` comes from the text languages, without `mul`.
  - `genre` comes from the text genre (play→`sprechtheater`, opera→`oper`,
    musical→`musical`) and from tags that match the ORIF genre list.
  - `productionType` comes from the premiere tags UA, DEA/DSE, ÖEA/SEA, and EA.
- Credits:
  - Actors and credits with a character become event `performer` entries.
  - All other memberships become `creator` roles with the German role label.
- Event:
  - Only the premiere is exported, as one `Premiere` event with `duration`,
    `url`, and the theatre as `location`.
  - It is exported only at day or minute precision.
  - Stored premieres are local wall time and are exported with the
    Europe/Berlin offset.

## Import

Theatre uploads (UI and `staged_to_db`) also accept an ORIF document. It is
converted to staged records before validation:

- Event URLs are used as production source links. A production without an event
  URL rejects the whole file.
- The `Premiere` event provides `premiere` and `duration`.
- Performers from all events and creators become memberships. Organization
  agents are skipped.
- The first `isBasedOn` or `workPerformed` work becomes `text`.
- `genre` and `productionType` become tags.

## Not mapped

- ORIF fields with no TheatreDB equivalent are never filled:
  - performance dates other than the premiere, `offers`, `eventStatus`,
    `subEvent`, and `superEvent`
  - images, videos, `logo`
  - accessibility fields, `contentWarning`, `funder`, `sponsor`, `creditText`
  - `dateModified`
- TheatreDB data that ORIF cannot hold is dropped from exports:
  - month or year premieres
  - critics, sources, `url_plays`, region, venues, founding dates, and quality
    metadata
  - people's biographies and dates
  - text lineage
- Productions without a theatre cannot be exported.
- Without a premiere event, character names are lost and actors are listed as
  creators.
