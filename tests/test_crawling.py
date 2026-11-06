import asyncio
import datetime as dt
import json
import os
import threading
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import procrastinate
import psycopg
import pytest
from aiohttp import web
from psycopg import sql
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.crawling import queue, runner, service
from backend.crawling.config import DEFINITION
from backend.models import (
    CrawlPage,
    CrawlRecord,
    CrawlRun,
    ImportRun,
    Organization,
    Production,
    ReconciliationCase,
    Source,
    SourceCrawl,
)


@pytest.fixture
def db(monkeypatch, tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'crawls.db'}")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    monkeypatch.setenv("THEATERDB_CRAWL_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setattr(queue, "enqueue", lambda session, run: 123)
    monkeypatch.setattr(queue, "cancel", lambda session, job_id: None)
    yield engine
    engine.dispose()


def rows(db: Engine, model: type[SQLModel]) -> list:
    with Session(db) as session:
        return list(session.exec(select(model)).all())


def setup_source(db: Engine) -> tuple[Source, Organization]:
    with Session(db) as session:
        source = Source(name="Test source")
        theatre = Organization(name="Test theatre")
        session.add(source)
        session.add(theatre)
        session.commit()
        session.refresh(source)
        session.refresh(theatre)
        return source, theatre


def theatre_config(base: str, theatre: Organization) -> dict:
    return {
        "kind": "theatre",
        "organization_id": str(theatre.id),
        "config": {
            "source_id": "not-the-source-database-id",
            "organization_name": theatre.name,
            "base_url": base,
            "listing": {"urls": [base + "/list"], "selector": "a", "include": "/p/", "max_pages": 3},
            "production": {"title": {"selector": "h1", "required": True}},
            "credits": [
                {
                    "selector": ".cast",
                    "name": {"selector": ".name"},
                    "role": {"selector": ".role"},
                    "url": {"selector": "a", "attribute": "href", "format": "url"},
                }
            ],
            "person": {
                "name": {"selector": "h1", "required": True},
                "description": {"selector": ".bio"},
            },
            "request_delay": 0,
        },
    }


def record_config(base: str) -> dict:
    return {
        "kind": "records",
        "base_url": base,
        "entity_type": "production",
        "listing": {"urls": [base + "/list"], "selector": "a", "include": "/p/", "max_pages": 3},
        "fields": {
            "name": {"selector": "h1", "required": True},
            "critics": {"selector": ".review", "many": True},
        },
        "request_delay": 0,
    }


@asynccontextmanager
async def site() -> AsyncGenerator[tuple[str, dict]]:
    state = {
        "paths": ["/p/1"],
        "requests": [],
        "revision": {},
        "titles": {},
        "bio": "Original biography",
        "status": {},
        "delay": 0,
    }

    async def page(request: web.Request) -> web.Response:
        path = request.path
        state["requests"].append((path, dict(request.headers)))
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if path in state["status"]:
            status = state["status"][path]
            if isinstance(status, list):
                status = status.pop(0)
                if not state["status"][path]:
                    del state["status"][path]
            headers = {"Retry-After": str(state["retry_after"])} if state.get("retry_after") else {}
            return web.Response(status=status, headers=headers)
        etag = f'"{path}-{state["revision"].get(path, 1)}"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        if path == "/list":
            body = "".join(f'<a href="{url}">Production</a>' for url in state["paths"])
        elif path == "/person/1":
            body = f'<h1>Test Actor</h1><div class="bio">{state["bio"]}</div>'
        else:
            title = state["titles"].get(path, "Test play " + path)
            body = (
                f'<h1>{title}</h1><div class="review">A test review</div>'
                '<div class="cast"><a href="/person/1"><span class="name">Test Actor</span></a>'
                '<span class="role">Mit</span></div>'
            )
        return web.Response(
            text=body,
            content_type="text/html",
            headers={"ETag": etag, "Last-Modified": "Mon, 02 Nov 2026 10:00:00 GMT"},
        )

    app = web.Application()
    app.router.add_get("/{path:.*}", page)
    server = web.AppRunner(app)
    await server.setup()
    tcp = web.TCPSite(server, "127.0.0.1", 0)
    await tcp.start()
    address = server.addresses[0]
    try:
        yield f"http://127.0.0.1:{address[1]}", state
    finally:
        await server.cleanup()


def test_source_owns_config_runs_and_cache(db: Engine) -> None:
    source, _ = setup_source(db)
    definition = record_config("https://example.org")
    service.save_config(source.id, definition)
    run_id = service.start(source.id, actor_id="test")
    with Session(db) as session:
        stored = session.get(Source, source.id)
        assert stored is not None
        assert stored.crawl_config is not None
        assert stored.crawl_config.source_id == source.id
        assert [run.id for run in stored.crawl_runs] == [run_id]
        assert stored.crawl_records == []
        assert stored.crawl_pages == []
    with pytest.raises(ValueError, match="active crawl"):
        service.start(source.id, actor_id="other-browser")
    definition["request_delay"] = 2
    service.save_config(source.id, definition)
    assert rows(db, CrawlRun)[0].definition["request_delay"] == 0
    service.stop(run_id)
    assert rows(db, CrawlRun)[0].status == "cancelled"
    asyncio.run(runner.execute(run_id))
    assert rows(db, CrawlPage) == []


def test_enqueue_failure_rolls_back_run(db: Engine, monkeypatch) -> None:
    source, _ = setup_source(db)
    service.save_config(source.id, record_config("https://example.org"))

    def fail(session, run):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(queue, "enqueue", fail)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        service.start(source.id, actor_id="test")
    assert rows(db, CrawlRun) == []


def test_configuration_rejects_bad_fields_origins_and_missing_theatre(db: Engine) -> None:
    source, theatre = setup_source(db)
    config = record_config("https://example.org")
    config["fields"]["organization_id"] = {"selector": "h2"}
    with pytest.raises(ValueError, match="dedicated adapter"):
        service.save_config(source.id, config)
    config = record_config("https://example.org")
    config["listing"]["urls"] = ["https://other.example.org"]
    with pytest.raises(ValueError, match="origin"):
        DEFINITION.validate_python(config)
    definition = theatre_config("https://example.org", theatre)
    definition["organization_id"] = str(uuid.uuid4())
    with pytest.raises(ValueError, match="Theatre not found"):
        service.save_config(source.id, definition)
    assert rows(db, SourceCrawl) == []


def test_incremental_theatre_fetch_import_and_biography_dependency(db: Engine) -> None:
    source, theatre = setup_source(db)

    async def scenario() -> None:
        async with site() as (base, state):
            service.save_config(source.id, theatre_config(base, theatre))
            first = service.start(source.id, actor_id="test")
            await runner.execute(first)
            run = service.runs(source.id)[0]
            assert run.status == "succeeded"
            assert run.stats["changed"] == 1
            assert rows(db, ImportRun)[0].scope == "delta"
            assert rows(db, ImportRun)[0].source_id == source.id
            assert len(rows(db, Production)) == 1
            assert run.artifact_path is not None
            artifact = json.loads(await asyncio.to_thread(Path(run.artifact_path).read_text, encoding="utf-8"))
            assert artifact[0]["memberships"][0]["description"] == "Original biography"
            second = service.start(source.id, actor_id="test")
            await runner.execute(second)
            run = service.runs(source.id)[0]
            assert run.stats["not_modified"] == 3
            assert run.stats["changed"] == 0
            assert len(rows(db, ImportRun)) == 1
            assert state["requests"][-1][1]["If-Modified-Since"] == "Mon, 02 Nov 2026 10:00:00 GMT"
            state["revision"]["/person/1"] = 2
            state["bio"] = "Updated biography"
            third = service.start(source.id, actor_id="test")
            await runner.execute(third)
            assert service.runs(source.id)[0].stats["changed"] == 1
            assert len(rows(db, ImportRun)) == 2
            assert len(rows(db, CrawlRecord)) == 1
            state["requests"].clear()
            fourth = service.start(source.id, actor_id="test", force_fetch=True)
            await runner.execute(fourth)
            assert service.runs(source.id)[0].stats["not_modified"] == 0
            assert service.runs(source.id)[0].stats["changed"] == 0
            assert all("If-None-Match" not in headers for _, headers in state["requests"])

    asyncio.run(scenario())


def test_generic_records_queue_review_not_theatre_import(db: Engine) -> None:
    source, _ = setup_source(db)

    async def scenario() -> None:
        async with site() as (base, state):
            service.save_config(source.id, record_config(base))
            await runner.execute(service.start(source.id, actor_id="test"))
            assert rows(db, ImportRun) == []
            assert rows(db, Production) == []
            cases = rows(db, ReconciliationCase)
            assert len(cases) == 1
            assert cases[0].source_id == source.id
            assert cases[0].incoming["critics"] == ["A test review"]
            state["revision"]["/p/1"] = 2  # HTML/ETag changes, but extracted fields do not.
            await runner.execute(service.start(source.id, actor_id="test"))
            assert service.runs(source.id)[0].stats["changed"] == 0
            assert len(rows(db, ReconciliationCase)) == 1

    asyncio.run(scenario())


def test_failed_or_limited_crawl_never_claims_full_coverage(db: Engine) -> None:
    source, _ = setup_source(db)

    async def scenario() -> None:
        async with site() as (base, state):
            state["paths"] = ["/p/1", "/p/2"]
            state["status"]["/p/2"] = 404
            service.save_config(source.id, record_config(base))
            failed = service.start(source.id, actor_id="test")
            with pytest.raises(aiohttp.ClientResponseError):
                await runner.execute(failed)
            assert service.runs(source.id)[0].status == "failed"
            assert rows(db, CrawlRecord) == []
            assert rows(db, ReconciliationCase) == []
            assert len(rows(db, CrawlPage)) == 2  # Transport cache is not an ingestion cursor.
            limited = service.start(source.id, actor_id="test", limit=1)
            await runner.execute(limited)
            assert service.runs(source.id)[0].limit == 1
            assert len(rows(db, CrawlRecord)) == 1
            del state["status"]["/p/2"]
            await runner.execute(service.start(source.id, actor_id="test"))
            assert service.runs(source.id)[0].limit is None
            assert service.runs(source.id)[0].stats["records"] == 2
            assert service.runs(source.id)[0].stats["changed"] == 1

    asyncio.run(scenario())


def test_import_failure_rolls_back_domain_and_fingerprint(db: Engine, monkeypatch) -> None:
    source, theatre = setup_source(db)
    original = runner.ingest_records

    def fail_after_import[**P, R](function: Callable[P, R]) -> Callable[P, R]:
        def fail(*args: P.args, **kwargs: P.kwargs) -> R:
            function(*args, **kwargs)
            raise ValueError("import checkpoint failed")

        return fail

    monkeypatch.setattr(runner, "ingest_records", fail_after_import(original))

    async def scenario() -> None:
        async with site() as (base, _):
            service.save_config(source.id, theatre_config(base, theatre))
            with pytest.raises(ValueError, match="checkpoint failed"):
                await runner.execute(service.start(source.id, actor_id="test"))
            assert rows(db, Production) == []
            assert rows(db, ImportRun) == []
            assert rows(db, CrawlRecord) == []
            monkeypatch.setattr(runner, "ingest_records", original)
            await runner.execute(service.start(source.id, actor_id="test"))
            assert len(rows(db, Production)) == 1
            assert service.runs(source.id)[0].stats["changed"] == 1

    asyncio.run(scenario())


def test_running_stop_and_stale_worker_are_fenced(db: Engine) -> None:
    source, _ = setup_source(db)
    service.save_config(source.id, record_config("https://example.org"))
    run_id = service.start(source.id, actor_id="test")
    with Session(db) as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        run.status = "running"
        run.heartbeat_at = service.utcnow()
        session.add(run)
        session.commit()
    with pytest.raises(ValueError, match="not stale"):
        service.fail_stale(run_id)
    service.stop(run_id)
    assert service.runs(source.id)[0].status == "cancelling"
    with pytest.raises(runner.CrawlStoppedError):
        runner.checkpoint(run_id)
    with Session(db) as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        run.heartbeat_at = service.utcnow() - dt.timedelta(minutes=4)
        session.add(run)
        session.commit()
    service.fail_stale(run_id)
    assert service.runs(source.id)[0].status == "failed"
    with pytest.raises(runner.CrawlStoppedError):
        runner.checkpoint(run_id)
    service.start(source.id, actor_id="new-worker")


def test_async_task_cancellation_does_not_import(db: Engine) -> None:
    source, _ = setup_source(db)

    async def scenario() -> None:
        async with site() as (base, state):
            state["delay"] = 0.1
            service.save_config(source.id, record_config(base))
            run_id = service.start(source.id, actor_id="test")
            task = asyncio.create_task(runner.execute(run_id))
            async with asyncio.timeout(3):
                while not state["requests"]:
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.runs(source.id)[0].status == "cancelled"
            assert rows(db, CrawlRecord) == []
            assert rows(db, ReconciliationCase) == []

    asyncio.run(scenario())


def test_fingerprint_ignores_order_but_not_changed_fields() -> None:
    assert runner.payload_hash({"names": ["a", "b"], "unused": None}) == runner.payload_hash({"names": ["b", "a"]})
    assert runner.payload_hash({"name": "a"}) != runner.payload_hash({"name": "b"})


def test_parser_change_reprocesses_304_without_reimporting_everything(db: Engine) -> None:
    source, _ = setup_source(db)

    async def scenario() -> None:
        async with site() as (base, _):
            definition = record_config(base)
            service.save_config(source.id, definition)
            await runner.execute(service.start(source.id, actor_id="test"))
            definition["fields"]["critics"]["selector"] = "h1"
            service.save_config(source.id, definition)
            await runner.execute(service.start(source.id, actor_id="test"))
            run = service.runs(source.id)[0]
            assert run.stats["not_modified"] == 2
            assert run.stats["changed"] == 1
            assert len(rows(db, ReconciliationCase)) == 2

    asyncio.run(scenario())


def test_transient_http_retry_and_long_retry_after(db: Engine, monkeypatch) -> None:
    source, _ = setup_source(db)
    original_delay = runner._retry_delay
    monkeypatch.setattr(runner, "_retry_delay", lambda error, attempts: 0.001)

    async def scenario() -> None:
        async with site() as (base, state):
            state["status"]["/list"] = [429, 503]
            service.save_config(source.id, record_config(base))
            await runner.execute(service.start(source.id, actor_id="test"))
            run = service.runs(source.id)[0]
            assert run.status == "succeeded"
            assert run.stats["requests"] == 4
            assert run.stats["changed"] == 1
            monkeypatch.setattr(runner, "_retry_delay", original_delay)
            state["status"]["/list"] = 429
            state["retry_after"] = "3600"
            state["requests"].clear()
            with pytest.raises(ValueError, match="retry this crawl later"):
                await runner.execute(service.start(source.id, actor_id="test"))
            assert len(state["requests"]) == 1
            assert service.runs(source.id)[0].status == "failed"

    asyncio.run(scenario())


def test_missing_records_never_delete_previous_results(db: Engine) -> None:
    source, _ = setup_source(db)

    async def scenario() -> None:
        async with site() as (base, state):
            service.save_config(source.id, record_config(base))
            await runner.execute(service.start(source.id, actor_id="test"))
            state["paths"] = []
            state["revision"]["/list"] = 2
            with pytest.raises(ValueError, match="No record links"):
                await runner.execute(service.start(source.id, actor_id="test"))
            assert len(rows(db, ReconciliationCase)) == 1
            assert len(rows(db, CrawlRecord)) == 1

    asyncio.run(scenario())


def test_streamlit_source_controls(db: Engine) -> None:
    source, _ = setup_source(db)
    service.save_config(source.id, record_config("https://example.org"))
    path = Path(__file__).resolve().parents[1] / "frontend/management/sources.py"
    app = AppTest.from_file(str(path)).run()
    assert not app.exception
    start = next(button for button in app.button if button.label == "Crawl starten")
    start.click().run()
    assert not app.exception
    assert service.runs(source.id)[0].status == "queued"
    stop = next(button for button in app.button if button.label == "Crawl stoppen")
    stop.click().run()
    assert not app.exception
    assert service.runs(source.id)[0].status == "cancelled"


@pytest.fixture
def postgres(monkeypatch, tmp_path: Path) -> Iterator[tuple[Engine, procrastinate.App]]:
    if os.environ.get("THEATERDB_TEST_POSTGRES") != "1":
        pytest.skip("Set THEATERDB_TEST_POSTGRES=1 for disposable PostgreSQL queue integration")
    name = "theaterdb_crawl_test_" + uuid.uuid4().hex
    url = user_management.DATABASE_URL.set(drivername="postgresql", database="postgres")
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        database_url = url.set(database=name)
        engine = create_engine(database_url.set(drivername="postgresql+psycopg"))
        app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(
                conninfo=database_url.render_as_string(hide_password=False),
            )
        )
        try:
            SQLModel.metadata.create_all(engine)
            with app.open():
                app.schema_manager.apply_schema()
            monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
            client = procrastinate.App(
                connector=procrastinate.SyncPsycopgConnector(
                    conninfo=database_url.render_as_string(hide_password=False)
                )
            )
            monkeypatch.setattr(queue, "app", lambda: client)
            monkeypatch.setenv("THEATERDB_CRAWL_DIR", str(tmp_path / "artifacts"))
            yield engine, app
        finally:
            engine.dispose()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_postgres_queue_transaction_and_real_worker(postgres, monkeypatch) -> None:
    db, app = postgres
    source, _ = setup_source(db)

    @app.task(name=queue.TASK_NAME)
    async def crawl(run_id: str) -> None:
        await runner.execute(uuid.UUID(run_id))

    async def scenario() -> None:
        async with site() as (base, state):
            service.save_config(source.id, record_config(base))
            run_id = service.start(source.id, actor_id="integration")
            with pytest.raises(ValueError, match="active crawl"):
                service.start(source.id, actor_id="duplicate")
            async with app.open_async():
                await app.run_worker_async(wait=False, install_signal_handlers=False)
            assert service.runs(source.id)[0].status == "succeeded"
            assert len(rows(db, ReconciliationCase)) == 1
            stopped = service.start(source.id, actor_id="integration")
            service.stop(stopped)
            assert service.runs(source.id)[0].status == "cancelled"
            assert service.runs(source.id)[1].id == run_id
            original = queue.enqueue

            def rollback_after_enqueue(session: Session, run: CrawlRun) -> int:
                original(session, run)
                raise ValueError("rollback queue transaction")

            with monkeypatch.context() as patch:
                patch.setattr(queue, "enqueue", rollback_after_enqueue)
                with pytest.raises(ValueError, match="rollback queue transaction"):
                    service.start(source.id, actor_id="rollback")
            assert len(service.runs(source.id)) == 2
            with db.connect() as connection:
                count = connection.exec_driver_sql("SELECT count(*) FROM procrastinate_jobs").scalar_one()
                assert count == 2
            state["delay"] = 0.3
            state["requests"].clear()
            running = service.start(source.id, actor_id="stop-running")
            async with app.open_async():
                worker = asyncio.create_task(
                    app.run_worker_async(
                        wait=False,
                        install_signal_handlers=False,
                        abort_job_polling_interval=0.01,
                    )
                )
                async with asyncio.timeout(10):
                    while not state["requests"]:
                        await asyncio.sleep(0.01)
                    service.stop(running)
                    await worker
            assert service.runs(source.id)[0].status == "cancelled"
            assert len(rows(db, ReconciliationCase)) == 1

    asyncio.run(scenario())


def test_postgres_concurrent_start_has_exactly_one_job(postgres) -> None:
    db, _ = postgres
    source, _ = setup_source(db)
    service.save_config(source.id, record_config("https://example.org"))
    barrier = threading.Barrier(2)

    def launch(actor: str) -> uuid.UUID | str:
        barrier.wait(timeout=5)
        try:
            return service.start(source.id, actor_id=actor)
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(launch, ["browser-one", "browser-two"]))
    assert sum(isinstance(result, uuid.UUID) for result in results) == 1
    assert "Source already has an active crawl" in results
    assert len(service.runs(source.id)) == 1
    with db.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM procrastinate_jobs").scalar_one() == 1
