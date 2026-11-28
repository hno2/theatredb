"""Admin-only, paginated mention scans and context-aware decisions."""

import streamlit as st

from backend import mentions
from backend.config import ENTITY_LABELS, REVIEW_STATUS_LABELS, role_label
from backend.reconciliation import search_entities
from frontend.auth import principal
from frontend.views.helpers import entity_link

CONFIDENCE = {"high": "Hoch (Graph)", "medium": "Mittel", "ambiguous": "Mehrdeutig"}


def render() -> None:
    """Review mention proposals without importing or merging entities."""
    user = principal()
    if user is None or not user.is_admin:
        st.error("Nur Administratoren können Erwähnungen prüfen.")
        return
    st.caption(
        "Graphgestützte Treffer werden verlinkt. Mittlere Sicherheit und Mehrdeutigkeiten benötigen Zustimmung. "
        "Scans ändern weder Beschreibungen noch Beziehungen."
    )
    _scan()
    _inbox(str(user.id))


def _scan() -> None:
    with st.expander("Beschreibungen nach Erwähnungen durchsuchen"):
        kind = st.selectbox(
            "Beschreibungen",
            list(mentions.OWNER_KINDS),
            format_func=ENTITY_LABELS.__getitem__,
            key="mention_scan_kind",
        )
        page = st.number_input("Scan-Seite (50 Beschreibungen)", min_value=1, step=1, key="mention_scan_page")
        if st.button("Diese Seite prüfen", key="mention_scan"):
            with st.spinner("Erwähnungen und Beziehungen prüfen …"):
                result = mentions.scan_descriptions(kind, offset=(page - 1) * 50)
            st.success(f"{result['scanned']} Beschreibungen geprüft, {result['queued']} neue Prüffälle.")
            if result["scanned"] == 50:
                st.caption("Für weitere Beschreibungen die nächste Scan-Seite wählen.")


def _inbox(actor_id: str) -> None:
    status = st.selectbox(
        "Erwähnungsstatus",
        ["pending", "approved", "rejected"],
        format_func=REVIEW_STATUS_LABELS.__getitem__,
        key="mention_status",
    )
    page = st.number_input("Prüffall-Seite (50 Fälle)", min_value=1, step=1, key="mention_case_page")
    cases = mentions.mention_cases(status=status, offset=(page - 1) * 50)
    if not cases:
        st.info("Keine Erwähnungsfälle auf dieser Seite.")
        return
    by_id = {case["id"]: case for case in cases}
    case_id = st.selectbox(
        "Erwähnung",
        list(by_id),
        format_func=lambda key: f"{by_id[key]['incoming']['quote']} · {key[:8]}",
        key=f"mention_case_{status}_{page}",
    )
    case = by_id[case_id]
    incoming = case["incoming"]
    entity_link("Beschreibung öffnen", incoming["owner_kind"], incoming["owner_id"])
    if status != "pending":
        st.json(case)
        return
    try:
        _pending_case(case_id, actor_id)
    except ValueError as exc:
        st.warning(f"Prüffall nicht aktuell: {exc}. Beschreibungen erneut scannen bzw. Belege aktualisieren.")


def _select_target(case_id: str, initial: dict) -> dict | None:
    options = {f"{row['kind']}:{row['id']}": row for row in initial["match"]["candidates"]}
    kind = st.selectbox(
        "Zusätzliches Ziel suchen",
        list(mentions.TARGET_KINDS),
        format_func=ENTITY_LABELS.__getitem__,
        key=f"mention_target_kind_{case_id}",
    )
    query = st.text_input("Name, Titel oder UUID", key=f"mention_query_{case_id}", type="search", live="500ms")
    if query.strip():
        for row in search_entities(kind, query):
            options[f"{kind}:{row['id']}"] = {
                "kind": kind,
                "id": row["id"],
                "label": row.get("name") or row.get("title"),
            }
    choice = st.selectbox(
        "Bestehenden Eintrag verlinken",
        [None, *options],
        format_func=lambda key: (
            "Kein Ziel gewählt"
            if key is None
            else f"{ENTITY_LABELS[options[key]['kind']]} · {options[key]['label']} · {options[key]['id'][:8]}"
        ),
        key=f"mention_target_{case_id}",
    )
    return options[choice] if choice else None


def _cached_preview(case_id: str, base_key: str, target: dict | None = None) -> dict:
    key = f"{base_key}_{target['kind']}_{target['id']}" if target else base_key
    if key not in st.session_state:
        st.session_state[key] = mentions.preview_mention(
            case_id,
            target_kind=target["kind"] if target else None,
            target_id=target["id"] if target else None,
        )
    return st.session_state[key]


def _pending_case(case_id: str, actor_id: str) -> None:
    base_key = f"mention_preview_{case_id}"
    if st.button("Kontext und Belege aktualisieren", key=f"mention_refresh_{case_id}"):
        for key in list(st.session_state):
            if isinstance(key, str) and key.startswith(base_key):
                del st.session_state[key]
    initial = _cached_preview(case_id, base_key)
    st.text(initial["match"]["context"])
    st.caption(f"Sicherheit: {CONFIDENCE[initial['match']['confidence']]}")
    target = _select_target(case_id, initial)
    preview = _cached_preview(case_id, base_key, target) if target else initial
    if target:
        for path in initial["match"]["evidence"].get(target["id"], []):
            st.caption(
                " · ".join(
                    filter(
                        None,
                        (
                            path["path"],
                            role_label(path["role"]),
                            str(path["year"] or ""),
                            f"ab {path['start_year']}" if path.get("start_year") else "",
                            f"bis {path['end_year']}" if path.get("end_year") else "",
                        ),
                    )
                )
            )
        with st.expander("Zieldaten"):
            st.json(preview["target_state"])
    note = st.text_input("Entscheidungsgrund", key=f"mention_note_{case_id}")
    approve, reject = st.columns(2)
    if (
        approve.button("Verlinken", disabled=not target or not note.strip(), key=f"mention_approve_{case_id}")
        and target
    ):
        mentions.decide_mention(
            case_id,
            revision=preview["revision"],
            actor_id=actor_id,
            note=note,
            target_kind=target["kind"],
            target_id=target["id"],
        )
        st.rerun()
    if reject.button("Nicht verlinken", disabled=not note.strip(), key=f"mention_reject_{case_id}"):
        mentions.decide_mention(case_id, revision=initial["revision"], actor_id=actor_id, note=note)
        st.rerun()
