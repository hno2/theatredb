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
## Contribution


## Planned Features
- [ ] 
- [ ] Actual Dates of performances
- [ ] Crawl all Staatstheater/Stadttheater for new info weekly 