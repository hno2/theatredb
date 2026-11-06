"""Scraping status per theatre: latest uploads, coverage by sources and how complete production data is."""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Collection

from sqlalchemy import func
from sqlmodel import Session, col, select

from backend.config import IMPORT_COVERAGE_FIELDS as FIELDS
from backend.config import canonical_role
from backend.models import (
    EntitySource,
    EntityType,
    ImportRun,
    Organization,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Source,
    SourceRecord,
)
from backend.production_import import OPERATION, url_host
from backend.user_management import get_session


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def data_date(run: ImportRun) -> dt.datetime:
    """When the uploaded data was scraped; the import time if the capture date is unknown."""
    return _as_utc(run.captured_at or run.imported_at)


def _run_row(run: ImportRun, sources: dict[uuid.UUID, str], now: dt.datetime) -> dict:
    return {
        "data_date": data_date(run),
        "age_days": (now - data_date(run)).days,
        "captured_known": run.captured_at is not None,
        "imported_at": _as_utc(run.imported_at),
        "source": sources.get(run.source_id, ""),
        "website": run.website,
        "file_name": run.file_name,
        "scope": run.scope,
        "total": run.total,
        "merged": run.merged,
        "created": run.created,
        "review": run.review,
        "unchanged": run.unchanged,
    }


def _production_stats(
    session: Session, organization_ids: Collection[uuid.UUID], own_hosts: dict[uuid.UUID, set[str]]
) -> dict[uuid.UUID, dict]:
    """Per theatre: productions, how many have an uploaded / own-website source and filled core fields."""
    stats: dict[uuid.UUID, dict] = {
        org_id: {"productions": 0, "scraped": 0, "own_website": 0, "fields": dict.fromkeys(FIELDS, 0)}
        for org_id in organization_ids
    }
    if not organization_ids:
        return stats
    in_theatres = col(Production.organization_id).in_(organization_ids)
    productions = session.exec(select(Production).where(in_theatres)).all()
    roles: dict[uuid.UUID, set[str | None]] = defaultdict(set)
    for production_id, role in session.exec(
        select(ProductionMembership.production_id, ProductionMembership.role)
        .join(Production, col(Production.id) == ProductionMembership.production_id)
        .where(in_theatres)
    ).all():
        roles[production_id].add(canonical_role(role))
    scraped = set(
        session.exec(
            select(SourceRecord.entity_id)
            .join(Production, col(Production.id) == SourceRecord.entity_id)
            .where(SourceRecord.entity_type == EntityType.PRODUCTION, in_theatres)
        ).all()
    )
    urls: dict[uuid.UUID, set[str | None]] = defaultdict(set)
    for model in (SourceRecord, EntitySource):
        for production_id, url in session.exec(
            select(model.entity_id, model.external_url)
            .join(Production, col(Production.id) == model.entity_id)
            .where(model.entity_type == EntityType.PRODUCTION, in_theatres)
        ).all():
            urls[production_id].add(url_host(url))
    for production in productions:
        if production.organization_id is None:
            continue
        row = stats[production.organization_id]
        hosts = {url_host(production.url)} | urls[production.id]
        row["productions"] += 1
        row["scraped"] += production.id in scraped
        row["own_website"] += bool(hosts & own_hosts.get(production.organization_id, set()))
        filled = {
            "description": bool(production.description and production.description.strip()),
            "premiere": production.premiere is not None,
            "director": "director" in roles[production.id],
            "cast": "actor" in roles[production.id],
            "text": production.text_id is not None,
        }
        for field, value in filled.items():
            row["fields"][field] += value
    return stats


def _own_hosts(organizations: dict[uuid.UUID, Organization], runs: list[ImportRun]) -> dict[uuid.UUID, set[str]]:
    """The theatre's website from its URL fields; otherwise the sites its uploads were scraped from."""
    hosts: dict[uuid.UUID, set[str]] = {}
    for org_id, organization in organizations.items():
        hosts[org_id] = {host for host in (url_host(organization.url), url_host(organization.url_plays)) if host}
    for run in runs:
        if not (organizations[run.organization_id].url or organizations[run.organization_id].url_plays) and run.website:
            hosts[run.organization_id].add(run.website)
    return {
        org_id: {host for host in values if host != "theapolis.de" and not host.endswith(".theapolis.de")}
        for org_id, values in hosts.items()
    }


def _open_conflicts(session: Session) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for context in session.exec(
        select(ReconciliationCase.context).where(
            ReconciliationCase.operation == OPERATION, ReconciliationCase.status == "pending"
        )
    ).all():
        counts[context["organization_id"]] += 1
    return counts


def _theatre_rows(session: Session, organization_ids: Collection[uuid.UUID], now: dt.datetime) -> list[dict]:
    organizations = {
        org.id: org for org in session.exec(select(Organization).where(col(Organization.id).in_(organization_ids)))
    }
    runs = list(
        session.exec(
            select(ImportRun)
            .where(col(ImportRun.organization_id).in_(organization_ids))
            .order_by(col(ImportRun.imported_at).desc())
        ).all()
    )
    sources = {source.id: source.name for source in session.exec(select(Source)).all()}
    own_hosts = _own_hosts(organizations, runs)
    stats = _production_stats(session, list(organizations), own_hosts)
    conflicts = _open_conflicts(session)
    by_theatre: dict[uuid.UUID, list[ImportRun]] = defaultdict(list)
    for run in runs:
        by_theatre[run.organization_id].append(run)
    return [
        {
            "organization_id": str(org_id),
            "theatre": organization.name,
            "own_hosts": sorted(own_hosts[org_id]),
            "runs": [_run_row(run, sources, now) for run in by_theatre[org_id]],
            "latest": _run_row(max(by_theatre[org_id], key=data_date), sources, now) if by_theatre[org_id] else None,
            "open_conflicts": conflicts.get(str(org_id), 0),
            **stats[org_id],
        }
        for org_id, organization in organizations.items()
    ]


def theatre_status(organization_id: uuid.UUID | str, *, now: dt.datetime | None = None) -> dict:
    """Uploads (newest first), latest data date, source coverage and field completeness of one theatre."""
    with get_session() as session:
        rows = _theatre_rows(session, [uuid.UUID(str(organization_id))], now or dt.datetime.now(dt.UTC))
    if not rows:
        raise ValueError("Theatre not found")
    return rows[0]


def scrape_overview(*, recent_days: int = 90, now: dt.datetime | None = None) -> dict:
    """How many theatres have recently uploaded data, plus a status row for every theatre with uploads.

    The denominator counts theatres with at least one production; rows are sorted stalest first.
    """
    now = now or dt.datetime.now(dt.UTC)
    with get_session() as session:
        theatres = session.exec(
            select(func.count(func.distinct(Production.organization_id))).where(
                col(Production.organization_id).is_not(None)
            )
        ).one()
        uploaded = set(session.exec(select(ImportRun.organization_id).distinct()).all())
        rows = _theatre_rows(session, uploaded, now) if uploaded else []
    cutoff = now - dt.timedelta(days=recent_days)
    rows.sort(key=lambda row: row["latest"]["data_date"])
    return {
        "recent_days": recent_days,
        "theatres": max(theatres, len(rows)),
        "uploaded": len(rows),
        "recent": sum(row["latest"]["data_date"] >= cutoff for row in rows),
        "rows": rows,
    }
