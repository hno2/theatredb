import streamlit as st

from backend.entity_management import get_people_productions
from frontend.views.helpers import entity_link, production_people
from frontend.views.organization import parse_date, render_production_card, render_sources, render_web_link


def collaboration_groups(productions: list[dict], person_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Group distinct productions by theatre and collaborator, excluding the subject."""
    theatres: dict[str, dict] = {}
    people: dict[str, dict] = {}
    for production in {str(production["id"]): production for production in productions}.values():
        organization = production.get("organization")
        if organization:
            group = theatres.setdefault(str(organization["id"]), {"name": organization["name"], "productions": []})
            group["productions"].append(production)
        for collaborator_id, person in production_people(production).items():
            if collaborator_id != person_id:
                group = people.setdefault(collaborator_id, {"name": person["name"], "productions": []})
                group["productions"].append(production)
    return theatres, people


def render_person_header(data: dict) -> None:
    """Show biographical data without inventing missing dates or links."""
    st.caption("THEATERDB / PERSON")
    st.title(data["name"])
    context = [*(data.get("type") or []), data.get("nationality")]
    if any(context):
        st.text(" · ".join(filter(None, context)))
    for field, label in (("born", "Geboren"), ("died", "Gestorben")):
        if data.get(field):
            value = parse_date(data[field])
            if value:
                st.caption(f"{label}: {value:%d.%m.%Y}")
            else:
                st.warning(f"{label}: Ungültiges Datum: {data[field]}")
    for index, url in enumerate(data.get("urls") or [], start=1):
        render_web_link(f"Website {index}", url)


def render_person(data: dict) -> None:
    """Show a person profile with credits and documented collaborations."""
    render_person_header(data)
    productions = get_people_productions([str(data["id"])])
    theatres, people = collaboration_groups(productions, str(data["id"]))
    metrics = [("Produktionen", len(productions)), ("Theater", len(theatres)), ("Mitwirkende", len(people))]
    for column, (label, value) in zip(st.columns(3), metrics, strict=True):
        column.metric(label, value, border=True)
    production_tab, collaboration_tab, source_tab = st.tabs(["Produktionen", "Zusammenarbeit", "Quellen & Nachweise"])
    with production_tab:
        st.subheader("Produktionen")
        if not productions:
            st.info("Für diese Person sind noch keine Produktionen erfasst.")
        for production in productions:
            render_production_card(production)
    with collaboration_tab:
        st.caption("Gezählt werden unterschiedliche erfasste Produktionen, unabhängig von der Anzahl der Rollen.")
        for heading, kind, groups in (("Mit Theatern", "organization", theatres), ("Mit Personen", "person", people)):
            st.subheader(heading)
            if not groups:
                st.info("Noch keine gemeinsamen Produktionen erfasst.")
            for entity_id, group in sorted(
                groups.items(), key=lambda item: (-len(item[1]["productions"]), item[1]["name"])
            ):
                with st.container(border=True):
                    entity_link(group["name"], kind, entity_id)
                    st.caption(f"Gemeinsame Produktionen: {len(group['productions'])}")
                    with st.expander("Produktionen anzeigen"):
                        for production in group["productions"]:
                            entity_link(production["name"], "production", production["id"])
    with source_tab:
        render_sources(data.get("sources") or [])
    with st.expander("Technische Angaben"):
        st.caption("Interne Kennung dieses Eintrags")
        st.code(str(data["id"]), language=None)
