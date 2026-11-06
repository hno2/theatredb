"""Transactional crawl controls shared by Streamlit and the worker."""

import datetime as dt
import uuid

from sqlmodel import Session, col, select

from backend.crawling import queue
from backend.crawling.config import DEFINITION, TheatreCrawl
from backend.models import CrawlRun, Organization, Source, SourceCrawl
from backend.user_management import get_session

ACTIVE = ("queued", "running", "cancelling")
STALE_AFTER = dt.timedelta(minutes=3)


def utcnow() -> dt.datetime:
    """Return a timezone-aware execution timestamp."""
    return dt.datetime.now(dt.UTC)


def _source(session: Session, source_id: uuid.UUID) -> Source:
    source = session.get(Source, source_id, with_for_update=True)
    if source is None:
        raise ValueError("Source not found")
    return source


def save_config(source_id: uuid.UUID, definition: dict, *, enabled: bool = True) -> None:
    """Validate and save one source definition without changing any queued snapshot."""
    checked = DEFINITION.validate_python(definition)
    with get_session() as session:
        _source(session, source_id)
        if isinstance(checked, TheatreCrawl) and session.get(Organization, checked.organization_id) is None:
            raise ValueError("Theatre not found")
        config = session.get(SourceCrawl, source_id) or SourceCrawl(source_id=source_id, definition={})
        config.definition = checked.model_dump(mode="json")
        config.enabled = enabled
        config.updated_at = utcnow()
        session.add(config)
        session.commit()


def start(
    source_id: uuid.UUID,
    *,
    actor_id: str | None,
    force_fetch: bool = False,
    limit: int | None = None,
    enrich_people: bool = True,
) -> uuid.UUID:
    """Queue at most one active run per source, including across browser sessions."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be positive, or None")
    with get_session() as session:
        _source(session, source_id)
        config = session.get(SourceCrawl, source_id)
        if config is None or not config.enabled:
            raise ValueError("Source has no enabled crawler configuration")
        DEFINITION.validate_python(config.definition)
        active = session.exec(
            select(CrawlRun).where(CrawlRun.source_id == source_id, col(CrawlRun.status).in_(ACTIVE))
        ).first()
        if active:
            raise ValueError("Source already has an active crawl")
        run = CrawlRun(
            source_id=source_id,
            definition=config.definition,
            actor_id=actor_id,
            force_fetch=force_fetch,
            limit=limit,
            enrich_people=enrich_people,
        )
        session.add(run)
        session.flush()
        run.job_id = queue.enqueue(session, run)
        session.commit()
        return run.id


def stop(run_id: uuid.UUID) -> None:
    """Persist stop intent and notify the queue in the same transaction."""
    with get_session() as session:
        run = session.get(CrawlRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("Crawl run not found")
        if run.status not in ACTIVE:
            raise ValueError("Crawl run is no longer active")
        if run.job_id is None:
            raise ValueError("Crawl run has no queue job")
        queue.cancel(session, run.job_id)
        run.cancel_requested_at = utcnow()
        if run.status == "queued":
            run.status = "cancelled"
            run.phase = "cancelled"
            run.finished_at = utcnow()
        else:
            run.status = "cancelling"
        session.add(run)
        session.commit()


def is_stale(run: CrawlRun) -> bool:
    """Distinguish a lost worker from a long but healthy crawl."""
    heartbeat = run.heartbeat_at
    if run.status not in ("running", "cancelling") or heartbeat is None:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=dt.UTC)
    return utcnow() - heartbeat > STALE_AFTER


def fail_stale(run_id: uuid.UUID) -> None:
    """Fence a lost worker before allowing another run; never publish partial data."""
    with get_session() as session:
        run = session.get(CrawlRun, run_id, with_for_update=True)
        if run is None or not is_stale(run):
            raise ValueError("Crawl is not stale; stop a healthy worker normally")
        if run.job_id is not None:
            queue.cancel(session, run.job_id)
        run.status = "failed"
        run.phase = "failed"
        run.error = "Worker heartbeat expired; run ended explicitly. Partial results were not imported."
        run.finished_at = utcnow()
        session.add(run)
        session.commit()


def runs(source_id: uuid.UUID, *, limit: int = 20) -> list[CrawlRun]:
    """List latest execution attempts, not just the latest successful import."""
    with get_session() as session:
        return list(
            session.exec(
                select(CrawlRun).where(CrawlRun.source_id == source_id).order_by(col(CrawlRun.id).desc()).limit(limit)
            ).all()
        )


def configuration(source_id: uuid.UUID) -> SourceCrawl | None:
    """Return the persisted source definition for the configuration editor."""
    with get_session() as session:
        return session.get(SourceCrawl, source_id)
