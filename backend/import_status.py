"""Scraping status per theatre: latest uploads, coverage by sources and how complete production data is."""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable
from operator import itemgetter

from sqlalchemy import func, union_all
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Exists
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

# Match Python's str.strip() for descriptions in both PostgreSQL and SQLite.
_WHITESPACE = "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def data_date(run: ImportRun) -> dt.datetime:
    """When the uploaded data was scraped; the import time if the capture date is unknown."""
    return _as_utc(run.captured_at or run.imported_at)


def _run_row(run: ImportRun, source: str | None, now: dt.datetime) -> dict:
    return {
        "data_date": data_date(run),
        "age_days": (now - data_date(run)).days,
        "captured_known": run.captured_at is not None,
        "imported_at": _as_utc(run.imported_at),
        "source": source or "",
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
    role_column = col(ProductionMembership.role_name)
    roles: dict[str, set[str]] = defaultdict(set)
    # Normalize only distinct labels, keeping Python casefold and role alias semantics.
    for role in session.exec(
        select(role_column)
        .join(Production, col(Production.id) == ProductionMembership.production_id)
        .where(in_theatres, role_column.is_not(None))
        .distinct()
    ):
        canonical = canonical_role(role)
        if role is not None and canonical in {"director", "actor"}:
            roles[canonical].add(role)
    scraped = (
        select(SourceRecord.id)
        .where(SourceRecord.entity_id == Production.id, SourceRecord.entity_type == EntityType.PRODUCTION)
        .exists()
    )

    def has_role(role: str) -> Exists:
        return (
            select(ProductionMembership.id)
            .where(ProductionMembership.production_id == Production.id, role_column.in_(roles[role]))
            .exists()
        )

    trim = func.btrim if session.get_bind().dialect.name == "postgresql" else func.trim
    filled = {
        "description": func.length(trim(Production.description, _WHITESPACE)) > 0,
        "premiere": col(Production.premiere).is_not(None),
        "director": has_role("director"),
        "cast": has_role("actor"),
        "text": col(Production.text_id).is_not(None),
    }
    for org_id, total, approved, *fields in session.exec(
        select(
            col(Production.organization_id),
            func.count(),
            func.count().filter(scraped),
            *(func.count().filter(condition) for condition in filled.values()),
        )
        .where(in_theatres)
        .group_by(col(Production.organization_id))
    ):
        if org_id is not None:
            stats[org_id].update(productions=total, scraped=approved, fields=dict(zip(filled, fields, strict=True)))
    for org_id, count in _own_website_counts(session, own_hosts).items():
        stats[org_id]["own_website"] = count
    return stats


def _own_website_counts(session: Session, own_hosts: dict[uuid.UUID, set[str]]) -> dict[uuid.UUID, int]:
    # Keep urlsplit semantics; only transfer URL columns for theatres with a host to match.
    org_ids = [org_id for org_id, hosts in own_hosts.items() if hosts]
    if not org_ids:
        return {}
    in_theatres = col(Production.organization_id).in_(org_ids)
    urls = union_all(
        select(Production.organization_id, Production.id, Production.url).where(
            in_theatres, col(Production.url).is_not(None)
        ),
        *(
            select(Production.organization_id, Production.id, model.external_url)
            .join(Production, col(Production.id) == model.entity_id)
            .where(in_theatres, model.entity_type == EntityType.PRODUCTION, col(model.external_url).is_not(None))
            for model in (SourceRecord, EntitySource)
        ),
    )
    matching: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for org_id, production_id, url in session.execute(urls):
        if url_host(url) in own_hosts[org_id]:
            matching[org_id].add(production_id)
    return {org_id: len(ids) for org_id, ids in matching.items()}


def _own_hosts(
    organizations: dict[uuid.UUID, Organization], websites: Iterable[tuple[uuid.UUID, str | None]]
) -> dict[uuid.UUID, set[str]]:
    """The theatre's website from its URL fields; otherwise the sites its uploads were scraped from."""
    hosts: dict[uuid.UUID, set[str]] = {}
    for org_id, organization in organizations.items():
        hosts[org_id] = {host for host in (url_host(organization.url), url_host(organization.url_plays)) if host}
    for org_id, website in websites:
        if not (organizations[org_id].url or organizations[org_id].url_plays) and website:
            hosts[org_id].add(website)
    return {
        org_id: {host for host in values if host != "theapolis.de" and not host.endswith(".theapolis.de")}
        for org_id, values in hosts.items()
    }


def _open_conflicts(session: Session, organization_ids: Collection[uuid.UUID]) -> dict[str, int]:
    org_id = col(ReconciliationCase.context)["organization_id"].as_string()
    return dict(
        session.exec(
            select(org_id.label("conflict_org_id"), func.count())
            .where(
                ReconciliationCase.operation == OPERATION,
                ReconciliationCase.status == "pending",
                org_id.in_([str(value) for value in organization_ids]),
            )
            .group_by("conflict_org_id")
        ).all()
    )


def _theatre_rows(
    session: Session, organization_ids: Collection[uuid.UUID], now: dt.datetime, *, include_history: bool = True
) -> list[dict]:
    organizations = {
        org.id: org for org in session.exec(select(Organization).where(col(Organization.id).in_(organization_ids)))
    }
    if not organizations:
        return []
    in_theatres = col(ImportRun.organization_id).in_(organizations)
    if include_history:
        runs = session.exec(
            select(ImportRun, Source.name)
            .outerjoin(Source, col(Source.id) == ImportRun.source_id)
            .where(in_theatres)
            .order_by(col(ImportRun.imported_at).desc(), col(ImportRun.id).desc())
        ).all()
        websites = ((run.organization_id, run.website) for run, _ in runs)
    else:
        ranked = (
            select(
                ImportRun,
                func.row_number()
                .over(
                    partition_by=col(ImportRun.organization_id),
                    order_by=(
                        func.coalesce(ImportRun.captured_at, ImportRun.imported_at).desc(),
                        col(ImportRun.imported_at).desc(),
                        col(ImportRun.id).desc(),
                    ),
                )
                .label("rank"),
            )
            .where(in_theatres)
            .subquery()
        )
        latest = aliased(ImportRun, ranked)
        runs = session.exec(
            select(latest, Source.name).outerjoin(Source, col(Source.id) == latest.source_id).where(ranked.c.rank == 1)
        ).all()
        without_urls = [org.id for org in organizations.values() if not (org.url or org.url_plays)]
        websites = (
            session.exec(
                select(ImportRun.organization_id, ImportRun.website)
                .where(col(ImportRun.organization_id).in_(without_urls), col(ImportRun.website).is_not(None))
                .distinct()
            )
            if without_urls
            else ()
        )
    own_hosts = _own_hosts(organizations, websites)
    stats = _production_stats(session, list(organizations), own_hosts)
    conflicts = _open_conflicts(session, organizations)
    by_theatre: dict[uuid.UUID, list[dict]] = defaultdict(list)
    for run, source in runs:
        by_theatre[run.organization_id].append(_run_row(run, source, now))
    return [
        {
            "organization_id": str(org_id),
            "theatre": organization.name,
            "own_hosts": sorted(own_hosts[org_id]),
            **({"runs": by_theatre[org_id]} if include_history else {}),
            "latest": max(by_theatre[org_id], key=itemgetter("data_date"), default=None),
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
    Full upload history is only available from theatre_status.
    """
    now = now or dt.datetime.now(dt.UTC)
    with get_session() as session:
        theatres = session.exec(
            select(func.count(func.distinct(Production.organization_id))).where(
                col(Production.organization_id).is_not(None)
            )
        ).one()
        uploaded = set(session.exec(select(ImportRun.organization_id).distinct()).all())
        rows = _theatre_rows(session, uploaded, now, include_history=False) if uploaded else []
    cutoff = now - dt.timedelta(days=recent_days)
    rows.sort(key=lambda row: row["latest"]["data_date"])
    return {
        "recent_days": recent_days,
        "theatres": max(theatres, len(rows)),
        "uploaded": len(rows),
        "recent": sum(row["latest"]["data_date"] >= cutoff for row in rows),
        "rows": rows,
    }
