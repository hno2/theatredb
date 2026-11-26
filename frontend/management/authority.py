"""Authority lookup for the selected entity: GND and Wikidata are fetched on selection, one click fills blanks."""

from __future__ import annotations

import operator
from uuid import UUID

import streamlit as st
from sqlmodel import Session

from backend.authority_match import MODELS, PROVIDERS, Proposal, Provider, label, propose
from backend.models import EntityType, Organization, Person, Text
from backend.user_management import get_session

_PROVIDERS = {provider.name: provider for provider in PROVIDERS}
_DAY = 24 * 3600


@st.cache_data(ttl=_DAY, show_spinner="Normdaten werden gesucht …")
def _search(provider: str, query: str, kind: EntityType) -> list[dict]:
    return _PROVIDERS[provider].search(query, kind)


@st.cache_data(ttl=_DAY, show_spinner="Normdatensatz wird geladen …")
def _fetch(provider: str, external_id: str) -> dict:
    return _PROVIDERS[provider].fetch(external_id)


@st.cache_data(ttl=_DAY, show_spinner=False)
def _predecessors(provider: str, external_id: str, _record: dict) -> list[tuple[str, str]]:
    """Cached per ID; the underscore keeps the record out of the cache key."""
    return _PROVIDERS[provider].predecessors(_record)


def _cached_fetch(provider: Provider, external_id: str) -> dict:
    return _fetch(provider.name, external_id)


def _hit(provider: Provider, record: dict) -> dict:
    external_id = provider.record_id(record)
    return {
        "id": external_id,
        "label": (provider.names(record) or [external_id])[0],
        "description": provider.summary(record),
        "url": provider.url(external_id),
        "record": record,
    }


def _hits(
    session: Session, provider: Provider, kind: EntityType, entity: Person | Text | Organization, key: str
) -> tuple[list[dict], str | None, Proposal | None]:
    """Linked record, else search hits with the high-confidence one first; a pasted ID overrides both."""
    if linked := provider.identity(session, kind, entity.id):
        return [_hit(provider, _fetch(provider.name, linked))], linked, None
    if manual := st.text_input("ID oder URL einfügen (optional)", key=f"{key}_manual"):
        return [_hit(provider, _fetch(provider.name, provider.parse_id(manual)))], None, None
    hits = _search(provider.name, label(entity), kind)
    proposal = propose(session, provider, kind, entity, hits=hits, fetch=_cached_fetch)
    if proposal is not None:
        best = next((hit for hit in hits if hit["id"] == proposal.external_id), None)
        best = best or _hit(provider, _fetch(provider.name, proposal.external_id))
        hits = [best, *(hit for hit in hits if hit["id"] != best["id"])]
    return hits, None, proposal


def _choose(hits: list[dict], proposal: Proposal | None, key: str) -> dict:
    def describe(hit: dict) -> str:
        mark = "✓ " if proposal and hit["id"] == proposal.external_id else ""
        description = f" — {hit['description']}" if hit["description"] else ""
        return f"{mark}{hit['label']}{description} ({hit['id']})"

    if len(hits) == 1:
        return hits[0]
    return st.selectbox("Treffer", hits, format_func=describe, key=f"{key}_hit")


def _apply(
    session: Session, provider: Provider, kind: EntityType, entity_id: UUID, hit: dict, predecessors: set | None
) -> None:
    try:
        report = provider.enrich(
            session, kind, entity_id, hit["id"], record=hit["record"], predecessor_ids=predecessors
        )
        session.commit()
    except (ValueError, *provider.errors) as exc:
        session.rollback()
        st.error(str(exc))
        return
    st.success(f"{provider.name}-Daten übernommen: {', '.join(report['filled']) or 'keine leeren Felder'}.")
    if report["conflicts"]:
        st.warning(
            "Abweichungen (nicht überschrieben): "
            + "; ".join(f"{c['field']}: {c['current']} ≠ {c['authority']}" for c in report["conflicts"])
        )
    with st.expander("Bericht"):
        st.json(report)


def _panel(session: Session, provider: Provider, kind: EntityType, entity: Person | Text | Organization) -> None:
    key = f"authority_{provider.name}_{kind}_{entity.id}"
    try:
        hits, linked, proposal = _hits(session, provider, kind, entity, key)
    except (ValueError, *provider.errors) as exc:
        st.warning(f"{provider.name} nicht erreichbar oder ID ungültig: {exc}")
        return
    if not hits:
        st.caption(f"Kein {provider.name}-Treffer für „{label(entity)}“.")
        return
    hit = _choose(hits, proposal, key)
    st.markdown(" · ".join(filter(None, [f"**{hit['label']}**", hit["description"]])))
    st.link_button("Eintrag öffnen", hit["url"], icon=":material/open_in_new:")
    if linked:
        st.caption("Bereits verknüpft; füllt weiterhin nur leere Felder.")
    elif proposal and proposal.external_id == hit["id"]:
        st.success(f"Hohe Sicherheit: {proposal.evidence}")
    else:
        st.caption("Nur Namenstreffer: Identität anhand des Eintrags prüfen.")
    predecessors = None
    try:
        refs = _predecessors(provider.name, hit["id"], hit["record"]) if kind == EntityType.TEXT else []
    except provider.errors as exc:
        st.warning(f"Vorgänger nicht ladbar: {exc}")
        return
    if refs:
        chosen = st.multiselect(
            "Direkte Vorgänger als Werk übernehmen (eine Ebene)",
            refs,
            default=refs,
            format_func=operator.itemgetter(1),
            key=f"{key}_predecessors",
        )
        predecessors = {ref[0] for ref in chosen}
    if st.button("Bestätigen & leere Felder füllen", key=f"{key}_apply", type="primary"):
        _apply(session, provider, kind, entity.id, hit, predecessors)


def render_authority_panel(kind: EntityType, row: dict) -> None:
    """Show GND and Wikidata candidates for the selected entity; writing needs the confirm click."""
    with st.expander("Normdaten (GND · Wikidata)", expanded=True), get_session() as session:
        entity = session.get(MODELS[kind], UUID(str(row["id"])))
        if entity is None:
            return
        for tab, provider in zip(st.tabs([p.name for p in PROVIDERS]), PROVIDERS, strict=True):
            with tab:
                _panel(session, provider, kind, entity)
