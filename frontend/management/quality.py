"""Focused editorial actions; ordinary CRUD must not manufacture verification."""

from operator import itemgetter
from uuid import UUID

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from backend import config
from backend.data_quality import apply_quality_action, season_checks_many, season_label
from backend.entity_management import entities_by_ids
from backend.models import Organization
from frontend.management.uifactory import _actor_id
from frontend.views.organization import load_organization_profile, render_quality_summary

_SECTIONS = {
    "season": "Aktuelle Spielzeit",
    "general": "Allgemeine Angaben",
    "history": "Produktionshistorie",
    "update": "Aktualisierung",
}


def _save(
    organization_id: UUID,
    action: str,
    *,
    evidence: str = "",
    season_start_year: int | None = None,
    history_from_season: int | None = None,
    history_through_season: int | None = None,
    update_scope: str | None = None,
    update_reason: str | None = None,
) -> None:
    try:
        apply_quality_action(
            organization_id,
            action,
            actor_id=_actor_id(),
            evidence=evidence,
            season_start_year=season_start_year,
            history_from_season=history_from_season,
            history_through_season=history_through_season,
            update_scope=update_scope,
            update_reason=update_reason,
        )
    except (ValueError, PermissionError, SQLAlchemyError) as exc:
        st.error(f"Datenqualität konnte nicht gespeichert werden: {exc}")
        return
    load_organization_profile.clear()
    st.session_state["_flash"] = "Datenqualität gespeichert."
    st.rerun()


def _remove(organization_id: UUID, action: str, *, season: int | None = None) -> None:
    confirmed = st.checkbox(
        "Diese Prüfung ist nicht mehr gültig und soll zurückgenommen werden.",
        key=f"quality_remove_confirm_{organization_id}_{action}_{season}",
    )
    if st.button(
        "Prüfung zurücknehmen",
        key=f"quality_remove_{organization_id}_{action}_{season}",
        disabled=not confirmed,
    ):
        _save(organization_id, action, season_start_year=season)


def _season(data: dict, organization_id: UUID, key: str) -> None:
    current = config.CURRENT_SEASON_START_YEAR
    st.caption(
        "Ein Klick bestätigt: Das verfügbare Programm wurde mit der Quelle abgeglichen. "
        "Spätere Ankündigungen können fehlen; auch ein leeres Programm kann geprüft werden."
    )
    season = int(
        st.number_input(
            "Spielzeit beginnt im Jahr",
            min_value=1000,
            max_value=current,
            value=current,
            key=f"{key}_season",
            help=f"Standard: aktuelle Spielzeit {season_label(current)}.",
        )
    )
    existing = next((check for check in data["season_checks"] if check["season_start_year"] == season), None)
    if existing:
        st.caption(f"Zuletzt geprüft: {existing['checked_at']} · {existing['checked_by']}")
    with st.form(f"{key}_season_form"):
        evidence = st.text_input(
            "Quelle / Beleg",
            value=(existing or {}).get("evidence") or data.get("url_plays") or data.get("url") or "",
            key=f"{key}_season_evidence_{season}",
            help="Website, gedrucktes Programm oder kurze Quellenangabe. Wird öffentlich angezeigt.",
        )
        if st.form_submit_button(f"Spielzeit {season_label(season)} als geprüft bestätigen", type="primary"):
            _save(organization_id, "check_season", evidence=evidence, season_start_year=season)
    if existing:
        _remove(organization_id, "remove_season", season=season)
    if data["season_checks"]:
        st.caption("Bisher geprüfte Spielzeiten")
        for check in sorted(data["season_checks"], key=itemgetter("season_start_year"), reverse=True):
            st.text(f"{season_label(check['season_start_year'])} · {check['checked_at']} · {check['checked_by']}")


def _general(data: dict, organization_id: UUID, key: str) -> None:
    st.text(" · ".join(str(data[field]) for field in ("name", "type") if data.get(field)))
    st.text(" · ".join(str(data[field]) for field in ("street", "zip_code", "city", "country") if data.get(field)))
    st.text(data.get("url") or "Keine Website hinterlegt")
    st.caption(
        "Bestätige Name, Standort, Website und Organisationstyp anhand der Quelle. "
        "Unbekannte oder nicht zutreffende optionale Angaben müssen nicht erfunden werden."
    )
    with st.form(f"{key}_general_form"):
        evidence = st.text_input(
            "Quelle / Beleg",
            value=data.get("general_verification_evidence") or data.get("url") or "",
            key=f"{key}_general_evidence",
            help="Wird öffentlich angezeigt.",
        )
        if st.form_submit_button("Allgemeine Angaben als geprüft bestätigen", type="primary"):
            _save(organization_id, "verify_general", evidence=evidence)
    if data.get("general_verified_at"):
        st.caption(f"Geprüft von {data['general_verified_by']} · {data['general_verified_at']}")
        _remove(organization_id, "withdraw_general")


def _history(data: dict, organization_id: UUID, key: str) -> None:
    current = config.CURRENT_SEASON_START_YEAR
    st.caption(
        "Bestätigt die vollständige Liste der Produktionen im angegebenen Zeitraum, "
        "nicht die Vollständigkeit jeder Besetzung oder jedes Datenfelds."
    )
    with st.form(f"{key}_history_form"):
        first = int(
            st.number_input(
                "Ab Spielzeit (Startjahr)",
                min_value=1000,
                max_value=current,
                value=data.get("history_from_season") or current,
                key=f"{key}_history_from",
            )
        )
        last = int(
            st.number_input(
                "Bis einschließlich Spielzeit (Startjahr)",
                min_value=1000,
                max_value=current,
                value=data.get("history_through_season") or current,
                key=f"{key}_history_through",
            )
        )
        evidence = st.text_input(
            "Archiv / Beleg",
            value=data.get("history_verification_evidence") or data.get("url_plays") or data.get("url") or "",
            key=f"{key}_history_evidence",
            help="Wird öffentlich angezeigt.",
        )
        if st.form_submit_button("Historie für diesen Zeitraum als vollständig bestätigen", type="primary"):
            _save(
                organization_id,
                "verify_history",
                evidence=evidence,
                history_from_season=first,
                history_through_season=last,
            )
    if data.get("history_verified_at"):
        st.caption(f"Geprüft von {data['history_verified_by']} · {data['history_verified_at']}")
        _remove(organization_id, "withdraw_history")


def _update(data: dict, organization_id: UUID, key: str) -> None:
    scopes = {"general": "Allgemeine Angaben", "productions": "Produktionen", "both": "Beides"}
    with st.form(f"{key}_update_form"):
        scope = st.selectbox(
            "Was braucht eine Aktualisierung?",
            list(scopes),
            index=list(scopes).index(data.get("update_scope") or "productions"),
            format_func=scopes.__getitem__,
            key=f"{key}_update_scope",
        )
        reason = st.text_area(
            "Grund",
            value=data.get("update_reason") or "",
            key=f"{key}_update_reason",
            help="Kurzer Arbeitsauftrag; wird auch öffentlich angezeigt.",
        )
        if st.form_submit_button("Aktualisierung vormerken", type="primary"):
            _save(organization_id, "flag_update", update_scope=scope, update_reason=reason)
    if data.get("update_scope"):
        st.caption(f"Vorgemerkt von {data['update_flagged_by']} · {data['update_flagged_at']}")
        if st.button("Aktualisierung erledigt - Markierung aufheben", key=f"{key}_resolve"):
            _save(organization_id, "resolve_update")
        st.caption("Aufheben bestätigt keine Prüfung und ändert weder Historie noch Spielzeitprüfung.")


def render_quality_editor(data: dict) -> None:
    """Use the selected CRUD row and existing actor, defaulting to current-season review."""
    organization_id = UUID(str(data["id"]))
    organizations = entities_by_ids(Organization, {organization_id})
    if not organizations:
        st.error("Organisation wurde inzwischen entfernt. Bitte die Auswahl aktualisieren.")
        return
    data = organizations[0].model_dump(mode="json")
    checks = season_checks_many([organization_id]).get(organization_id, [])
    data = {**data, "season_checks": [check.model_dump(mode="json") for check in checks]}
    render_quality_summary(data)
    if not _actor_id():
        st.info("Zum Prüfen bitte mit einem Verwaltungskonto anmelden.")
        return
    key = f"quality_{organization_id}"
    section = st.segmented_control(
        "Datenqualität bearbeiten",
        list(_SECTIONS),
        default="season",
        required=True,
        format_func=_SECTIONS.__getitem__,
        key=f"{key}_section",
    )
    renderers = {"season": _season, "general": _general, "history": _history, "update": _update}
    renderers[section](data, organization_id, key)
