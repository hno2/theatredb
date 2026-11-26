"""Bulk authority pass: propose only high-confidence GND/Wikidata links, apply the ones the editor keeps."""

import streamlit as st

from backend.authority_match import NETWORK_ERRORS, PROVIDERS, apply, bulk_proposals
from backend.models import EntityType
from backend.user_management import get_session

KINDS = {
    "Personen": EntityType.PERSON,
    "Theater/Organisationen": EntityType.ORGANIZATION,
    "Texte": EntityType.TEXT,
}


def _scan(provider_name: str, kind_label: str, limit: int) -> None:
    provider = next(p for p in PROVIDERS if p.name == provider_name)
    state_key = f"bulk_{provider_name}_{kind_label}"
    offset = st.session_state.get(f"{state_key}_offset", 0)
    bar = st.progress(0.0, text="Abgleich läuft …")
    with get_session() as session:
        try:
            proposals, next_offset, done = bulk_proposals(
                session,
                provider,
                KINDS[kind_label],
                offset=offset,
                limit=limit,
                progress=lambda i, n: bar.progress(i / n, text=f"{i}/{n} geprüft"),
            )
        except NETWORK_ERRORS as exc:
            st.error(f"{provider_name} nicht erreichbar: {exc}")
            return
    bar.empty()
    st.session_state[f"{state_key}_offset"] = 0 if done else next_offset
    st.session_state["bulk_proposals"] = proposals
    st.session_state["bulk_status"] = (
        f"{len(proposals)} sichere Treffer unter Einträgen {offset + 1} bis {next_offset}."
        + (" Alle Einträge geprüft." if done else " Nächster Durchlauf setzt dort fort.")
    )


def _review() -> None:
    proposals = st.session_state.get("bulk_proposals") or []
    if status := st.session_state.get("bulk_status"):
        st.info(status)
    if not proposals:
        return
    edited = st.data_editor(
        [
            {
                "Übernehmen": True,
                "Eintrag": p.label,
                "Quelle": p.provider.name,
                "Treffer": p.provider.url(p.external_id),
                "Beleg": p.evidence,
            }
            for p in proposals
        ],
        column_config={"Treffer": st.column_config.LinkColumn(display_text=r".*/(.+)$")},
        disabled=["Eintrag", "Quelle", "Treffer", "Beleg"],
        hide_index=True,
        key="bulk_editor",
    )
    chosen = [p for p, row in zip(proposals, edited, strict=True) if row["Übernehmen"]]
    if st.button(f"{len(chosen)} übernehmen (leere Felder füllen)", type="primary", disabled=not chosen):
        with get_session() as session, st.spinner("Wird übernommen …"):
            results = apply(session, chosen)
        failed = [(p, r) for p, r in results if isinstance(r, str)]
        st.success(f"{len(results) - len(failed)} verknüpft und angereichert.")
        for proposal, error in failed:
            st.error(f"{proposal.label}: {error}")
        st.session_state["bulk_proposals"] = []


def main() -> None:
    """Render the bulk authority matching page."""
    st.title("Normdaten-Abgleich")
    st.caption(
        "Schlägt nur Treffer mit Beleg über den Namen hinaus vor: Querverweis GND↔Wikidata, "
        "Name + Lebensjahr, Titel + verknüpfte:r Autor:in oder Name + Ort. "
        "Übernahme füllt nur leere Felder; Vorgängerwerke bleiben der Einzelansicht vorbehalten. "
        "Reihenfolge: zuerst Personen, dann Texte (nutzen verknüpfte Autor:innen)."
    )
    cols = st.columns(3)
    kind_label = cols[0].selectbox("Bereich", list(KINDS))
    provider_name = cols[1].selectbox("Normdatei", [p.name for p in PROVIDERS])
    limit = cols[2].number_input("Einträge pro Durchlauf", min_value=5, max_value=200, value=25, step=5)
    if st.button("Sichere Treffer suchen"):
        _scan(provider_name, kind_label, int(limit))
    _review()


if __name__ == "__main__":
    main()
