import asyncio
import copy
import datetime as dt
import json
import re
import threading
import uuid
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from multidict import CIMultiDict, CIMultiDictProxy
from sqlmodel import Session, SQLModel, col, create_engine, select
from yarl import URL

from backend import user_management
from backend.import_status import theatre_status
from backend.ingestion import theapolis
from backend.ingestion.staging import staging_schema, validate_records
from backend.ingestion.theapolis_import import migrate_pending_cases
from backend.models import (
    EntitySource,
    EntityType,
    Organization,
    Person,
    Production,
    ReconciliationCase,
    Source,
    SourceRecord,
    Text,
    TheapolisProfileCache,
)
from backend.reconciliation import (
    approve_case,
    find_candidates,
    merge_entities,
    preview_case,
    preview_entity_merge,
    propose_record,
)
from backend.scd2 import SCD2History
from frontend.views.organization import filter_productions, premiere_label, production_dates


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'theapolis.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def item():
    return {
        "id": 123,
        "name": "Hamlet",
        "premierYear": 2026,
        "premierDate": "2026-10-03",
        "organization": {
            "id": 42,
            "name": "Theater",
            "slug": "theater",
            "location": {"city": "Berlin", "country": "Deutschland", "geolocation": "52.5,13.4"},
        },
        "directors": [{"id": 9, "name": "Alex"}],
        "creators": [{"id": 9, "name": "Alex"}, {"id": 10, "name": "Other Alex"}],
    }


@pytest.mark.parametrize(
    ("raw", "date", "precision"),
    [
        ({"premierDate": "2026-10-03"}, "2026-10-03T00:00:00", "day"),
        ({"premierDate": "3.10.2026"}, "2026-10-03T00:00:00", "day"),
        ({"premierDate": "2026-10-03T19"}, "2026-10-03T19:00:00", "minute"),
        ({"premierDate": "2026-10-03T19:00:00+02:00"}, "2026-10-03T19:00:00", "minute"),
        ({"premierDate": "2026"}, "2026-01-01T00:00:00", "year"),
        ({"premierDate": "2026-01"}, "2026-01-01T00:00:00", "month"),
        ({"premierDate": "2026-12", "premierYear": "2026"}, "2026-12-01T00:00:00", "month"),
        ({"premierYear": 2026}, "2026-01-01T00:00:00", "year"),
        ({"year": "2026"}, "2026-01-01T00:00:00", "year"),
        ({}, None, None),
    ],
)
def test_premiere_precision(raw, date, precision):
    fields = theapolis.premiere_fields(raw)
    assert fields == {"premiere": date, "premiere_precision": precision}
    Production.model_validate({"name": "Test", **fields})


@pytest.mark.parametrize(
    "raw",
    [
        {"premierDate": "2026-02-31"},
        {"premierDate": "2026-00"},
        {"premierDate": "2026-13"},
        {"premierDate": "0000-01"},
        {"premierDate": "2026-01", "premierYear": 2025},
        {"premierDate": "2026-01", "premierYear": False},
        {"premierYear": "unknown"},
        {"premierYear": True},
        {"premierYear": False},
        {"premierYear": 0},
        {"premierDate": "2026-01-01", "premierYear": 2025},
        {"premierDate": "2026", "premierYear": 2025},
    ],
)
def test_invalid_dates_are_explicit(raw):
    with pytest.raises(ValueError):
        theapolis.premiere_fields(raw)


def test_export_paginates_parallel_and_preserves_dates(monkeypatch, tmp_path, item):
    active, peak = 0, 0
    requested = []

    def reject_database():
        pytest.fail("JSON export must not initialize domain/queue tables")

    monkeypatch.setattr(theapolis, "init_db", reject_database)

    async def fetch(session, url):
        nonlocal active, peak
        if "/api/de/profiles?" in url:
            return b'{"success": true, "data": {"items": []}}'
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        requested.append(page)
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1
        record = {**item, "id": page}
        if page == 2:
            record["premierDate"] = "2026-01"
        return json.dumps(
            {"success": True, "data": {"totalCount": 1500, "numItemsPerPage": 500, "items": [record]}}
        ).encode()

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    path = tmp_path / "export.json"
    asyncio.run(theapolis.download(path, concurrency=2))
    data = json.loads(path.read_text())
    source = json.loads(path.with_suffix(".source.json").read_text())
    assert requested == [1, 2, 3]
    assert peak == 2
    assert isinstance(data, list)
    assert theapolis.without_null_fields(validate_records(data)) == data
    Draft202012Validator(staging_schema(), format_checker=FormatChecker()).validate(data)
    assert source["partial"] is False
    assert dt.datetime.fromisoformat(source["captured_at"]).tzinfo
    assert data[0]["title"] == "Hamlet"
    assert "id" not in data[0]
    assert "organization" not in data[0]
    assert data[0]["memberships"][0]["role"] == "director"
    assert data[1]["premiere"] == "2026-01-01T00:00:00"
    assert data[1]["premiere_precision"] == "month"
    assert source["items"][1]["premierDate"] == "2026-01"
    with user_management.get_session() as session:
        assert not session.exec(select(Person)).all()
        assert not session.exec(select(Source)).all()
        assert not session.exec(select(ReconciliationCase)).all()
    requested.clear()
    asyncio.run(theapolis.download(path, max_pages=1))
    assert requested == [1]
    assert json.loads(path.with_suffix(".source.json").read_text())["partial"] is True


def test_production_tags_and_profile_birthday_preserve_source_identity(db, tmp_path, item):
    profile = {
        "slug": "alex-example",
        "url": f"{theapolis.SITE}/de/profil/alex-example",
        "birthDate": "1966-05-19T00:00:00+02:00",
        "skills": [{"title": "Fechten"}],
    }
    item["genre"] = {"name": "Schauspiel"}
    item["tags"] = ["UA", {"name": "Musical"}]
    item["independent"] = True
    item["directors"][0]["profile"] = profile
    path = tmp_path / "profile-export.json"
    path.write_text(json.dumps({"items": [item]}))

    staged = theapolis.staged_records([item])[0]
    assert staged["tags"] == ["Schauspiel", "UA", "Musical", "Freie Darstellende Künste"]
    assert "tags" not in staged["memberships"][0]
    assert staged["memberships"][0]["external_id"] == "9"
    assert staged["memberships"][0]["born"] == "1966-05-19T00:00:00"
    assert staged["memberships"][0]["born_precision"] == "day"
    result = theapolis.import_all(path)
    with Session(db) as session:
        record = session.exec(select(SourceRecord).where(SourceRecord.external_id == "directors:9")).one()
        person = session.get(Person, record.entity_id)
        assert person is not None
        assert record.external_url == profile["url"]
        assert person.born == dt.datetime(1966, 5, 19)
        assert person.born_precision == "day"
        assert person.type == ["director"]
        assert "tags" not in record.payload["incoming"]
        assert session.exec(select(Production)).one().tags == [
            "Schauspiel",
            "UA",
            "Musical",
            "Freie Darstellende Künste",
        ]
        assert result["person"]


@pytest.mark.parametrize("birthday", [" ", "unknown", "1966-02-31", "1966-00-00"])
def test_invalid_profile_birthday_is_logged_and_omitted(item, caplog, birthday):
    profile = {"url": f"{theapolis.SITE}/de/profil/alex", "birthDate": birthday}
    item["directors"][0]["profile"] = profile
    member = theapolis.staged_records([item])[0]["memberships"][0]
    assert member["born"] is None
    assert member["born_precision"] is None
    assert member["url"] == profile["url"]
    assert item["directors"][0]["profile"]["birthDate"] == birthday
    assert "Ignoring invalid Theapolis profile birthday" in caplog.text
    assert profile["url"] in caplog.text
    assert theapolis._add_people(item, {}) == [
        ("directors:9", "director"),
        ("creators:9", "author"),
        ("creators:10", "author"),
    ]


@pytest.mark.parametrize(
    ("birthday", "born", "precision"),
    [
        ("1966", "1966-01-01T00:00:00", "year"),
        ("1966-05", "1966-05-01T00:00:00", "month"),
        ("1966-05-19", "1966-05-19T00:00:00", "day"),
        ("1966-05-19T00:00:00+02:00", "1966-05-19T00:00:00", "day"),
    ],
)
def test_valid_profile_birthday_keeps_precision(birthday, born, precision):
    assert theapolis._profile_birthday({"birthDate": birthday}) == {"born": born, "born_precision": precision}


def test_profile_lookup_uses_unique_slug_in_person_url(monkeypatch):
    calls = []

    def fetch(session, url):
        calls.append(url)
        if "/api/de/profiles?" in url:
            return json.dumps({"success": True, "data": {"items": [{"name": "Alex", "slug": "alex-profile"}]}}).encode()
        assert url.endswith("/de/people/api/show/alex-profile")
        return json.dumps(
            {
                "status": "OK",
                "code": 200,
                "error": False,
                "data": {
                    "profile": {
                        "slug": "alex-profile",
                        "personalData": {"birthDate": "1966-05-19"},
                    }
                },
            }
        ).encode()

    monkeypatch.setattr(theapolis, "_fetch", AsyncMock(side_effect=fetch))

    async def run():
        async with theapolis._session(1) as session:
            return await theapolis._profile_for_name(session, "Alex")

    profile = asyncio.run(run())
    assert profile == {
        "slug": "alex-profile",
        "url": f"{theapolis.SITE}/de/profil/alex-profile",
        "birthDate": "1966-05-19",
    }
    assert len(calls) == 2


def test_missing_profile_details_do_not_abort_export(monkeypatch, caplog):
    def fetch(session, url):
        if "/api/de/profiles?" in url:
            return json.dumps(
                {"success": True, "data": {"items": [{"name": "Bertolt Brecht", "slug": "bertolt-brecht"}]}}
            ).encode()
        assert url.endswith("/de/people/api/show/bertolt-brecht")
        info = aiohttp.RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()))
        raise aiohttp.ClientResponseError(info, (), status=404)

    monkeypatch.setattr(theapolis, "_fetch", AsyncMock(side_effect=fetch))

    async def run():
        async with theapolis._session(1) as session:
            return await theapolis._profile_for_name(session, "Bertolt Brecht")

    assert asyncio.run(run()) is None
    assert not caplog.records


@pytest.mark.parametrize("with_people", [True, False])
def test_profile_summary_and_missing_authors_keep_source_id(db, monkeypatch, tmp_path, item, caplog, with_people):
    def lookup(session, name):
        if name == "Other Alex":
            return {"slug": "other-alex", "url": f"{theapolis.SITE}/de/profil/other-alex"}
        return None

    monkeypatch.setattr(theapolis, "_profile_for_name", AsyncMock(side_effect=lookup))
    if not with_people:
        item["directors"] = []
        item["creators"] = []

    async def run():
        async with theapolis._session(1) as session:
            await theapolis._enrich_people(session, [item, item], 2)

    with caplog.at_level("INFO", logger=theapolis.__name__):
        asyncio.run(run())
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Theapolis profiles: 2/3 people missing (66.7%); all production credits retained"
        if with_people
        else "Theapolis profiles: 0/0 people missing (0.0%); all production credits retained"
    ]
    path = tmp_path / "people.json"
    theapolis._write_json(path, theapolis.staged_records([item]))
    theapolis._write_json(path.with_suffix(".source.json"), {"items": [item]})
    result = theapolis.import_all(path)
    if not with_people:
        assert not result["person"]
        return
    memberships = json.loads(path.read_text())[0]["memberships"]
    author = next(member for member in memberships if member["external_id"] == "9" and member["role"] == "author")
    assert author["name"] == "Alex"
    assert "url" not in author and "born" not in author
    with Session(db) as session:
        assert not session.exec(select(ReconciliationCase)).all()
        records = session.exec(select(SourceRecord).where(SourceRecord.entity_type == "person")).all()
        assert {record.external_id for record in records} == {"directors:9", "creators:9", "creators:10"}
    with Session(db) as session:
        sources = session.exec(select(EntitySource).where(EntitySource.entity_type == "person")).all()
        assert {source.external_id for source in sources} == {"directors:9", "creators:9", "creators:10"}


@pytest.mark.parametrize("legacy", [False, True])
def test_staged_person_source_ids_are_checked_and_legacy_ids_remain_optional(db, tmp_path, item, legacy):
    records = theapolis.staged_records([item])
    if legacy:
        for member in records[0]["memberships"]:
            del member["external_id"]
    else:
        records[0]["memberships"][0]["external_id"] = "wrong-id"
    path = tmp_path / "source-ids.json"
    theapolis._write_json(path, records)
    theapolis._write_json(path.with_suffix(".source.json"), {"items": [item]})
    if legacy:
        assert len(theapolis.import_all(path)["person"]) == 3
    else:
        with pytest.raises(ValueError, match="differs"):
            theapolis.import_all(path)
        with Session(db) as session:
            assert not session.exec(select(Source)).all()


def test_json_export_omits_null_and_empty_containers(tmp_path):
    path = tmp_path / "clean.json"
    theapolis._write_json(
        path,
        {"null": None, "object": {}, "array": [], "nested": [{"empty": [], "object": {}, "zero": 0}]},
    )
    assert json.loads(path.read_text()) == {"nested": [{"zero": 0}]}


def test_export_error_preserves_previous_file(monkeypatch, tmp_path):
    async def fetch(session, url):
        await asyncio.sleep(0)
        return b'{"success": false}'

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    path = tmp_path / "export.json"
    path.write_text("previous")
    with pytest.raises(ValueError, match="rejected"):
        asyncio.run(theapolis.download(path))
    assert path.read_text() == "previous"


def test_first_page_error_still_creates_json(monkeypatch, tmp_path):
    async def fetch(session, url):
        await asyncio.sleep(0)
        raise TimeoutError("page unavailable")

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    path = tmp_path / "export.json"
    with pytest.raises(TimeoutError, match="page unavailable"):
        asyncio.run(theapolis.download(path))
    assert json.loads(path.read_text()) == []
    source = json.loads(theapolis._source_path(path).read_text())
    assert source["success"] is False
    assert source["partial"] is True


def test_parallel_failure_stops_pending_tasks_before_checkpoint():
    completed = []
    stopped = []
    checkpoints = []

    async def action(value):
        if value == 1:
            completed.append(value)
        elif value == 2:
            await asyncio.sleep(0)
            raise ValueError("failed")
        else:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(value)

    with pytest.raises(ValueError, match="failed"):
        asyncio.run(
            theapolis._parallel(
                [1, 2, 3],
                action,
                3,
                checkpoint=lambda: checkpoints.append((completed.copy(), stopped.copy())),
            )
        )
    assert checkpoints == [([1], [3])]


def test_parallel_workers_do_not_wait_for_slow_peer_and_preserve_order():
    async def run():
        third_started = asyncio.Event()
        active = peak = 0

        async def action(value):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if value == 1:
                await asyncio.wait_for(third_started.wait(), timeout=1)
            elif value == 3:
                third_started.set()
            await asyncio.sleep(0)
            active -= 1
            return value

        results = await theapolis._parallel(range(1, 6), action, 2)
        assert results == [1, 2, 3, 4, 5]
        assert peak == 2

    asyncio.run(run())


def test_periodic_checkpoint_uses_sixty_second_interval(monkeypatch):
    now = 0
    saved = []
    monkeypatch.setattr(theapolis, "monotonic", lambda: now)
    checkpoint = theapolis._periodic_checkpoint(lambda: saved.append(now))
    for instant in [0, 59, 60, 61, 119, 120]:
        now = instant
        checkpoint()
    assert saved == [60, 120]


def test_profile_cache_reuses_found_and_missing_profiles_and_expires(monkeypatch, tmp_path, item):
    now = 1_000_000
    monkeypatch.setattr(theapolis, "time", lambda: now)
    calls = []

    async def lookup(session, name):
        calls.append(name)
        await asyncio.sleep(0)
        return {"url": f"{theapolis.SITE}/de/profil/alex"} if name == "Alex" else None

    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)

    async def enrich(records):
        async with theapolis._session(2) as session:
            await theapolis._enrich_people(session, records, 2, cache=await theapolis._ProfileCache.load())

    def run():
        records = [copy.deepcopy(item)]
        asyncio.run(enrich(records))
        assert records[0]["directors"][0]["profile"]["url"].endswith("/alex")
        assert "profile" not in records[0]["creators"][1]

    run()
    assert calls == ["Alex", "Other Alex"]
    calls.clear()
    now += 30 * 86400
    run()
    assert not calls
    now += 1
    run()
    assert calls == ["Alex", "Other Alex"]


def test_profile_cache_resumes_after_error_without_caching_failure(monkeypatch, tmp_path, item):
    calls = []
    failing = True

    async def lookup(session, name):
        calls.append(name)
        await asyncio.sleep(0)
        if name == "Other Alex" and failing:
            raise TimeoutError("unavailable")
        return {"url": f"{theapolis.SITE}/de/profil/{name.replace(' ', '-').lower()}"}

    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)

    async def enrich():
        async with theapolis._session(1) as session:
            await theapolis._enrich_people(
                session, [copy.deepcopy(item)], 1, cache=await theapolis._ProfileCache.load()
            )

    with pytest.raises(TimeoutError, match="unavailable"):
        asyncio.run(enrich())
    calls.clear()
    failing = False
    asyncio.run(enrich())
    assert calls == ["Other Alex"]


def test_profile_cache_database_error_is_not_silently_ignored(monkeypatch):
    def fail():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(theapolis, "get_session", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(theapolis._ProfileCache.load())


def test_profile_cache_upsert_keeps_newest_and_does_not_write_domain_records():
    newest = TheapolisProfileCache(name="Alex", profile={"slug": "alex"}, captured_at=200)
    theapolis._ProfileCache._put(newest)
    theapolis._ProfileCache._put(TheapolisProfileCache(name="Alex", profile=None, captured_at=100))
    with user_management.get_session() as session:
        entries = session.exec(select(TheapolisProfileCache)).all()
        assert len(entries) == 1
        assert entries[0].profile == {"slug": "alex"}
        assert entries[0].captured_at == 200
        assert not session.exec(select(Person)).all()
        assert not session.exec(select(Source)).all()
        assert not session.exec(select(ReconciliationCase)).all()
    theapolis._ProfileCache._put(TheapolisProfileCache(name="Alex", profile=None, captured_at=300))
    with user_management.get_session() as session:
        entry = session.exec(select(TheapolisProfileCache)).one()
        assert entry.profile is None
        assert entry.captured_at == 300


def test_profile_cache_commit_failure_propagates(monkeypatch):
    def fail(entry):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(theapolis._ProfileCache, "_put", staticmethod(fail))
    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(theapolis._ProfileCache({}).put("Alex", None))


def test_profile_cache_cancellation_waits_for_commit(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    committed = []
    original_put = theapolis._ProfileCache._put

    def put(entry):
        started.set()
        assert release.wait(timeout=5)
        original_put(entry)
        committed.append(entry.name)

    monkeypatch.setattr(theapolis._ProfileCache, "_put", staticmethod(put))

    async def run():
        task = asyncio.create_task(theapolis._ProfileCache({}).put("Alex", None))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert committed == ["Alex"]
    with user_management.get_session() as session:
        assert session.exec(select(TheapolisProfileCache)).one().name == "Alex"


def test_thirty_thousand_people_avoid_full_json_per_batch(monkeypatch, tmp_path):
    records = [
        {"id": index, "name": f"Production {index}", "directors": [{"id": index, "name": f"Person {index}"}]}
        for index in range(30_000)
    ]
    calls = writes = 0
    original_write = theapolis._write_json

    async def fetch(session, url):
        await asyncio.sleep(0)
        return json.dumps(
            {"success": True, "data": {"totalCount": 30_000, "numItemsPerPage": 30_000, "items": records}}
        ).encode()

    async def lookup(session, name):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    def write(path, data):
        nonlocal writes
        writes += 1
        original_write(path, data)

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)
    monkeypatch.setattr(theapolis, "_write_json", write)
    monkeypatch.setattr(theapolis, "_periodic_checkpoint", lambda checkpoint: lambda: None)
    path = tmp_path / "export.json"
    asyncio.run(theapolis.download(path))
    assert calls == 30_000
    assert writes == 9
    assert len(json.loads(path.read_text())) == 30_000
    calls = writes = 0
    asyncio.run(theapolis.download(path))
    assert calls == 0
    assert writes == 7
    assert len(json.loads(path.read_text())) == 30_000


@pytest.mark.parametrize("birthday", ["_____________", "_", "  _____________  "])
def test_export_with_placeholder_birthday_completes(monkeypatch, tmp_path, item, caplog, birthday):
    profile = {"url": f"{theapolis.SITE}/de/profil/alex", "birthDate": birthday}

    async def fetch(session, url):
        await asyncio.sleep(0)
        return json.dumps(
            {"success": True, "data": {"totalCount": 1, "numItemsPerPage": 500, "items": [item]}}
        ).encode()

    async def lookup(session, name):
        await asyncio.sleep(0)
        return profile

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)
    path = asyncio.run(theapolis.download(tmp_path / "export.json"))
    source = json.loads(theapolis._source_path(path).read_text())
    assert source["success"] is True and source["partial"] is False
    assert source["items"][0]["directors"][0]["profile"] == profile
    assert all("born" not in member for member in json.loads(path.read_text())[0]["memberships"])
    assert not caplog.records


def test_failed_page_batch_saves_completed_pages_in_order(monkeypatch, tmp_path, item):
    path = tmp_path / "export.json"

    async def fetch(session, url):
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        if page == 2:
            await asyncio.sleep(0)
        if page == 4:
            assert len(json.loads(path.read_text())) == 3
            raise TimeoutError("page unavailable")
        return json.dumps(
            {
                "success": True,
                "data": {"totalCount": 2500, "numItemsPerPage": 500, "items": [{**item, "id": page}]},
            }
        ).encode()

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    with pytest.raises(TimeoutError, match="page unavailable"):
        asyncio.run(theapolis.download(path, concurrency=2))
    source = json.loads(theapolis._source_path(path).read_text())
    assert [record["id"] for record in source["items"]] == [1, 2, 3, 5]
    assert source["success"] is False and source["partial"] is True
    assert json.loads(path.read_text()) == theapolis.without_null_fields(theapolis.staged_records(source["items"]))


@pytest.mark.parametrize("cancelled", [False, True])
def test_profile_error_checkpoints_completed_enrichment(monkeypatch, tmp_path, item, cancelled):
    path = tmp_path / "export.json"
    profile = {"url": f"{theapolis.SITE}/de/profil/alex", "birthDate": "_____________"}

    async def fetch(session, url):
        await asyncio.sleep(0)
        return json.dumps(
            {"success": True, "data": {"totalCount": 1, "numItemsPerPage": 500, "items": [item]}}
        ).encode()

    async def lookup(session, name):
        assert path.exists()
        if name == "Alex":
            return profile
        await asyncio.sleep(0)
        if cancelled:
            raise asyncio.CancelledError
        raise ValueError("profile unavailable")

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)
    with pytest.raises(asyncio.CancelledError if cancelled else ValueError):
        asyncio.run(theapolis.download(path, concurrency=2))
    source = json.loads(theapolis._source_path(path).read_text())
    assert source["items"][0]["directors"][0]["profile"] == profile
    assert source["success"] is False and source["partial"] is True
    records = json.loads(path.read_text())
    assert records == theapolis.without_null_fields(theapolis.staged_records(source["items"]))
    assert records[0]["memberships"][0]["url"] == profile["url"]
    assert "born" not in records[0]["memberships"][0]
    with pytest.raises(ValueError, match="incomplete"):
        theapolis.import_all(path)


def test_invalid_production_preserves_raw_page_and_last_valid_json(monkeypatch, tmp_path, item):
    path = tmp_path / "export.json"

    async def fetch(session, url):
        await asyncio.sleep(0)
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        record = {**item, "id": page}
        if page == 2:
            record["premierDate"] = "invalid"
        return json.dumps(
            {"success": True, "data": {"totalCount": 1000, "numItemsPerPage": 500, "items": [record]}}
        ).encode()

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    with pytest.raises(ValueError, match="Invalid calendar date"):
        asyncio.run(theapolis.download(path))
    source = json.loads(theapolis._source_path(path).read_text())
    assert source["items"][1]["premierDate"] == "invalid"
    assert source["success"] is False and source["partial"] is True
    assert len(json.loads(path.read_text())) == 1


def test_accepted_import_is_idempotent_and_preserves_source_only_urls(db, tmp_path, item):
    path = tmp_path / "export.json"
    year_only = {**copy.deepcopy(item), "id": 124, "premierDate": None}
    path.write_text(json.dumps({"items": [item, year_only], "captured_at": "2026-11-05T12:00:00Z"}))
    result = theapolis.import_all(path)
    assert result == theapolis.import_all(path)
    assert {key: len(value) for key, value in result.items()} == {
        "organization": 1,
        "person": 3,
        "text": 2,
        "production": 2,
    }
    with Session(db) as session:
        assert not session.exec(select(ReconciliationCase)).all()
        persons = session.exec(select(Person)).all()
        assert {tuple(p.type) for p in persons if p.name == "Alex"} == {
            ("director",),
            ("author",),
        }
    with Session(db) as session:
        productions = session.exec(select(Production).order_by(col(Production.premiere))).all()
        assert len(productions) == 2
        assert productions[0].premiere == dt.datetime(2026, 1, 1)
        assert productions[0].premiere_precision == "year"
        assert productions[1].premiere is not None
        assert productions[1].premiere.date() == dt.date(2026, 10, 3)
        assert all(p.url is None for p in productions)
        organization = session.exec(select(Organization)).one()
        assert organization.url is None
        assert organization.lat == pytest.approx(52.5) and organization.lon == pytest.approx(13.4)
        assert any("produktionen/123" in (s.external_url or "") for s in session.exec(select(EntitySource)).all())


def test_role_scoped_person_ids_keep_colliding_credits_separate(db, tmp_path, item):
    item["directors"] = [{"id": 15696, "name": "Director"}]
    item["creators"] = [{"id": 15696, "name": "Author"}]
    path = tmp_path / "colliding-ids.json"
    theapolis._write_json(path, theapolis.staged_records([item]))
    theapolis._write_json(path.with_suffix(".source.json"), {"items": [item]})

    result = theapolis.import_all(path)
    assert result == theapolis.import_all(path)
    with Session(db) as session:
        records = {
            record.external_id: record
            for record in session.exec(select(SourceRecord).where(SourceRecord.entity_type == "person"))
        }
        names = {person.id: person.name for person in session.exec(select(Person))}
        assert {key: names[record.entity_id] for key, record in records.items()} == {
            "directors:15696": "Director",
            "creators:15696": "Author",
        }
        production = session.get(Production, uuid.UUID(result["production"][0]))
        text = session.get(Text, uuid.UUID(result["text"][0]))
        assert production is not None and text is not None
        assert {(member.person_id, member.role) for member in production.memberships} == {
            (records["directors:15696"].entity_id, "director"),
            (records["creators:15696"].entity_id, "author"),
        }
        assert {credit.person.name for credit in text.contributions} == {"Author"}


def test_same_role_person_id_name_conflicts_fail_before_writes(db, tmp_path, item):
    other = copy.deepcopy(item)
    other["id"] = 124
    other["directors"][0]["name"] = "Different"
    path = tmp_path / "conflicting-names.json"
    path.write_text(json.dumps({"items": [item, other]}))
    with pytest.raises(ValueError, match="Inconsistent name for person ID directors:9"):
        theapolis.import_all(path)
    with Session(db) as session:
        assert not session.exec(select(Source)).all()
        assert not session.exec(select(ReconciliationCase)).all()


def test_enrichment_looks_up_names_with_colliding_role_ids(monkeypatch, item, caplog):
    item["directors"] = [{"id": 15696, "name": "Director"}]
    item["creators"] = [{"id": 15696, "name": "Author"}]
    looked_up = []

    async def lookup(session, name):
        looked_up.append(name)
        await asyncio.sleep(0)
        return {"url": f"{theapolis.SITE}/de/profil/{name.lower()}"}

    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)

    async def run():
        async with theapolis._session(1) as session:
            await theapolis._enrich_people(session, [item], 2)

    with caplog.at_level("INFO", logger=theapolis.__name__):
        asyncio.run(run())
    assert set(looked_up) == {"Director", "Author"}
    assert item["directors"][0]["profile"]["url"].endswith("/director")
    assert item["creators"][0]["profile"]["url"].endswith("/author")
    assert "0/2 people missing (0.0%)" in caplog.text


def test_all_items_validated_before_database_writes(db, tmp_path, item):
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"items": [item, {**item, "id": 124, "premierDate": "not a date"}]}))
    with pytest.raises(ValueError):
        theapolis.import_all(path)
    with Session(db) as session:
        assert not session.exec(select(Source)).all()
        assert not session.exec(select(ReconciliationCase)).all()


def test_legacy_api_envelope_accepted(db, tmp_path, item):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"data": {"items": [item]}}))
    assert len(theapolis.import_all(path)["production"]) == 1


def test_staged_export_reimports_using_source_ids(db, tmp_path, item):
    item["premierDate"] = "2026-01"
    path = tmp_path / "productions.json"
    path.write_text(json.dumps(theapolis.staged_records([item])))
    path.with_suffix(".source.json").write_text(json.dumps({"items": [item]}))
    result = theapolis.import_all(path)
    assert len(result["production"]) == 1
    with Session(db) as session:
        production = session.get(Production, uuid.UUID(result["production"][0]))
        assert production is not None
        assert production.premiere == dt.datetime(2026, 1, 1)
        assert production.premiere_precision == "month"
        assert production.url is None
        record = session.exec(select(SourceRecord).where(SourceRecord.entity_id == production.id)).one()
        assert record.external_id == str(item["id"])


def test_staged_import_rejects_missing_or_mismatched_metadata_before_writes(db, tmp_path, item):
    path = tmp_path / "productions.json"
    records = theapolis.staged_records([item])
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError, match="requires source metadata"):
        theapolis.import_all(path)
    path.with_suffix(".source.json").write_text(json.dumps({"items": [item]}))
    records[0]["title"] = "Changed"
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError, match="differs"):
        theapolis.import_all(path)
    with Session(db) as session:
        assert not session.exec(select(Source)).all()


def test_default_exports_use_shared_timestamp_scope_names_and_import_latest_full(db, monkeypatch, tmp_path, item):
    assert str(theapolis.DEFAULT_EXPORT_DIR) == "data/staged/theapolis.de"
    monkeypatch.setattr(theapolis, "DEFAULT_EXPORT_DIR", tmp_path)

    async def fetch(session, url):
        await asyncio.sleep(0)
        if "/api/de/profiles?" in url:
            return b'{"success": true, "data": {"items": []}}'
        return json.dumps(
            {"success": True, "data": {"totalCount": 1, "numItemsPerPage": 500, "items": [item]}}
        ).encode()

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    first = asyncio.run(theapolis.download())
    second = asyncio.run(theapolis.download())
    limited = asyncio.run(theapolis.download(max_pages=1))
    assert first != second and first.exists() and second.exists()
    assert re.fullmatch(r"\d{8}_\d{6}_\d{6}_all-people-[0-9a-f]{12}_productions\.json", second.name)
    assert re.fullmatch(r"\d{8}_\d{6}_\d{6}_limit-500-people-[0-9a-f]{12}_productions\.json", limited.name)
    assert second.with_suffix(".source.json").exists()
    incomplete = tmp_path / f"99999999_999999_999999_{theapolis._export_scope(None)}_productions.json"
    theapolis._write_json(incomplete, theapolis.staged_records([item]))
    theapolis._write_json(theapolis._source_path(incomplete), {"success": False, "partial": True, "items": [item]})
    assert theapolis.import_all() == theapolis.import_all(second)
    assert theapolis.import_all() == theapolis.import_all(limited)


def test_default_import_does_not_select_limited_snapshot(db, monkeypatch, tmp_path, item):
    monkeypatch.setattr(theapolis, "DEFAULT_EXPORT_DIR", tmp_path)
    limited = tmp_path / f"20261105_120000_000001_{theapolis._export_scope(1)}_productions.json"
    limited.write_text(json.dumps(theapolis.staged_records([item])))
    with pytest.raises(ValueError, match="No full Theapolis snapshot"):
        theapolis.import_all()


def test_direct_import_preserves_conflicts_fills_blanks_and_keeps_source_observation(db, tmp_path, item):
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"items": [item], "captured_at": "2026-11-01T12:00:00Z"}))
    result = theapolis.import_all(path)
    with Session(db) as session:
        production = session.get(Production, uuid.UUID(result["production"][0]))
        organization = session.get(Organization, uuid.UUID(result["organization"][0]))
        assert production is not None and organization is not None
        production.name = "Manual title"
        production.url = "https://official.example/hamlet"
        organization.city = "Manual city"
        session.add_all([production, organization])
        session.commit()
    item["name"] = "Changed source title"
    item["organization"]["location"]["city"] = "Hamburg"
    item["directors"][0]["profile"] = {"birthDate": "1980", "url": f"{theapolis.SITE}/de/profil/alex"}
    path.write_text(json.dumps({"items": [item], "captured_at": "2026-11-02T12:00:00Z"}))
    assert theapolis.import_all(path) == result
    with Session(db) as session:
        production = session.exec(select(Production)).one()
        assert production.name == "Manual title" and production.url == "https://official.example/hamlet"
        assert session.exec(select(Organization)).one().city == "Manual city"
        director = session.exec(select(SourceRecord).where(SourceRecord.external_id == "directors:9")).one()
        person = session.get(Person, director.entity_id)
        assert person is not None and person.born_precision == "year"
        observation = session.exec(select(SourceRecord).where(SourceRecord.entity_id == production.id)).one()
        assert observation.payload["incoming"]["name"] == "Changed source title"
        before = len(session.exec(select(SCD2History)).all())
    assert theapolis.import_all(path) == result
    with Session(db) as session:
        assert len(session.exec(select(SCD2History)).all()) == before
        assert not session.exec(select(ReconciliationCase)).all()


def test_reimport_after_people_and_text_merge_reuses_survivors(db, tmp_path, item):
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"items": [item, {**item, "id": 124}]}))
    result = theapolis.import_all(path)
    targets = {}
    for kind, ids in (("person", result["person"][:2]), ("text", result["text"])):
        preview = preview_entity_merge(kind, ids)
        targets[kind] = str(
            merge_entities(
                kind,
                ids,
                target_id=preview["target_id"],
                revision=preview["revision"],
                actor_id="test",
            )
        )
    again = theapolis.import_all(path)
    assert set(again["text"]) == {targets["text"]}
    assert len(again["person"]) == 2
    assert targets["person"] in again["person"]
    with Session(db) as session:
        assert len(session.exec(select(Person)).all()) == 2
        assert len(session.exec(select(Text)).all()) == 1
        assert {production.text_id for production in session.exec(select(Production))} == {uuid.UUID(targets["text"])}
        assert all(
            record.payload.get("merged_sources")
            for record in session.exec(select(SourceRecord).where(SourceRecord.entity_type == EntityType.PERSON))
            if record.external_id in {"directors:9", "creators:9"}
        )


def test_import_failure_after_organization_rolls_back_all_writes(db, tmp_path, item):
    item["directors"][0]["profile"] = {"url": "not a source URL"}
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"items": [item]}))
    with pytest.raises(ValueError, match="Invalid source URL"):
        theapolis.import_all(path)
    with Session(db) as session:
        for model in (Organization, Person, Production, Text, Source, SourceRecord, EntitySource, SCD2History):
            assert not session.exec(select(model)).all()


def test_shared_profile_urls_do_not_override_scoped_identity_during_merge(db, tmp_path, item):
    for person in [*item["directors"], *item["creators"]]:
        person["profile"] = {"url": f"{theapolis.SITE}/de/profil/shared"}
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"items": [item]}))
    result = theapolis.import_all(path)
    preview = preview_entity_merge("person", result["person"][:2])
    merge_entities(
        "person", result["person"][:2], target_id=preview["target_id"], revision=preview["revision"], actor_id="test"
    )
    assert len(theapolis.import_all(path)["person"]) == 2


def _old_theapolis_cases(db, item):
    with Session(db) as session:
        source = Source(name="theapolis.de")
        session.add(source)
        session.commit()
        source_id = source.id
    organizations, people, productions, _ = theapolis._prepare_import({"items": [item]})

    def propose(kind, payload, identity=None, key=None, context=None):
        return propose_record(kind, payload, source_id=source_id, external_id=identity, record_key=key, context=context)

    orgs = {key: propose("organization", payload, identity=key) for key, (payload, _) in organizations.items()}
    persons = {key: propose("person", payload, identity=key) for key, payload in people.items()}
    cases = [*orgs.values(), *persons.values()]
    for production_id, org_id, payload, raw_credits in productions:
        credits = [{"case_id": str(persons[key].id), "role": role} for key, role in raw_credits]
        text = propose(
            "text",
            {"title": payload["name"]},
            key=f"work:production:{production_id}",
            context={
                "credits": [credit for credit in credits if credit["role"] == "author"],
            },
        )
        production = propose(
            "production",
            payload,
            identity=production_id,
            context={
                "credits": credits,
                "dependencies": [
                    {"case_id": str(orgs[org_id].id), "field": "organization_id"},
                    {"case_id": str(text.id), "field": "text_id"},
                ],
            },
        )
        cases.extend([text, production])
    return source_id, cases


def test_explicit_migration_preserves_cases_and_reimports_idempotently(db, tmp_path, item):
    _, cases = _old_theapolis_cases(db, item)
    result = migrate_pending_cases()
    assert {kind: len(ids) for kind, ids in result.items()} == {
        "organization": 1,
        "person": 3,
        "text": 1,
        "production": 1,
    }
    with Session(db) as session:
        for case in cases:
            stored = session.get(ReconciliationCase, case.id)
            assert stored is not None and stored.status == "approved"
            assert stored.context["migration"]["target_id"] == str(stored.target_id)
        production = session.exec(select(Production)).one()
        assert production.organization_id and production.text_id
        assert len(production.memberships) == 3
        assert len(session.exec(select(Text)).one().contributions) == 2
    assert all(not ids for ids in migrate_pending_cases().values())
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"items": [item]}))
    reimport = theapolis.import_all(path)
    assert {kind: set(ids) for kind, ids in reimport.items()} == {kind: set(ids) for kind, ids in result.items()}


def test_migration_uses_newest_payload_and_rejects_ambiguous_legacy_namespace(db):
    with Session(db) as session:
        source = Source(name="theapolis.de")
        session.add(source)
        session.commit()
        source_id = source.id
    for name, captured in (("Older", "2026-01-01"), ("Newer", "2026-02-01")):
        propose_record(
            "person",
            {"name": name, "type": ["director"]},
            source_id=source_id,
            external_id="9",
            captured_at=dt.datetime.fromisoformat(captured).replace(tzinfo=dt.UTC),
        )
    migrate_pending_cases()
    with Session(db) as session:
        assert session.exec(select(Person)).one().name == "Newer"
        assert session.exec(select(SourceRecord)).one().external_id == "directors:9"
    ambiguous = propose_record(
        "person", {"name": "Unknown", "type": ["director", "author"]}, source_id=source_id, external_id="10"
    )
    with pytest.raises(ValueError, match=f"Ambiguous legacy Theapolis person case {ambiguous.id}"):
        migrate_pending_cases()
    with Session(db) as session:
        stored = session.get(ReconciliationCase, ambiguous.id)
        assert stored is not None and stored.status == "pending"
        assert len(session.exec(select(Person)).all()) == 1


def test_migration_missing_dependency_rolls_back_earlier_entities(db, item):
    _, cases = _old_theapolis_cases(db, item)
    with Session(db) as session:
        production_case = session.get(ReconciliationCase, cases[-1].id)
        assert production_case is not None
        production_case.context = {"dependencies": [{"case_id": str(uuid.uuid4()), "field": "text_id"}]}
        session.add(production_case)
        session.commit()
    with pytest.raises(ValueError, match="dependency"):
        migrate_pending_cases()
    with Session(db) as session:
        for model in (Organization, Person, Text, Production, SourceRecord, EntitySource, SCD2History):
            assert not session.exec(select(model)).all()
        assert all(case.status == "pending" for case in session.exec(select(ReconciliationCase)))


def test_migration_maps_approved_legacy_people_dependencies_for_reimport(db, tmp_path, item):
    source_id, cases = _old_theapolis_cases(db, item)
    director = next(
        case for case in cases if case.entity_type == EntityType.PERSON and case.incoming["type"] == ["director"]
    )
    with Session(db) as session:
        stored = session.get(ReconciliationCase, director.id)
        assert stored is not None
        stored.record_key = "id:9"
        stored.context = {**stored.context, "external_id": "9", "record_key": "id:9"}
        session.add(stored)
        session.commit()
    preview = preview_case(director.id)
    approved_id = approve_case(director.id, revision=preview["revision"], allow_create=True, actor_id="test")
    migrate_pending_cases()
    with Session(db) as session:
        qualified = session.exec(
            select(SourceRecord).where(
                SourceRecord.source_id == source_id,
                SourceRecord.record_key == "id:directors:9",
            )
        ).one()
        assert qualified.entity_id == approved_id
    path = tmp_path / "legacy-approved.json"
    path.write_text(json.dumps({"items": [item]}))
    result = theapolis.import_all(path)
    assert str(approved_id) in result["person"] and len(result["person"]) == 3


def test_year_matching_and_display_do_not_invent_date(db):
    with Session(db) as session:
        session.add(Production.model_validate({"name": "Hamlet", "premiere": "2026"}))
        session.commit()
    candidate = find_candidates("production", {"name": "Hamlet", "premiere": "2026-10-03"})[0]
    assert "Gleiche Premiere" in candidate["evidence"]
    candidate = find_candidates("production", {"name": "Hamlet", "premiere": "2025"})[0]
    assert "Abweichende Premiere" in candidate["conflicts"]
    record = {"name": "Hamlet", "premiere": "2026-01-01T00:00:00", "premiere_precision": "year"}
    assert premiere_label(record) == "2026"
    assert production_dates([record])[1] == []
    assert filter_productions([record], "", "", [], (dt.date(2026, 6, 1), dt.date(2026, 6, 30))) == [record]
    assert filter_productions([record], "", "", [], (dt.date(2025, 6, 1), dt.date(2025, 6, 30))) == []


def test_year_counts_as_premiere_but_theapolis_never_counts_as_own_website(db):
    with Session(db) as session:
        organization = Organization(name="Theater", url="https://www.theapolis.de/de/organization/theater")
        session.add(organization)
        session.flush()
        session.add(
            Production.model_validate(
                {
                    "name": "Hamlet",
                    "organization_id": organization.id,
                    "premiere": "2026",
                    "url": "https://www.theapolis.de/de/produktionen/123",
                }
            )
        )
        session.commit()
        organization_id = organization.id
    status = theatre_status(organization_id)
    assert status["fields"]["premiere"] == 1
    assert status["own_hosts"] == []
    assert status["own_website"] == 0


def test_sitemap_image_downloads_parallel_with_slug_names(monkeypatch, tmp_path):
    root = theapolis.SITE
    pages = [f"{root}/de/profil/vanessa-waldhart", f"{root}/de/organization/theater"]
    maps = [f"{root}/sitemaps/sitemap_people_1.xml", f"{root}/sitemaps/sitemap_organization_1.xml"]
    png = b"\x89PNG\r\n\x1a\nexample"
    active, peak = 0, 0
    calls = []

    def xml(urls):
        return (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{url}</loc></url>" for url in urls)
            + "</urlset>"
        ).encode()

    async def fetch(session, url):
        nonlocal active, peak
        calls.append(url)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        if url == f"{root}/sitemap.xml":
            return xml(maps)
        if url in maps:
            return xml([pages[maps.index(url)]])
        if url in pages:
            return f'<meta property="og:image" content="https://media.theapolis.de/{pages.index(url)}.png">'.encode()
        return png

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    asyncio.run(theapolis.download_images(tmp_path, concurrency=2))
    assert peak == 2
    assert (tmp_path / "people/vanessa-waldhart.png").read_bytes() == png
    assert (tmp_path / "organizations/theater.png").read_bytes() == png
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["people/vanessa-waldhart"]["page_url"] == pages[0]
    assert manifest["organizations/theater"]["status"] == "downloaded"
    calls.clear()
    asyncio.run(theapolis.download_images(tmp_path, concurrency=2, limit=1))
    assert len([url for url in calls if url in pages]) == 1


def test_missing_image_logged_and_unsafe_images_rejected(caplog):
    assert theapolis.profile_image(b"<html></html>", theapolis.SITE)["status"] == "no-image"
    assert "No profile image" in caplog.text
    with pytest.raises(ValueError, match="Unexpected"):
        theapolis.profile_image(b'<meta property="og:image" content="http://localhost/private">', theapolis.SITE)


def test_fetch_retries_then_propagates_http_errors(monkeypatch):
    attempts = []

    async def response(session, url, attempt):
        await asyncio.sleep(0)
        attempts.append(attempt)
        if attempt == 0:
            return 0.0
        if attempt == 1:
            return 0.0
        return b"ok"

    monkeypatch.setattr(theapolis, "_response", response)

    async def run():
        async with theapolis._session(1) as session:
            return await theapolis._fetch(session, "https://example.test")

    assert asyncio.run(run()) == b"ok"
    assert attempts == [0, 1, 2]

    async def error(session, url, attempt):
        await asyncio.sleep(0)
        info = aiohttp.RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()))
        raise aiohttp.ClientResponseError(info, (), status=403)

    monkeypatch.setattr(theapolis, "_response", error)
    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(run())


def test_missing_organization_stays_unknown(db, tmp_path, item):
    item["organization"] = None
    path = tmp_path / "independent.json"
    path.write_text(json.dumps({"items": [item]}))
    result = theapolis.import_all(path)
    assert not result["organization"]
    with Session(db) as session:
        production = session.get(Production, uuid.UUID(result["production"][0]))
        assert production is not None and production.organization_id is None


def test_image_failure_is_recorded_while_other_downloads_continue(monkeypatch, tmp_path):
    pages = [f"{theapolis.SITE}/de/profil/broken", f"{theapolis.SITE}/de/profil/working"]
    completed = []

    async def profiles(session, concurrency, limit):
        await asyncio.sleep(0)
        return pages

    async def image(session, page_url, output):
        await asyncio.sleep(0)
        if page_url.endswith("/broken"):
            raise ValueError("Invalid image")
        completed.append(page_url)
        return {"id": "working", "kind": "people", "status": "downloaded", "page_url": page_url}

    monkeypatch.setattr(theapolis, "_profile_pages", profiles)
    monkeypatch.setattr(theapolis, "_download_image", image)
    with pytest.raises(RuntimeError, match="1 image downloads failed"):
        asyncio.run(theapolis.download_images(tmp_path, concurrency=2))
    assert completed == [pages[1]]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["people/broken"]["status"] == "failed"
    assert manifest["people/working"]["status"] == "downloaded"
