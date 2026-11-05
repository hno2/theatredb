## Install
### Docker
### Local Usage

## Quick Start
### Data Ingestion
### UI
Start the German-language Streamlit app with `uv run streamlit run frontend/app.py`.
The start page introduces the research goal, shows database counts and location
coverage, and offers a searchable theatre map and directory. Select a map point
to reveal its detail-page link, or use “Ansehen” in the directory. Entries without
valid coordinates remain searchable in the directory. Counts describe the
current database, not the entire German-speaking theatre landscape.
If several theatres share coordinates, selecting the point shows a link for
each matching entry.

The interface uses native Streamlit components and a theme in
`.streamlit/config.toml`. German is the current UI language; a future translation
mode should extract UI strings into language catalogs and use one shared language
selection across pages, leaving theatre names and source data unchanged.

Theatre detail pages show an address and location map, a premiere timeline
(newest or oldest first), and readable source citations with review status and
capture date. Productions without a valid premiere remain listed separately;
premiere dates do not represent production run dates. Missing coordinates and
unusable source links are indicated explicitly. Production titles are searchable;
long lists scroll within the production tab, while sources have their own tab.

Production profiles use the same style, with premiere, duration, age guidance,
cast and team credits, sources, and links to other productions at the theatre or
with the same contributors. Person profiles list productions and shared credits
with theatres and other people. Counts use distinct recorded productions, not
roles or inferred similarity; “other” excludes the production currently shown.
Detail links navigate in the same tab and replace the entity kind and UUID in
the URL, including links between two entries on the detail page.
## Contribution

Pull requests run the **PR checks** workflow with Python 3.14 and locked uv
dependencies, including all dependency groups. `prek` selects files changed
between the PR base and head; hooks configured with `pass_filenames: false`
(including Pyrefly) still run their repository-wide checks. The full pytest suite
runs even if hooks fail, provided dependency installation succeeded.

## Planned Features
- [ ]
- [ ] Actual Dates of performances
- [ ] Crawl all Staatstheater/Stadttheater for new info weekly
