import streamlit as st

from backend.entity_management import get_people_productions
from frontend.views.helpers import entity_link, production_people, role_label
from frontend.views.organization import (
    parse_date,
    premiere_label,
    render_production_card,
    render_sources,
    render_web_link,
)


def render_cast(data: dict, other_productions: list[dict]) -> None:
    """Keep roles together and count other credits at this production's theatre."""
    st.subheader("Besetzung & Team")
    people = production_people(data)
    organization = data.get("organization") or {}
    if not people:
        st.info("Für diese Produktion sind noch keine Mitwirkenden erfasst.")
    for person_id, person in people.items():
        with st.container(border=True):
            entity_link(person["name"], "person", person_id)
            credits = [
                " · ".join(filter(None, (role_label(membership.get("role")), membership.get("character"))))
                for membership in data.get("memberships") or []
                if str(membership.get("person_id")) == person_id
                or str((membership.get("person") or {}).get("id")) == person_id
            ]
            st.text(" / ".join(dict.fromkeys(filter(None, credits))) or "Rolle nicht hinterlegt")
            if organization:
                count = sum(
                    str(production.get("organization_id")) == str(organization["id"])
                    and person_id in production_people(production)
                    for production in other_productions
                )
                st.caption(f"Weitere Produktionen an diesem Theater: {count}")


def render_connections(data: dict, other_productions: list[dict]) -> None:
    """Link to other productions by theatre and shared contributors."""
    organization = data.get("organization") or {}
    people = production_people(data)
    st.caption("Verbindungen beruhen auf erfassten Produktionen, nicht auf inhaltlicher Ähnlichkeit.")
    st.subheader("Weitere Produktionen an diesem Theater")
    theatre_productions = {
        str(production["id"]): production
        for production in organization.get("productions") or []
        if str(production["id"]) != str(data["id"])
    }
    st.caption(f"Weitere erfasste Produktionen: {len(theatre_productions)}")
    if not organization:
        st.info("Noch kein Theater hinterlegt.")
    for related in sorted(theatre_productions.values(), key=lambda production: production["name"].casefold()):
        render_production_card(related, show_organization=False)
    st.subheader("Weitere Produktionen mit diesen Mitwirkenden")
    st.caption(f"Weitere erfasste Produktionen: {len(other_productions)}. Jede Produktion zählt einmal.")
    for related in other_productions:
        shared = production_people(related).keys() & people.keys()
        st.text("Mit " + " · ".join(sorted(people[person_id]["name"] for person_id in shared)))
        render_production_card(related)


def render_text(text: dict) -> None:
    """Show the recorded text and link its authors, translators and adapters."""
    st.subheader("Textgrundlage")
    st.text(text["title"])
    context = [
        f"Sprache: {text['language']}" if text.get("language") else "",
        f"Erschienen: {text['published_year']}" if text.get("published_year") is not None else "",
    ]
    if any(context):
        st.caption(" · ".join(filter(None, context)))
    for contribution in text.get("contributions") or []:
        person = contribution.get("person") or {}
        st.caption(role_label(contribution.get("role")) or "Mitwirkung am Text")
        if person.get("id"):
            entity_link(person["name"], "person", person["id"])
        else:
            st.warning("Textbeitrag ohne verknüpfte Person.")


def render_production(data: dict) -> None:
    """Show production details, credits and documented connections."""
    st.caption("THEATERDB / PRODUKTION")
    st.title(data["name"])
    organization = data.get("organization") or {}
    if organization:
        entity_link(organization["name"], "organization", organization["id"])
        render_web_link("Theater-Website", organization.get("url"), official=True)
        if organization.get("city"):
            st.text(organization["city"])
    premiere = parse_date(data.get("premiere"))
    facts = [
        ("Premiere", premiere_label(data)),
        ("Dauer", f"{data['duration']} Minuten" if data.get("duration") is not None else "Nicht hinterlegt"),
        (
            "Altersempfehlung",
            f"Ab {data['age_rating']} Jahren" if data.get("age_rating") is not None else "Nicht hinterlegt",
        ),
    ]
    for column, (label, value) in zip(st.columns(3), facts, strict=True):
        with column.container(border=True):
            st.caption(label)
            st.text(value)
    if data.get("premiere") and premiere is None:
        st.warning(f"Ungültiges Premierendatum: {data['premiere']}")
    st.caption("Die Premiere bezeichnet keine Laufzeit und keinen aktuellen Aufführungstermin.")
    render_web_link("Offizielle Seite", data.get("url"), official=True)
    if data.get("tags"):
        st.text(" · ".join(data["tags"]))
    if data.get("description"):
        st.write(data["description"])
    else:
        st.caption("Noch keine Beschreibung hinterlegt.")
    text = data.get("text") or {}
    if text:
        render_text(text)

    people = production_people(data)
    other_productions = [
        production for production in get_people_productions(list(people)) if str(production["id"]) != str(data["id"])
    ]
    cast_tab, related_tab, source_tab = st.tabs(["Besetzung & Team", "Verbindungen", "Quellen & Nachweise"])
    with cast_tab:
        render_cast(data, other_productions)
    with related_tab:
        render_connections(data, other_productions)
    with source_tab:
        render_sources(data.get("sources") or [])
        if data.get("critics"):
            st.subheader("Kritiken")
            for critic in data["critics"]:
                st.write(critic)
    with st.expander("Technische Angaben"):
        st.caption("Interne Kennung dieses Eintrags")
        st.code(str(data["id"]), language=None)
