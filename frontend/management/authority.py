"""Authority lookup for the selected entity: GND and Wikidata side by side, one click fills blanks from both."""

from __future__ import annotations

from uuid import UUID

import streamlit as st
from sqlmodel import Session

from backend.authority_match import (
    GND,
    MODELS,
    NETWORK_ERRORS,
    PROVIDERS,
    WIKIDATA,
    Proposal,
    Provider,
    agreement,
    label,
    propose,
)
from backend.models import EntityType, Organization, Person, Text
from backend.reconciliation import normalize
from backend.user_management import get_session
from frontend.management.uifactory import refresh_edit_form

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


def _choose(provider: Provider, hits: list[dict], proposal: Proposal | None, key: str) -> dict | None:
    def describe(hit: dict | None) -> str:
        if hit is None:
            return "— nicht übernehmen —"
        mark = "✓ " if proposal and hit["id"] == proposal.external_id else ""
        description = f" — {hit['description']}" if hit["description"] else ""
        return f"{mark}{hit['label']}{description} ({hit['id']})"

    return st.selectbox(provider.name, [*hits, None], format_func=describe, key=f"{key}_hit")


def _column(
    session: Session, provider: Provider, kind: EntityType, entity: Person | Text | Organization, hint: str | None
) -> dict | None:
    """One provider's candidates; ``hint`` is the ID the other provider's choice declares identical."""
    key = f"authority_{provider.name}_{kind}_{entity.id}"
    try:
        hits, linked, proposal = _hits(session, provider, kind, entity, key)
        if st.session_state.get(f"{key}_manual"):
            hint = None
        if hint and not linked and hint not in {hit["id"] for hit in hits}:
            hits = [_hit(provider, _fetch(provider.name, hint)), *hits]
    except (ValueError, *provider.errors) as exc:
        st.warning(f"{provider.name} nicht erreichbar oder ID ungültig: {exc}")
        return None
    if hint and not linked and proposal is None:
        hits = sorted(hits, key=lambda hit: hit["id"] != hint)
    if not hits:
        st.caption(f"Kein {provider.name}-Treffer für „{label(entity)}“.")
        return None
    hit = _choose(provider, hits, proposal, key)
    if hit is None:
        return None
    st.markdown(" · ".join(filter(None, [f"**{hit['label']}**", hit["description"]])))
    st.link_button("Eintrag öffnen", hit["url"], icon=":material/open_in_new:")
    if linked:
        st.caption("Bereits verknüpft; füllt weiterhin nur leere Felder.")
    elif proposal and proposal.external_id == hit["id"]:
        st.success(f"Hohe Sicherheit: {proposal.evidence}")
    elif hint == hit["id"]:
        st.success("Querverweis aus dem anderen Normdatensatz")
    else:
        st.caption("Nur Namenstreffer: Identität anhand des Eintrags prüfen.")
    return hit


def _predecessor_choice(chosen: dict[Provider, dict], key: str) -> dict[str, set[str]] | None:
    """One multiselect over both providers; a Wikidata work titled like a GND one is offered once (GND wins)."""
    options: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for provider, hit in chosen.items():
        try:
            refs = _predecessors(provider.name, hit["id"], hit["record"])
        except provider.errors as exc:
            st.warning(f"{provider.name}-Vorgänger nicht ladbar: {exc}")
            return None
        for ref_id, title in refs:
            if normalize(title) not in seen:
                seen.add(normalize(title))
                options.append((provider.name, ref_id, title))
    selected = {provider.name: set() for provider in chosen}
    if options:
        picked = st.multiselect(
            "Direkte Vorgänger als Werk übernehmen (eine Ebene)",
            options,
            default=options,
            format_func=lambda option: f"{option[2]} ({option[0]})",
            key=f"{key}_predecessors",
        )
        for name, ref_id, _ in picked:
            selected[name].add(ref_id)
    return selected


def _apply(
    session: Session, kind: EntityType, entity_id: UUID, chosen: dict[Provider, dict], predecessors: dict[str, set[str]]
) -> dict | None:
    """All chosen providers in one transaction; fill keeps the most precise agreeing date whatever the order."""
    reports = {}
    try:
        for provider, hit in chosen.items():
            reports[provider.name] = provider.enrich(
                session,
                kind,
                entity_id,
                hit["id"],
                record=hit["record"],
                predecessor_ids=predecessors.get(provider.name),
            )
        session.commit()
    except (ValueError, *NETWORK_ERRORS) as exc:
        session.rollback()
        st.error(str(exc))
        return None
    return reports


def _show(reports: dict) -> None:
    filled = sorted({field for report in reports.values() for field in report["filled"]})
    st.success(f"Übernommen: {', '.join(filled) or 'keine leeren Felder'}.")
    conflicts = [(name, c) for name, report in reports.items() for c in report["conflicts"]]
    if conflicts:
        st.warning(
            "Abweichungen (nicht überschrieben): "
            + "; ".join(f"{c['field']}: {c['current']} ≠ {name} {c['authority']}" for name, c in conflicts)
        )
    with st.expander("Bericht"):
        st.json(reports)


def render_authority_panel(kind: EntityType, row: dict) -> None:
    """GND and Wikidata candidates side by side; one confirm click links and fills from both."""
    with st.expander("Normdaten (GND · Wikidata)", expanded=True), get_session() as session:
        entity = session.get(MODELS[kind], UUID(str(row["id"])))
        if entity is None:
            return
        key = f"authority_{kind}_{entity.id}"
        if reports := st.session_state.pop(f"{key}_reports", None):
            _show(reports)
        left, right = st.columns(2)
        with left:
            gnd_hit = _column(session, GND, kind, entity, None)
        hint = next(iter(sorted(GND.cross_ids(gnd_hit["record"]))), None) if gnd_hit else None
        with right:
            wikidata_hit = _column(session, WIKIDATA, kind, entity, hint)
        chosen = {provider: hit for provider, hit in ((GND, gnd_hit), (WIKIDATA, wikidata_hit)) if hit}
        if not chosen:
            return
        match = agreement(gnd_hit["record"], wikidata_hit["record"]) if gnd_hit and wikidata_hit else None
        if match is False:
            st.error("GND und Wikidata verweisen auf andere Einträge: einen Treffer abwählen oder korrigieren.")
            return
        if match:
            st.success("GND und Wikidata verweisen aufeinander.")
        elif len(chosen) == 2:
            st.caption("Kein Querverweis zwischen GND und Wikidata: Identität beider Einträge prüfen.")
        predecessors = _predecessor_choice(chosen, key) if kind == EntityType.TEXT else {}
        if predecessors is None:
            return
        st.caption("Leere Felder werden gefüllt, Daten in der genauesten Form; Beschreibung aus Wikidata.")
        if (
            st.button("Bestätigen & übernehmen", key=f"{key}_apply", type="primary")
            and (reports := _apply(session, kind, entity.id, chosen, predecessors)) is not None
        ):
            refresh_edit_form(entity.id)
            st.session_state[f"{key}_reports"] = reports
            st.rerun()
