from collections import defaultdict
from html import escape

import altair as alt
import streamlit as st

from backend.config import canonical_role
from backend.entity_management import get_people_productions
from frontend.views.helpers import entity_anchor, render_tags, role_label, theatre_label
from frontend.views.organization import (
    domain_date_interval,
    domain_date_label,
    is_theapolis_url,
    premiere_label,
    render_sources,
    render_web_link,
    web_url,
)
from frontend.views.personal import render_actions

RELATED_LIMIT = 5


def _organization_anchor(label: str, organization: dict) -> str:
    verified = (
        organization.get("general_verified_at") is not None
        and organization.get("general_verification_evidence") is not None
        and organization.get("update_scope") in (None, "productions")
    )
    return entity_anchor(label, "organization", organization["id"], include_unverified=not verified)


def _director_ids(data: dict) -> set[str]:
    return {
        str(person_id)
        for credit in data.get("memberships") or []
        if canonical_role(credit.get("role")) == "director"
        and (person_id := credit.get("person_id") or (credit.get("person") or {}).get("id"))
    }


def _named_credits(credits: list[dict]) -> list[dict]:
    return [credit for credit in credits if (person := credit.get("person") or {}).get("id") and person.get("name")]


def _role_order(role: str) -> tuple[int, str]:
    return {"Text": 0, "Regie": 1, "Schauspiel": 2}.get(role, 3), role.casefold()


def _render_cast(memberships: list[dict], text: dict) -> None:
    people: dict[str, str] = {}
    roles: dict[str, dict[str, dict[str, None]]] = {}
    cast_ids = {credit["person"]["id"] for credit in memberships}
    text_credits = [
        credit for credit in _named_credits(text.get("contributions") or []) if credit["person"]["id"] in cast_ids
    ]
    for credits, fallback in [(memberships, "Mitwirkung"), (text_credits, "Mitwirkung am Text")]:
        for credit in credits:
            person = credit["person"]
            person_id = person["id"]
            people[person_id] = person["name"]
            role = role_label(credit.get("role")) or fallback
            characters = roles.setdefault(person_id, {}).setdefault(role, {})
            if character := credit.get("character"):
                characters[character] = None
    if not roles:
        return
    st.subheader("Besetzung")
    lines = []
    for person_id in sorted(roles, key=lambda pid: (min(map(_role_order, roles[pid])), people[pid].casefold())):
        labels = []
        for role, characters in sorted(roles[person_id].items(), key=lambda item: _role_order(item[0])):
            label = escape(role)
            if characters:
                label += f" ({', '.join(map(escape, characters))})"
            labels.append(label)
        name = entity_anchor(people[person_id], "person", person_id)
        lines.append(f"<p><strong>{', '.join(labels)}</strong>: {name}</p>")
    st.markdown("\n".join(lines), unsafe_allow_html=True)


def _premiere_year(production: dict) -> int | None:
    interval = domain_date_interval(production, "premiere")
    return interval[0].year if interval else production.get("premiere_year")


def _related_productions(entries: list[dict], current_id: str) -> list[dict]:
    unique = {entry["id"]: entry for entry in entries if entry["id"] != current_id}
    return sorted(
        unique.values(),
        key=lambda entry: (-(_premiere_year(entry) or 0), entry["name"].casefold(), entry["id"]),
    )


def _related_line(entry: dict, *, context_field: str | None) -> str:
    organization = entry.get("organization") or {}
    parts = [entry["name"]]
    theatre = theatre_label(organization.get("name") or "", organization.get("city"))
    context = (organization.get("city") or theatre) if context_field == "city" else theatre
    if context_field and context:
        parts.append(context)
    if year := _premiere_year(entry):
        parts.append(str(year))
    return entity_anchor(" · ".join(parts), "production", entry["id"])


def _render_history(label: str, entries: list[tuple[int | None, str]], *, publications: bool = False) -> None:
    titles_by_year: dict[int, list[str]] = defaultdict(list)
    for year, title in entries:
        if year is not None:
            titles_by_year[year].append(title)
    if not titles_by_year:
        return

    axis = "Erscheinungsjahr" if publications else "Premierenjahr"
    count_label = "Texte" if publications else "Premieren"
    rows = []
    for year in range(min(titles_by_year), max(titles_by_year) + 1):
        titles = sorted(titles_by_year.get(year, []), key=str.casefold)
        rows.append({"year": year, "count": len(titles), "titles": " · ".join(titles) or "—"})
    chart = (
        alt.Chart(alt.InlineData(values=rows))
        .mark_bar()
        .encode(
            x=alt.X("year:O", title=axis, axis=alt.Axis(labelAngle=0, labelOverlap="greedy")),
            y=alt.Y("count:Q", title=count_label, axis=alt.Axis(tickMinStep=1, tickCount=3)),
            tooltip=[
                alt.Tooltip("year:O", title=axis),
                alt.Tooltip("count:Q", title=count_label, format="d"),
                alt.Tooltip("titles:N", title="Titel" if publications else "Produktionen"),
            ],
        )
    )
    total = sum(map(len, titles_by_year.values()))
    singular, plural = (
        ("Text mit Erscheinungsjahr", "Texte mit Erscheinungsjahr")
        if publications
        else ("erfasste Premiere", "erfasste Premieren")
    )
    st.caption(f"{total} {singular if total == 1 else plural}")
    st.altair_chart(chart, height=145, alt=f"{label}: {count_label} pro Jahr")


def _render_related_card(title: str, rows: list[dict], context: str | None, footer: str, current: dict) -> None:
    with st.container(border=True, gap="xsmall"):
        st.markdown(f"#### {title}")
        lines = [_related_line(entry, context_field=context) for entry in rows]
        st.markdown(f"<p>{'<br>'.join(lines[:RELATED_LIMIT])}</p>", unsafe_allow_html=True)
        if context == "city" and (remaining := lines[RELATED_LIMIT:]):
            label = "1 weitere Produktion" if len(remaining) == 1 else f"{len(remaining)} weitere Produktionen"
            with st.expander(label, type="compact"):
                st.markdown(f"<p>{'<br>'.join(remaining)}</p>", unsafe_allow_html=True)
        if footer:
            st.caption(f"<p>{footer}</p>", unsafe_allow_html=True)
        if context is not None:
            _render_history(title, [(_premiere_year(entry), entry["name"]) for entry in [*rows, current]])


def _related_texts(text: dict) -> list[tuple[int | None, str]]:
    related = {}
    for direction, endpoint in (("source", "derived_text"), ("derived", "source_text")):
        for lineage in text.get(f"lineages_as_{direction}") or []:
            if lineage["relation"] in {"translation", "adaptation"} and (entry := lineage.get(endpoint)):
                related[entry["id"]] = entry
    rows = []
    for entry in related.values():
        interval = domain_date_interval(entry, "published")
        rows.append((interval[0].year if interval else entry.get("published_year"), entry["title"]))
    return sorted(rows, key=lambda row: (-(row[0] or 0), row[1].casefold()))


def _render_related_text_card(rows: list[tuple[int | None, str]]) -> None:
    title = "Verwandte Texte"
    lines = [
        f"{escape(name)} · {year}" if year is not None else f"{escape(name)} · Erscheinungsjahr unbekannt"
        for year, name in rows
    ]
    with st.container(border=True, gap="xsmall"):
        st.markdown(f"#### {title}")
        st.caption("Übersetzungen und Bearbeitungen")
        st.markdown(f"<p>{'<br>'.join(lines[:RELATED_LIMIT])}</p>", unsafe_allow_html=True)
        if remaining := lines[RELATED_LIMIT:]:
            label = "1 weiterer Text" if len(remaining) == 1 else f"{len(remaining)} weitere Texte"
            with st.expander(label, type="compact"):
                st.markdown(f"<p>{'<br>'.join(remaining)}</p>", unsafe_allow_html=True)
        _render_history(title, rows, publications=True)


def _render_connections(data: dict) -> None:
    organization = data.get("organization") or {}
    text = data.get("text") or {}
    director_ids = _director_ids(data)
    directed = (
        [entry for entry in get_people_productions(sorted(director_ids)) if _director_ids(entry) & director_ids]
        if director_ids
        else []
    )
    director_links = list(
        dict.fromkeys(
            entity_anchor(f"Mehr von {person['name']} →", "person", person["id"])
            for credit in data.get("memberships") or []
            if canonical_role(credit.get("role")) == "director"
            and (person := credit.get("person") or {}).get("id")
            and person.get("name")
        )
    )
    same_text = text.get("productions") or []
    groups = [
        ("Gleiche Textgrundlage", same_text, "city", ""),
        (
            "Am selben Theater",
            organization.get("productions") or [],
            None,
            _organization_anchor("Mehr vom Theater →", organization) if organization.get("id") else "",
        ),
        ("Dieselbe Regie", directed, "name", "<br>".join(director_links)),
    ]
    cards = [
        (title, rows, context_field, footer)
        for title, entries, context_field, footer in groups
        if (rows := _related_productions(entries, data["id"]))
    ]
    related_texts = _related_texts(text)
    if not cards and not related_texts:
        return
    st.header("Weitere Produktionen und Texte" if related_texts else "Weitere Produktionen")
    with st.grid(min(3, len(cards) + bool(related_texts)), min_column_width=320):
        for title, rows, context_field, footer in cards:
            _render_related_card(title, rows, context_field, footer, data)
        if related_texts:
            _render_related_text_card(related_texts)


def _render_text_credit(text: dict, production_title: str, cast_ids: set[str]) -> None:
    languages = ", ".join(text.get("languages") or []) or text.get("language")
    published = domain_date_label(text, "published") if text.get("published") else text.get("published_year")
    details = [f"Sprache: {languages}" if languages else "", f"Erschienen: {published}" if published else ""]
    credits: dict[str, dict[str, None]] = {}
    for contribution in text.get("contributions") or []:
        person = contribution.get("person") or {}
        role = role_label(contribution.get("role")) or "Mitwirkung am Text"
        if person.get("id") and person.get("name"):
            if person["id"] not in cast_ids:
                name = entity_anchor(person["name"], "person", person["id"])
                credits.setdefault(name, {})[role] = None
        else:
            details.append(f"{role}: Person nicht verknüpft")

    title = text["title"]
    authors = ", ".join(name for name, roles in credits.items() if roles.keys() == {"Text"})
    if title.strip().casefold() != production_title.strip().casefold():
        credit = f"Nach „{escape(title)}“" + (f" von {authors}" if authors else "")
    else:
        credit = f"Von {authors}" if authors else ""
    parts = [credit] if credit else []
    for name, roles in credits.items():
        if roles.keys() != {"Text"}:
            parts.append(f"{escape(', '.join(sorted(roles, key=_role_order)))}: {name}")
    help_text = "\n\n".join(filter(None, details)) or None
    line = " · ".join(parts) or f"Text: „{escape(title)}“"
    if text.get("id"):
        line += f" · {entity_anchor('Zum Text', 'text', str(text['id']))}"
    st.caption(f"<p>{line}</p>", unsafe_allow_html=True, help=help_text)


def _render_heading(data: dict, organization: dict, memberships: list[dict]) -> None:
    if organization.get("id"):
        back = _organization_anchor(f"← {theatre_label(organization['name'], organization.get('city'))}", organization)
        st.markdown(f"<p>{back}</p>", unsafe_allow_html=True)
    st.title(data["name"])
    if text := data.get("text"):
        _render_text_credit(text, data["name"], {credit["person"]["id"] for credit in memberships})
    premiere = premiere_label(data)
    facts = [
        f"Premiere: {premiere}" if premiere != "Nicht hinterlegt" else "",
        f"{data['duration']} Minuten" if data.get("duration") is not None else "",
        f"Ab {data['age_rating']} Jahren" if data.get("age_rating") is not None else "",
    ]
    if any(facts):
        st.markdown(" · ".join(filter(None, facts)), help="Die Premiere ist kein aktueller Aufführungstermin.")
    if data.get("premiere") and premiere == "Nicht hinterlegt":
        st.warning(f"Ungültiges Premierendatum: {data['premiere']}")
    render_tags(data.get("tags") or [])


def _render_overview(data: dict, memberships: list[dict]) -> None:
    description = data.get("description")
    if description and memberships:
        layout = st.grid(2, min_column_width=320, gap="medium")
        overview, cast = layout.cell(), layout.cell()
    else:
        overview = cast = st.container()
    with overview:
        if description:
            st.subheader("Beschreibung")
            st.markdown(description)
    with cast:
        _render_cast(memberships, data.get("text") or {})


def render_production(data: dict) -> None:
    """Show the production, cast, related productions and citations in one view."""
    organization = data.get("organization") or {}
    memberships = _named_credits(data.get("memberships") or [])
    _render_heading(data, organization, memberships)
    render_actions("production", data["id"])
    sources = list(data.get("sources") or [])
    for label, url in (("Offizielle Seite", data.get("url")), ("Theater-Website", organization.get("url"))):
        if (valid_url := web_url(url)) and is_theapolis_url(valid_url):
            if not any(web_url(source.get("external_url")) == valid_url for source in sources):
                sources.append({"title": "Theapolis-Quelle", "external_url": valid_url, "link_only": True})
        else:
            render_web_link(label, url)

    _render_overview(data, memberships)
    _render_connections(data)
    if critics := data.get("critics"):
        st.subheader("Kritiken")
        for critic in critics:
            st.markdown(critic)
    with st.expander("Technische Angaben"):
        st.json(data)
    if sources:
        render_sources(sources, compact=True)
