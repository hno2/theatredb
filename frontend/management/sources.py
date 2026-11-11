import json
import logging
import uuid

import streamlit as st
import yaml
from procrastinate.exceptions import ProcrastinateException
from sqlalchemy.exc import SQLAlchemyError

from backend.crawling import service
from backend.crawling.config import RecordCrawl, TheatreCrawl
from backend.entity_management import list_entities
from backend.ingestion.configurable import CONFIG_DIR, load_config
from backend.models import CrawlRun, Organization, Source
from backend.user_management import init_db
from frontend.management.uifactory import _actor_id, render_sqlmodel_crud

LOG = logging.getLogger(__name__)
ERRORS = (ValueError, TypeError, yaml.YAMLError, SQLAlchemyError, ProcrastinateException)


def _definition(kind: str, organization: str | None, text: str) -> dict:
    if kind == "theatre":
        if organization is None:
            raise ValueError("Ein bestehendes Theater muss ausgewählt werden")
        return TheatreCrawl.model_validate(
            {
                "kind": kind,
                "organization_id": organization,
                "config": yaml.safe_load(text),
            }
        ).model_dump(mode="json")
    return RecordCrawl.model_validate(json.loads(text)).model_dump(mode="json")


def _config_editor(source: Source) -> None:
    stored = service.configuration(source.id)
    definition = stored.definition if stored else {}
    with st.expander("Crawler konfigurieren", expanded=stored is None):
        kind = st.selectbox(
            "Crawler-Typ",
            ["theatre", "records"],
            index=0 if definition.get("kind", "theatre") == "theatre" else 1,
            format_func=lambda value: {
                "theatre": "Theater (YAML)",
                "records": "Weitere Quelle (Crawler-Konfiguration)",
            }[value],
            key=f"kind-{source.id}",
        )
        if kind == "theatre":
            bundled = sorted(path.stem for path in CONFIG_DIR.glob("*.yaml"))
            saved = definition.get("config") if definition.get("kind") == kind else None
            templates = ["Gespeicherte Konfiguration", *bundled] if saved else bundled
            template = st.selectbox("YAML-Vorlage", templates, key=f"template-{source.id}")
            config = (
                saved if template == "Gespeicherte Konfiguration" else load_config(template).model_dump(mode="json")
            )
            theatres = list_entities(Organization, order_by="name")
            by_id = {str(theatre.id): theatre.name for theatre in theatres}
            selected = str(definition.get("organization_id", ""))
            organization = st.selectbox(
                "Bestehendes Theater",
                list(by_id),
                index=list(by_id).index(selected) if selected in by_id else None,
                format_func=lambda value: by_id[value],
                key=f"theatre-{source.id}",
            )
            text = st.text_area(
                "Theater-YAML",
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                height=350,
                key=f"yaml-{source.id}-{template}",
            )
        else:
            default = {
                "kind": "records",
                "base_url": source.base_url or "https://example.org",
                "entity_type": "production",
                "listing": {
                    "urls": [source.base_url or "https://example.org"],
                    "selector": "a[href]",
                    "include": "/reviews/",
                    "max_pages": 20,
                },
                "fields": {
                    "name": {"selector": "h1", "required": True},
                    "critics": {"selector": ".review", "many": True},
                },
            }
            text = st.text_area(
                "Crawler-Konfiguration (JSON)",
                json.dumps(definition if definition.get("kind") == kind else default, ensure_ascii=False, indent=2),
                height=350,
                key=f"json-{source.id}",
            )
            st.caption(
                "Felder des gewählten Entitätstyps; z. B. Produktionsname und Kritiken. "
                "Ergebnisse werden unter Datenabgleich → Andere Quellen & gemeldete Duplikate geprüft, "
                "nicht als Theater-Upload."
            )
            organization = None
        enabled = st.checkbox("Crawler aktiviert", value=stored.enabled if stored else True, key=f"enabled-{source.id}")
        if st.button("Konfiguration speichern", key=f"save-{source.id}"):
            try:
                service.save_config(source.id, _definition(kind, organization, text), enabled=enabled)
            except ERRORS as error:
                LOG.exception("Cannot save crawler configuration for %s", source.id)
                st.error(str(error))
            else:
                st.success("Konfiguration gespeichert")
                st.rerun()


@st.fragment(run_every="5s")
def _run_controls(source_id: uuid.UUID) -> None:
    history = service.runs(source_id)
    active = next((run for run in history if run.status in service.ACTIVE), None)
    config = service.configuration(source_id)
    st.caption("Läufe und Fortschritt werden alle 5 Sekunden aus der Datenbank aktualisiert.")
    force = st.checkbox("Alle Seiten neu laden (ohne HTTP-Cache)", key=f"force-{source_id}")
    limited = st.checkbox("Begrenzter Testlauf", key=f"limited-{source_id}")
    limit = (
        st.number_input("Maximale Datensätze", min_value=1, value=5, step=1, key=f"limit-{source_id}")
        if limited
        else None
    )
    people = True
    if config and config.definition["kind"] == "theatre":
        people = st.checkbox("Personenprofile ergänzen", value=True, key=f"people-{source_id}")
    if limited:
        st.warning("Auch Testläufe importieren Änderungen nach den bestehenden Regeln.")
    if st.button(
        "Crawl starten",
        disabled=active is not None or config is None or not config.enabled,
        key=f"start-{source_id}",
    ):
        try:
            service.start(source_id, actor_id=_actor_id(), force_fetch=force, limit=limit, enrich_people=people)
        except ERRORS as error:
            LOG.exception("Cannot start crawler for %s", source_id)
            st.error(f"Crawl konnte nicht gestartet werden: {error}")
        else:
            st.rerun()
    if active:
        _active_controls(active)
    _history(history)


def _active_controls(active: CrawlRun) -> None:
    st.info(f"Aktueller Lauf: {active.status} · {active.phase}")
    st.json(active.stats)
    if st.button("Crawl stoppen", disabled=active.status == "cancelling", key=f"stop-{active.source_id}"):
        try:
            service.stop(active.id)
        except ERRORS as error:
            LOG.exception("Cannot stop crawler %s", active.id)
            st.error(str(error))
        else:
            st.rerun()
    if service.is_stale(active):
        st.warning("Worker meldet sich seit mehr als 3 Minuten nicht mehr.")
        if st.button("Verwaisten Lauf als fehlgeschlagen beenden", key=f"stale-{active.source_id}"):
            try:
                service.fail_stale(active.id)
            except ERRORS as error:
                LOG.exception("Cannot end stale crawl %s", active.id)
                st.error(str(error))
            else:
                st.rerun()


def _history(history: list[CrawlRun]) -> None:
    if not history:
        st.info("Noch keine Crawl-Läufe für diese Quelle.")
        return
    st.subheader("Letzte Läufe")
    st.dataframe(
        [
            {
                "Status": run.status,
                "Phase": run.phase,
                "Gestartet": run.started_at,
                "Angefordert": run.created_at,
                "Beendet": run.finished_at,
                "Umfang": f"limit-{run.limit}" if run.limit else "all",
                "Datensätze": run.stats.get("records"),
                "Geändert": run.stats.get("changed"),
                "Unverändert": run.stats.get("unchanged"),
                "HTTP 304": run.stats.get("not_modified"),
                "Fehler": run.error,
            }
            for run in history
        ],
        hide_index=True,
        width="stretch",
    )
    for run in history[:5]:
        with st.expander(f"{run.created_at} · {run.status}"):
            st.json(run.stats)
            if run.error:
                st.error(run.error)
            if run.artifact_path:
                st.caption(f"Staging-Datei (Worker): {run.artifact_path}")
            st.json(run.definition, expanded=False)


def main() -> None:
    """Render source metadata and source-owned crawler configuration/history."""
    init_db()
    st.title("Quellen & Crawls")
    crawls, metadata = st.tabs(["Crawler", "Quellen bearbeiten"])
    with metadata:
        render_sqlmodel_crud(model_cls=Source)
    with crawls:
        sources = list_entities(Source, order_by="name")
        if not sources:
            st.info("Zuerst eine Quelle unter „Quellen bearbeiten“ anlegen.")
            return
        by_id = {str(source.id): source for source in sources}
        selected = st.selectbox("Quelle", list(by_id), format_func=lambda value: by_id[value].name)
        if selected is not None:
            source = by_id[selected]
            _config_editor(source)
            _run_controls(source.id)


if __name__ == "__main__":
    main()
