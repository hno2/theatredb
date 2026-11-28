"""Administrative import and review overview."""

import datetime as dt

import streamlit as st
from sqlalchemy import func
from sqlmodel import col, select

from backend.config import IMPORT_RESULT_LABELS
from backend.crawling.service import ACTIVE
from backend.models import CrawlRun, ImportRun, IngestionRun, Organization, ReconciliationCase, Source
from backend.production_import import OPERATION
from backend.user_management import get_session


def _review_counts(groups: list[tuple[str, str | None, int]]) -> dict[str, int]:
    counts = {"production": 0, "text": 0, "other": 0, "theapolis": 0}
    for operation, source, count in groups:
        if operation == OPERATION:
            counts["production"] += count
        elif operation in {"link_text", "text_lineage"}:
            counts["text"] += count
        elif source == "theapolis.de":
            counts["theapolis"] += count
        else:
            counts["other"] += count
    return counts


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _render_ingestion(ingestion: IngestionRun) -> None:
    label = "Verlagsimport" if ingestion.kind == "publisher" else "Theapolis-Import"
    st.write(f"{label} · {ingestion.source_name} · {_utc(ingestion.imported_at):%d.%m.%Y %H:%M} UTC")
    if ingestion.file_name or ingestion.scope:
        st.caption(f"Datei: {ingestion.file_name or 'Nicht angegeben'} · Umfang: {ingestion.scope or 'Unbekannt'}")
    if ingestion.captured_at:
        st.caption(f"Quellstand: {_utc(ingestion.captured_at):%d.%m.%Y %H:%M} UTC")
    labels = {
        "observations": "Beobachtungen",
        "review": "Zur Prüfung",
        "organization": "Organisationen",
        "person": "Personen",
        "text": "Texte",
        "production": "Produktionen",
    }
    st.caption(" · ".join(f"{labels[key]}: {value}" for key, value in ingestion.stats.items() if key in labels))


def _latest_import(
    theatre: tuple[ImportRun, str, str] | None,
    ingestion: IngestionRun | None,
    crawl: tuple[CrawlRun, str] | None,
) -> None:
    candidates: list[tuple[dt.datetime, str]] = []
    if theatre:
        candidates.append((_utc(theatre[0].imported_at), "theatre"))
    if ingestion:
        candidates.append((_utc(ingestion.imported_at), "ingestion"))
    if crawl and crawl[0].finished_at:
        candidates.append((_utc(crawl[0].finished_at), "crawl"))
    if not candidates:
        st.info("Noch kein erfasster Import vorhanden.")
        return
    _, kind = max(candidates)
    if kind == "theatre" and theatre:
        run, organization, source = theatre
        st.write(f"Theater-Import · {organization} · {source} · {_utc(run.imported_at):%d.%m.%Y %H:%M} UTC")
        st.caption(
            f"Datei: {run.file_name or 'Nicht angegeben'} · "
            f"Quellstand: {_utc(run.captured_at).strftime('%d.%m.%Y %H:%M UTC') if run.captured_at else 'Unbekannt'} · "
            f"Datensätze: {run.total}"
        )
        for column, (key, label) in zip(
            st.columns(len(IMPORT_RESULT_LABELS)), IMPORT_RESULT_LABELS.items(), strict=True
        ):
            column.metric(label, getattr(run, key))
    elif kind == "ingestion" and ingestion:
        _render_ingestion(ingestion)
    elif crawl and crawl[0].finished_at:
        run, source = crawl
        st.write(f"Crawl-Import · {source} · {_utc(crawl[0].finished_at):%d.%m.%Y %H:%M} UTC")
        st.caption(
            f"Datensätze: {run.stats.get('records', 'Unbekannt')} · "
            f"Geändert: {run.stats.get('changed', 'Unbekannt')} · "
            f"Unverändert: {run.stats.get('unchanged', 'Unbekannt')}"
        )


def _render_crawls(
    latest: tuple[CrawlRun, str] | None, active: int, failed_sources: list[tuple[str, str | None]]
) -> None:
    st.header("Letzter Crawl")
    st.caption(f"Aktive Crawls: {active}")
    if latest:
        crawl, source = latest
        st.write(f"{source} · Angefordert: {crawl.created_at:%d.%m.%Y %H:%M} UTC · Status: {crawl.status}")
        if crawl.finished_at:
            st.caption(f"Beendet: {crawl.finished_at:%d.%m.%Y %H:%M} UTC")
        if crawl.error:
            st.error(f"Crawl-Fehler: {crawl.error}")
    else:
        st.info("Noch kein Crawl vorhanden.")
    if failed_sources:
        st.warning(f"Letzte Crawls fehlgeschlagen: {len(failed_sources)}.")
        for source, error in failed_sources[:5]:
            st.text(f"{source} · {error or 'Kein Fehlertext'}")
        if len(failed_sources) > 5:
            st.caption("Weitere fehlgeschlagene Läufe stehen unter Quellen & Crawls.")
    st.page_link("management/sources.py", label="Quellen & Crawls öffnen")


def render() -> None:
    """Show the latest persisted activity and outstanding editorial work."""
    st.title("Verwaltung: Übersicht")
    st.caption("Letzte Importe und offene Aufgaben für die Redaktion.")

    with get_session() as session:
        latest_import = session.exec(
            select(ImportRun, Organization.name, Source.name)
            .join(Organization, col(Organization.id) == ImportRun.organization_id)
            .join(Source, col(Source.id) == ImportRun.source_id)
            .order_by(col(ImportRun.imported_at).desc(), col(ImportRun.id).desc())
            .limit(1)
        ).first()
        latest_crawl = session.exec(
            select(CrawlRun, Source.name)
            .join(Source, col(Source.id) == CrawlRun.source_id)
            .order_by(col(CrawlRun.created_at).desc(), col(CrawlRun.id).desc())
            .limit(1)
        ).first()
        latest_completed_crawl = session.exec(
            select(CrawlRun, Source.name)
            .join(Source, col(Source.id) == CrawlRun.source_id)
            .where(CrawlRun.status == "succeeded", col(CrawlRun.finished_at).is_not(None))
            .order_by(col(CrawlRun.finished_at).desc(), col(CrawlRun.id).desc())
            .limit(1)
        ).first()
        latest_ingestion = session.exec(
            select(IngestionRun).order_by(col(IngestionRun.imported_at).desc(), col(IngestionRun.id).desc()).limit(1)
        ).first()
        active_crawls = session.exec(
            select(func.count()).select_from(CrawlRun).where(col(CrawlRun.status).in_(ACTIVE))
        ).one()
        ranked_crawls = select(
            CrawlRun.id,
            func.row_number()
            .over(
                partition_by=col(CrawlRun.source_id),
                order_by=(col(CrawlRun.created_at).desc(), col(CrawlRun.id).desc()),
            )
            .label("rank"),
        ).subquery()
        failed_sources = session.exec(
            select(Source.name, CrawlRun.error)
            .join(CrawlRun, col(CrawlRun.source_id) == Source.id)
            .join(ranked_crawls, ranked_crawls.c.id == col(CrawlRun.id))
            .where(ranked_crawls.c.rank == 1, CrawlRun.status == "failed")
            .order_by(col(Source.name))
        ).all()
        oldest_case = session.exec(
            select(func.min(ReconciliationCase.created_at)).where(ReconciliationCase.status == "pending")
        ).one()
        groups = session.exec(
            select(ReconciliationCase.operation, Source.name, func.count())
            .outerjoin(Source, col(Source.id) == ReconciliationCase.source_id)
            .where(ReconciliationCase.status == "pending")
            .group_by(ReconciliationCase.operation, Source.name)
        ).all()
        flagged_count = session.exec(
            select(func.count()).select_from(Organization).where(col(Organization.update_scope).is_not(None))
        ).one()
        flagged = session.exec(
            select(Organization)
            .where(col(Organization.update_scope).is_not(None))
            .order_by(col(Organization.name), col(Organization.id))
            .limit(10)
        ).all()

    st.header("Letzter Import")
    _latest_import(latest_import, latest_ingestion, latest_completed_crawl)
    st.page_link("management/reconciliation.py", label="Datenabgleich & Theater-Uploads öffnen")

    _render_crawls(latest_crawl, active_crawls, list(failed_sources))

    counts = _review_counts(list(groups))
    st.header("Offene Aufgaben")
    if oldest_case:
        st.caption(f"Ältester offener Prüffall: {_utc(oldest_case):%d.%m.%Y %H:%M} UTC")
    columns = st.columns(4)
    columns[0].metric("Produktionskonflikte", counts["production"])
    columns[1].metric("Andere Prüffälle", counts["other"])
    columns[2].metric("Text-Prüffälle", counts["text"])
    columns[3].metric("Aktualisierungen", flagged_count)
    st.page_link(
        "management/reconciliation.py",
        label="Produktionskonflikte und andere Prüffälle bearbeiten",
        help="Andere Prüffälle stehen im Reiter „Andere Quellen & gemeldete Duplikate“.",
    )
    st.page_link("management/texts.py", label="Text-Prüffälle bearbeiten")
    if counts["theapolis"]:
        st.caption(
            f"Zusätzlich {counts['theapolis']} offene Theapolis-Prüffälle; "
            "diese sind nicht im allgemeinen Prüffall-Reiter enthalten."
        )
    if flagged:
        st.subheader("Vorgemerkte Aktualisierungen")
        for organization in flagged:
            st.text(f"{organization.name} · {organization.update_reason}")
        if flagged_count > len(flagged):
            st.caption(f"Die ersten {len(flagged)} von {flagged_count} vorgemerkten Organisationen.")
        st.page_link("management/organizations.py", label="Organisationen bearbeiten")


if __name__ == "__main__":
    render()
