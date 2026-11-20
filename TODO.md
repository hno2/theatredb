
- [ ] Images
- [ ] Merge with theatrekompass
- [x]
- [ ] Should seperate for productions in the future vs. already done.
- [ ] Default is Schauspiel, check all False Positives.
- [/] Texts can be multi-lang and might have multi-lang or alternative titles. Match them.
- [ ] Theatres can have shortcuts, findable. Give them
- [ ] Icons for sources, e.g. Wikipedia. --> Use Favicon
- [ ] Types of Theatre (Staatstheater: https://de.wikipedia.org/wiki/Liste_von_Staatstheatern)
- [x] Person with multiple roles in Prod shows up multiple times
- [x] Country import from theapolis contains both Bundesland as well as country
- [ ] Not all persons are imported from Theapolis (e.g. Cinderalla Example from BST)
- [/] Each Management Page should show the number of entries in general
- [x] THeapolis only import german ?
- [ ] Better Overview Page on the front
- [ ] Nice easy graph Hamlet over time
- [ ] Besetzung looks not nice, too much vertical space, not enough, at one glance?
- [ ] Better colour schema
- [ ] Production Page Tag styling
- [ ] Production Sort SChauspiel/Crew
- [ ] Handle/Understand Alt names Theatre/Person 
- [ ] Fix Favicon
- [ ] Understand Wikidata mapping
- [ ] Page top level nav (go back to theatre, page before)
- [ ] Details sort by number productions, beschreibung shortened not in expander
## Sources
- [ ] TheatreBW
- [ ] Theaterverlage https://www.concordtheatricals.de/

## Performance / reuse follow-up

- [x] **Startup:** management pages still run migration/queue checks per rerun.
  Move checks to explicit startup; preserve reset/reinitialize behavior. No naive global cache.
  See [database lifecycle](backend/user_management.py) and [reset CLI](backend/db.py).
- [x] **CRUD reads:** paginate/filter in SQL; fetch selected entity's sources with a scoped join,
  not both full tables. Keep native Streamlit tables.
  See [generic management UI](frontend/management/uifactory.py).
- [x] **Text detail:** batch productions by family member; avoid loading every text just to
  render one family. See [text management](frontend/management/texts.py).
- [x] **People matching:** publisher missing-credit paths still scan all people per name.
  Reuse one request-local lookup; keep ambiguity checks and source identity boundaries.
  See [publisher imports](backend/ingestion/publishers.py) and [text resolver](backend/texts.py).
- [x] **Bulk text writes:** batch display-title lookups and scope lineage checks to affected paths.
  Preserve cycle validation, advisory locking and transaction visibility.
  See [model flush hooks](backend/models.py).
- [x] **Upload overview:** aggregate coverage in SQL; scope conflict counts and source reads.
  Overview currently loads productions, memberships and upload history into Python.
  See [import status](backend/import_status.py).
- [x] **Duplicate discovery:** reuse one session and batch production candidates and credits.
  Keep limits and explicit review. Retain individual queries for other types, where batching did not help.
- [x] **Large public lists:** add SQL pagination/search for organization overview; fetch only
  linked sources on organization pages. See [public views](frontend/views/).
- [x] **Crawler fetches:** measured on PostgreSQL 18.4. Combining checkpoint/cache reads
  did not reduce median crawl time (1,215 ms separate vs. 1,324 ms combined over 61 URLs).
  Retain separate sessions and independent HTTP-cache durability.
- [ ] **Cross-tab drafts:** lazy review tabs clean up inactive widgets. Add explicit draft
  persistence only if workflows need unsaved inputs across tabs; saved records unaffected.
- [/] **Deployment checks:** benchmarked local PostgreSQL 18.4 and smoke-tested installed
  Streamlit 1.67. Production network latency remains unmeasured.

## Stretch
- [ ] Also have planned performances/ # number of performances
- [ ] Plus show overview via which theatres have a config.
- [ ] critiques form newspapers, access via archive.ph
- Rezensionen Zeit.de
- scrapAI
- Rezensionen https://www.ndr.de/kultur/buehne/theaterberichte
