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
from sqlalchemy import event
from sqlmodel import Session, SQLModel, col, create_engine, select
from yarl import URL

from backend import user_management
from backend.config import canonical_role
from backend.date_precision import DatePrecision
from backend.entity_management import browse_entities, list_entities
from backend.import_status import theatre_status
from backend.ingestion import theapolis
from backend.ingestion.staging import staging_schema, validate_records
from backend.ingestion.theapolis_import import migrate_pending_cases
from backend.models import (
    Award,
    AwardNomination,
    AwardResult,
    EntityRedirect,
    EntitySource,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Source,
    SourceRecord,
    Text,
    TextContribution,
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
        if "/api/de/organizations?" in url:
            return json.dumps(
                {"success": True, "data": {"totalCount": 1, "numItemsPerPage": 500, "items": [item["organization"]]}}
            ).encode()
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
    item["type"] = {"id": 1, "name": "Uraufführung", "pill": "UA"}
    item["tags"] = [{"name": "Musical"}]
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
    result = theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        record = session.exec(select(SourceRecord).where(SourceRecord.external_id == "directors:9")).one()
        person = session.get(Person, record.entity_id)
        assert person is not None
        assert record.external_url == profile["url"]
        assert person.born == dt.datetime(1966, 5, 19)
        assert person.born_precision == "day"
        assert set(person.type) == {"director", "author"}
        assert "tags" not in record.payload["incoming"]
        assert session.exec(select(Production)).one().tags == [
            "Schauspiel",
            "UA",
            "Musical",
            "Freie Darstellende Künste",
        ]
        assert result["person"]


def test_download_imports_live_api_shaped_types_and_descriptions(db, monkeypatch, tmp_path, item):
    item["type"] = {"id": 1, "name": "Uraufführung", "pill": "UA"}
    item["description"] = "<p>Production <strong>summary</strong>.</p>"
    organization = {**item["organization"], "description": "<p>Theatre &amp; opera.</p>"}
    calls = []

    async def fetch(session, url):
        calls.append(url)
        await asyncio.sleep(0)
        if "/api/de/productions?" in url:
            data = {"totalCount": 1, "numItemsPerPage": 500, "items": [item]}
        elif "/api/de/organizations?" in url:
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            data = {"totalCount": 1000, "numItemsPerPage": 500, "items": [organization] if page == 2 else []}
        elif "/api/de/profiles?" in url:
            name = parse_qs(urlsplit(url).query)["search"][0]
            data = {"items": [{"name": "Alex", "slug": "alex"}] if name == "Alex" else []}
        else:
            assert url.endswith("/de/people/api/show/alex")
            return json.dumps(
                {
                    "code": 200,
                    "error": False,
                    "data": {
                        "profile": {
                            "slug": "alex",
                            "biographyText": "<p>Bio <strong>one</strong>.</p>",
                            "extraBiographyText": "<p>Extra.</p>",
                        }
                    },
                }
            ).encode()
        return json.dumps({"success": True, "data": data}).encode()

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    path = asyncio.run(theapolis.download(tmp_path / "descriptions.json", concurrency=2))
    staged = json.loads(path.read_text())[0]
    assert staged["tags"] == ["UA"]
    assert staged["description"] == "Production **summary**."
    assert staged["memberships"][0]["description"] == "Bio **one**.\n\nExtra."
    raw = json.loads(theapolis._source_path(path).read_text())
    assert raw["items"][0]["organization"]["description"] == organization["description"]
    assert len([url for url in calls if "/api/de/organizations?" in url]) == 2
    result = theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert session.exec(select(Organization)).one().description == "Theatre & opera."
        assert session.exec(select(Production)).one().description == "Production **summary**."
        person = session.exec(select(Person).where(Person.name == "Alex")).one()
        assert person.description == "Bio **one**.\n\nExtra."
        person.description = "Manual biography"
        session.commit()
    assert theapolis.import_all(path, dach_only=False) == result
    with Session(db) as session:
        assert session.exec(select(Person).where(Person.name == "Alex")).one().description == "Manual biography"


@pytest.mark.parametrize("version", [1, 2])
def test_old_export_metadata_is_upgraded_but_new_exports_remain_checked(db, tmp_path, item, version):
    legacy = theapolis.staged_records([item])
    item["type"] = {"id": 1, "name": "Uraufführung", "pill": "UA"}
    item["description"] = "<p>Source description.</p>"
    path = tmp_path / "legacy-metadata.json"
    theapolis._write_json(path, legacy)
    theapolis._write_json(theapolis._source_path(path), {"format_version": version, "items": [item]})
    if version == 2:
        with pytest.raises(ValueError, match="differs"):
            theapolis.import_all(path, dach_only=False)
    else:
        theapolis.import_all(path, dach_only=False)
        with Session(db) as session:
            production = session.exec(select(Production)).one()
            assert production.tags == ["UA"]
            assert production.description == "Source description."


def test_legacy_export_still_rejects_missing_explicit_source_tags(tmp_path, item):
    item["tags"] = ["UA"]
    item["type"] = {"pill": "UA"}
    records = theapolis.staged_records([item])
    records[0]["tags"] = []
    path = tmp_path / "edited-legacy.json"
    theapolis._write_json(path, records)
    theapolis._write_json(theapolis._source_path(path), {"items": [item]})
    with pytest.raises(ValueError, match="differs"):
        theapolis.import_all(path, dach_only=False)


def test_repeated_organization_keeps_available_description(db, tmp_path, item):
    first = copy.deepcopy(item)
    first["organization"]["description"] = "<p>Theatre description.</p>"
    path = tmp_path / "organization-description.json"
    path.write_text(json.dumps({"items": [first, {**item, "id": 124}]}))
    theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert session.exec(select(Organization)).one().description == "Theatre description."


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
        "description": None,
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
    result = theapolis.import_all(path, dach_only=False)
    if not with_people:
        assert not result["person"]
        return
    memberships = json.loads(path.read_text())[0]["memberships"]
    author = next(member for member in memberships if member["external_id"] == "9" and member["role"] == "author")
    assert author["name"] == "Alex"
    assert "url" not in author and "born" not in author
    with Session(db) as session:
        assert not session.exec(select(ReconciliationCase).where(ReconciliationCase.status == "pending")).all()
        records = session.exec(select(SourceRecord).where(SourceRecord.entity_type == "person")).all()
        assert {record.external_id for record in records} == {"directors:9", "creators:9", "creators:10"}
    with Session(db) as session:
        sources = session.exec(select(EntitySource).where(EntitySource.entity_type == "person")).all()
        assert len(sources) == 2
        assert "creators:10" in {source.external_id for source in sources}


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
        assert len(theapolis.import_all(path, dach_only=False)["person"]) == 2
    else:
        with pytest.raises(ValueError, match="differs"):
            theapolis.import_all(path, dach_only=False)
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


def test_profile_cache_refreshes_legacy_entries_for_biographies(monkeypatch, item):
    theapolis._ProfileCache._put(
        TheapolisProfileCache(name="Alex", profile={"slug": "alex"}, captured_at=theapolis.time())
    )
    lookup = AsyncMock(return_value={"slug": "alex", "description": "<p>Biography.</p>"})
    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)

    async def run():
        async with theapolis._session(1) as session:
            records = [copy.deepcopy(item)]
            await theapolis._enrich_people(session, records, 1, cache=await theapolis._ProfileCache.load())
            return records

    assert asyncio.run(run())[0]["directors"][0]["profile"]["description"] == "<p>Biography.</p>"
    lookup.reset_mock()
    asyncio.run(run())
    lookup.assert_not_called()


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


def test_profile_cache_serializes_concurrent_commits(monkeypatch):
    active = peak = 0
    committed = []

    async def to_thread(function, entry):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            function(entry)
        finally:
            active -= 1

    monkeypatch.setattr(theapolis.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(theapolis._ProfileCache, "_put", staticmethod(committed.append))
    cache = theapolis._ProfileCache({})

    async def run():
        await asyncio.gather(*(cache.put(f"Person {index}", None) for index in range(10)))

    asyncio.run(run())
    assert peak == 1
    assert {entry.name for entry in committed} == set(cache.entries)
    assert len(committed) == 10


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
        if "/api/de/organizations?" in url:
            return json.dumps(
                {"success": True, "data": {"totalCount": 1, "numItemsPerPage": 500, "items": [item["organization"]]}}
            ).encode()
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
        if "/api/de/organizations?" in url:
            return json.dumps(
                {"success": True, "data": {"totalCount": 1, "numItemsPerPage": 500, "items": [item["organization"]]}}
            ).encode()
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
        theapolis.import_all(path, dach_only=False)


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
    result = theapolis.import_all(path, dach_only=False)
    assert result == theapolis.import_all(path, dach_only=False)
    assert {key: len(value) for key, value in result.items()} == {
        "organization": 1,
        "person": 2,
        "text": 2,
        "production": 2,
    }
    with Session(db) as session:
        assert not session.exec(select(ReconciliationCase).where(ReconciliationCase.status == "pending")).all()
        persons = session.exec(select(Person)).all()
        assert [set(p.type) for p in persons if p.name == "Alex"] == [{"director", "author"}]
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


def test_dach_import_limits_all_related_records_and_counts_distinct_productions(db, tmp_path, item):
    items = []
    for org_id, country in enumerate(["Nordrhein-Westfalen", "Wien", "Kanton Bern", "France", None, "Germany"], 1):
        for production in range(2 if org_id != 6 else 1):
            record = copy.deepcopy(item)
            record["id"] = org_id * 100 + production
            record["name"] = f"Production {record['id']}"
            record["organization"].update(id=org_id, name=f"Organization {org_id}")
            record["organization"]["location"]["country"] = country
            record["directors"] = [{"id": org_id, "name": f"Director {org_id}"}]
            record["creators"] = [{"id": org_id, "name": f"Author {org_id}"}]
            items.append(record)
    items.append(copy.deepcopy(items[-1]))
    orphan = copy.deepcopy(item)
    orphan.update(id=999, organization=None, directors=[{"id": 99, "name": "Orphan"}], creators=[])
    items.append(orphan)
    path = tmp_path / "filtered.json"
    path.write_text(json.dumps({"items": items}))

    result = theapolis.import_all(path)
    assert {kind: len(ids) for kind, ids in result.items()} == {
        "organization": 3,
        "person": 6,
        "text": 6,
        "production": 6,
    }
    assert theapolis.import_all(path) == result
    with Session(db) as session:
        assert {org.name: (org.country, org.region) for org in session.exec(select(Organization))} == {
            "Organization 1": ("Deutschland", "Nordrhein-Westfalen"),
            "Organization 2": ("Österreich", "Wien"),
            "Organization 3": ("Schweiz", "Bern"),
        }
        assert {person.name for person in session.exec(select(Person))} == {
            f"{role} {number}" for role in ("Director", "Author") for number in range(1, 4)
        }
        assert len(session.exec(select(ProductionMembership)).all()) == 12
        assert len(session.exec(select(TextContribution)).all()) == 6

    unfiltered = theapolis.import_all(path, dach_only=False)
    assert len(unfiltered["organization"]) == 6
    assert len(unfiltered["production"]) == 12
    assert len(theapolis.import_all(path, dach_only=True)["production"]) == 6
    with Session(db) as session:
        assert len(session.exec(select(Production)).all()) == 12
        foreign = session.exec(select(Organization).where(Organization.name == "Organization 4")).one()
        assert (foreign.country, foreign.region) == ("Frankreich", None)


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("de", "Deutschland"),
        (" Germany ", "Deutschland"),
        ("AT", "Österreich"),
        ("austria", "Österreich"),
        ("ch", "Schweiz"),
        ("Switzerland", "Schweiz"),
    ],
)
def test_dach_import_accepts_country_aliases(db, item, tmp_path, country, expected):
    item["organization"]["location"]["country"] = country
    second = {**copy.deepcopy(item), "id": 124}
    path = tmp_path / "countries.json"
    path.write_text(json.dumps({"items": [item, second]}))
    assert len(theapolis.import_all(path, dach_only=True)["production"]) == 2
    with Session(db) as session:
        org = session.exec(select(Organization)).one()
        assert (org.country, org.region) == (expected, None)


@pytest.mark.parametrize(
    ("existing_country", "expected"),
    [
        ("Kanton Tessin", ("Schweiz", "Tessin")),
        ("Italien", ("Italien", None)),
    ],
)
def test_reimport_repairs_only_unchanged_legacy_region(db, tmp_path, item, existing_country, expected):
    path = tmp_path / "legacy.json"
    location = item["organization"]["location"]
    location.update(city="Lugano", country="Schweiz")
    path.write_text(json.dumps({"items": [item], "captured_at": "2026-11-01T12:00:00Z"}))
    result = theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        org = session.exec(select(Organization)).one()
        org.country = existing_country
        record = session.exec(select(SourceRecord).where(SourceRecord.entity_id == org.id)).one()
        record.payload = {**record.payload, "incoming": {**record.payload["incoming"], "country": "Kanton Tessin"}}
        session.commit()

    location["country"] = "Kanton Tessin"
    path.write_text(json.dumps({"items": [item], "captured_at": "2026-11-02T12:00:00Z"}))
    assert theapolis.import_all(path, dach_only=False) == result
    assert theapolis.import_all(path, dach_only=False) == result
    with Session(db) as session:
        org = session.exec(select(Organization)).one()
        assert (org.country, org.region) == expected


@pytest.mark.parametrize(("switch", "dach_only"), [([], True), (["--dach-only"], True), (["--all"], False)])
def test_dach_import_cli_switch(monkeypatch, tmp_path, switch, dach_only):
    path = tmp_path / "export.json"
    calls = []
    monkeypatch.setattr("sys.argv", ["theapolis", "import", "--json", str(path), *switch])
    monkeypatch.setattr(
        theapolis, "import_all", lambda json_path, *, dach_only: calls.append((json_path, dach_only)) or {}
    )
    theapolis.main()
    assert calls == [(path, dach_only)]


def test_role_scoped_person_ids_keep_colliding_credits_separate(db, tmp_path, item):
    item["directors"] = [{"id": 15696, "name": "Director"}]
    item["creators"] = [{"id": 15696, "name": "Author"}]
    path = tmp_path / "colliding-ids.json"
    theapolis._write_json(path, theapolis.staged_records([item]))
    theapolis._write_json(path.with_suffix(".source.json"), {"items": [item]})

    result = theapolis.import_all(path, dach_only=False)
    assert result == theapolis.import_all(path, dach_only=False)
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


@pytest.mark.parametrize("existing_duplicates", [False, True])
def test_same_name_cross_role_merge_preserves_credits_and_source_ids(
    db, monkeypatch, tmp_path, item, existing_duplicates
):
    item["creators"] = [{"id": 77, "name": "Alex"}]
    other = copy.deepcopy(item)
    other["id"] = 124
    item["creators"] = []
    other["directors"] = []
    path = tmp_path / "cross-role.json"
    path.write_text(json.dumps({"items": [item, other]}))

    if existing_duplicates:
        with monkeypatch.context() as patch:
            patch.setattr(theapolis, "_merge_people_by_name", lambda *args: {})
            old = theapolis.import_all(path, dach_only=False)
        assert len(old["person"]) == 2
    else:
        path.write_text(json.dumps({"items": [item]}))
        assert len(theapolis.import_all(path, dach_only=False)["person"]) == 1
        path.write_text(json.dumps({"items": [other]}))

    result = theapolis.import_all(path, dach_only=False)
    assert len(result["person"]) == 1
    assert theapolis.import_all(path, dach_only=False) == result
    with Session(db) as session:
        person = session.exec(select(Person)).one()
        assert set(person.type) == {"director", "author"}
        records = session.exec(select(SourceRecord).where(SourceRecord.entity_type == EntityType.PERSON)).all()
        assert {record.external_id for record in records} == {"directors:9", "creators:77"}
        assert {record.entity_id for record in records} == {person.id}
        assert {(credit.person_id, credit.role) for credit in session.exec(select(ProductionMembership))} == {
            (person.id, "director"),
            (person.id, "author"),
        }
        assert {(credit.person_id, credit.role) for credit in session.exec(select(TextContribution))} == {
            (person.id, "author")
        }


def test_cross_role_merge_rejects_conflicting_person_fields(db, monkeypatch, tmp_path, item):
    item["creators"] = [{"id": 77, "name": "Alex", "profile": {"birthDate": "1980"}}]
    item["directors"][0]["profile"] = {"birthDate": "1970"}
    path = tmp_path / "conflicting-person.json"
    path.write_text(json.dumps({"items": [item]}))
    with monkeypatch.context() as patch:
        patch.setattr(theapolis, "_merge_people_by_name", lambda *args: {})
        theapolis.import_all(path, dach_only=False)
    with pytest.raises(ValueError, match="Conflicting fields for Theapolis person 'Alex'"):
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert {person.born for person in session.exec(select(Person))} == {
            dt.datetime(1970, 1, 1),
            dt.datetime(1980, 1, 1),
        }


def test_batched_cross_role_merge_preserves_all_references_and_audits(db, monkeypatch, tmp_path, item):
    item["creators"] = [{"id": 77, "name": "Alex"}]
    path = tmp_path / "references.json"
    path.write_text(json.dumps({"items": [item]}))
    with monkeypatch.context() as patch:
        patch.setattr(theapolis, "_merge_people_by_name", lambda *args: {})
        result = theapolis.import_all(path, dach_only=False)
    identities = sorted(uuid.UUID(identity) for identity in result["person"])
    target, duplicate = identities
    alias = uuid.uuid4()
    with Session(db) as session:
        production = session.exec(select(Production)).one()
        text = session.exec(select(Text)).one()
        organization = session.exec(select(Organization)).one()
        award = Award(name="Test award")
        session.add(award)
        session.flush()
        session.add_all(
            [
                EntityRedirect(entity_type=EntityType.PERSON, old_id=alias, target_id=duplicate),
                ReconciliationCase(
                    key="pending-alias",
                    entity_type=EntityType.PERSON,
                    operation="merge",
                    target_id=target,
                    duplicate_id=alias,
                    reason="Review",
                ),
                *[
                    row
                    for person_id in identities
                    for row in (
                        ProductionMembership(production_id=production.id, person_id=person_id, role="Regie"),
                        TextContribution(text_id=text.id, person_id=person_id, role="author"),
                        OrganizationMembership(organization_id=organization.id, person_id=person_id, role="director"),
                        AwardNomination(
                            award_id=award.id,
                            person_id=person_id,
                            category="Direction",
                            date=dt.datetime(2026, 1, 1),
                            date_precision=DatePrecision.YEAR,
                            result=AwardResult.NOMINATED,
                        ),
                    )
                ],
            ]
        )
        session.commit()
    result = theapolis.import_all(path, dach_only=False)
    assert result["person"] == [str(target)]
    with Session(db) as session:
        assert session.get(Person, duplicate) is None
        memberships = session.exec(select(ProductionMembership)).all()
        assert len(memberships) == 2
        assert {(row.person_id, canonical_role(row.role)) for row in memberships} == {
            (target, "director"),
            (target, "author"),
        }
        for model in (TextContribution, OrganizationMembership, AwardNomination):
            assert [row.person_id for row in session.exec(select(model))] == [target]
        assert {row.target_id for row in session.exec(select(EntityRedirect))} == {target}
        assert (
            session.exec(select(ReconciliationCase).where(ReconciliationCase.key == "pending-alias")).one().status
            == "approved"
        )
        records = session.exec(select(SourceRecord).where(SourceRecord.entity_type == EntityType.PERSON)).all()
        assert {record.entity_id for record in records} == {target}
        assert all(record.payload["merged_sources"] for record in records)
        deleted = session.exec(select(SCD2History).where(SCD2History.operation == "delete")).all()
        assert {row.entity_name for row in deleted} >= {"person", "textcontribution", "productionmembership"}
        assert all(not row.is_current and row.valid_to is not None for row in deleted)
        person_history = session.exec(
            select(SCD2History).where(SCD2History.entity_name == "person", SCD2History.entity_id == str(duplicate))
        ).all()
        assert all(not row.is_current for row in person_history)
    assert theapolis.import_all(path, dach_only=False) == result


def test_matching_keeps_other_source_identities_on_reviewed_path(db, monkeypatch, tmp_path, item):
    item["creators"] = [{"id": 77, "name": "Alex"}]
    path = tmp_path / "mixed-source.json"
    path.write_text(json.dumps({"items": [item]}))
    with monkeypatch.context() as patch:
        patch.setattr(theapolis, "_merge_people_by_name", lambda *args: {})
        result = theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        source = Source(name="Other source")
        session.add(source)
        session.flush()
        source_id = source.id
        session.add(
            EntitySource(
                source_id=source_id,
                entity_type=EntityType.PERSON,
                entity_id=uuid.UUID(result["person"][1]),
                external_id="other:123",
            )
        )
        session.commit()
    result = theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        person = session.exec(select(Person)).one()
        assert {link.source_id for link in person.sources} >= {source_id}
        record = session.exec(select(SourceRecord).where(SourceRecord.source_id == source_id)).one()
        assert record.external_id == "other:123" and record.entity_id == person.id
    assert theapolis.import_all(path, dach_only=False) == result


@pytest.mark.parametrize("size", [50, 200])
@pytest.mark.parametrize("existing_duplicates", [False, True])
def test_cross_role_matching_uses_bounded_queries(db, monkeypatch, tmp_path, item, size, existing_duplicates):
    items = [
        {
            **item,
            "id": identity,
            "directors": [{"id": identity, "name": f"Person {identity}"}],
            "creators": [{"id": identity, "name": f"Person {identity}"}],
        }
        for identity in range(size)
    ]
    path = tmp_path / "cross-role-batch.json"
    path.write_text(json.dumps({"items": items}))
    if existing_duplicates:
        with monkeypatch.context() as patch:
            patch.setattr(theapolis, "_merge_people_by_name", lambda *args: {})
            theapolis.import_all(path, dach_only=False)
    statements = []

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        # Schema checks grow with every table; only data queries must stay bounded.
        if not statement.startswith("PRAGMA"):
            statements.append(statement)

    event.listen(db, "before_cursor_execute", record_statement)
    try:
        result = theapolis.import_all(path, dach_only=False)
        assert len(result["person"]) == size
        assert len(statements) < 100
        assert sum(statement.startswith("SELECT ") for statement in statements) < 60
        statements.clear()
        assert theapolis.import_all(path, dach_only=False) == result
        assert not any(statement.startswith(("INSERT ", "UPDATE ", "DELETE ")) for statement in statements)
    finally:
        event.remove(db, "before_cursor_execute", record_statement)


def test_import_reports_cross_role_matching_before_productions(db, tmp_path, item, caplog, capsys):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"items": [item]}))
    with caplog.at_level("INFO", logger=theapolis.__name__):
        theapolis.import_all(path, dach_only=False)
    assert "Loading Theapolis people for cross-role matching" in caplog.text
    progress = capsys.readouterr().err
    assert progress.index("Matching people") < progress.index("Productions")


def test_same_role_person_id_name_conflicts_fail_before_writes(db, tmp_path, item):
    other = copy.deepcopy(item)
    other["id"] = 124
    other["directors"][0]["name"] = "Different"
    path = tmp_path / "conflicting-names.json"
    path.write_text(json.dumps({"items": [item, other]}))
    with pytest.raises(ValueError, match="Inconsistent name for person ID directors:9"):
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert not session.exec(select(Source)).all()
        assert not session.exec(select(ReconciliationCase).where(ReconciliationCase.status == "pending")).all()


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
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert not session.exec(select(Source)).all()
        assert not session.exec(select(ReconciliationCase)).all()


def test_legacy_api_envelope_accepted(db, tmp_path, item):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"data": {"items": [item]}}))
    assert len(theapolis.import_all(path, dach_only=False)["production"]) == 1


def test_staged_export_reimports_using_source_ids(db, tmp_path, item):
    item["premierDate"] = "2026-01"
    path = tmp_path / "productions.json"
    path.write_text(json.dumps(theapolis.staged_records([item])))
    path.with_suffix(".source.json").write_text(json.dumps({"items": [item]}))
    result = theapolis.import_all(path, dach_only=False)
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
        theapolis.import_all(path, dach_only=False)
    path.with_suffix(".source.json").write_text(json.dumps({"items": [item]}))
    records[0]["title"] = "Changed"
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError, match="differs"):
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert not session.exec(select(Source)).all()


def test_default_exports_use_shared_timestamp_scope_names_and_import_latest_full(db, monkeypatch, tmp_path, item):
    assert str(theapolis.DEFAULT_EXPORT_DIR) == "data/staged/theapolis.de"
    monkeypatch.setattr(theapolis, "DEFAULT_EXPORT_DIR", tmp_path)

    async def fetch(session, url):
        await asyncio.sleep(0)
        if "/api/de/profiles?" in url:
            return b'{"success": true, "data": {"items": []}}'
        if "/api/de/organizations?" in url:
            return json.dumps(
                {"success": True, "data": {"totalCount": 1, "numItemsPerPage": 500, "items": [item["organization"]]}}
            ).encode()
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
    assert theapolis.import_all(dach_only=False) == theapolis.import_all(second, dach_only=False)
    assert theapolis.import_all(dach_only=False) == theapolis.import_all(limited, dach_only=False)


def test_default_import_does_not_select_limited_snapshot(db, monkeypatch, tmp_path, item):
    monkeypatch.setattr(theapolis, "DEFAULT_EXPORT_DIR", tmp_path)
    limited = tmp_path / f"20261105_120000_000001_{theapolis._export_scope(1)}_productions.json"
    limited.write_text(json.dumps(theapolis.staged_records([item])))
    with pytest.raises(ValueError, match="No full Theapolis snapshot"):
        theapolis.import_all(dach_only=False)


def test_direct_import_preserves_conflicts_fills_blanks_and_keeps_source_observation(db, tmp_path, item):
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"items": [item], "captured_at": "2026-11-01T12:00:00Z"}))
    result = theapolis.import_all(path, dach_only=False)
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
    assert theapolis.import_all(path, dach_only=False) == result
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
    assert theapolis.import_all(path, dach_only=False) == result
    with Session(db) as session:
        assert len(session.exec(select(SCD2History)).all()) == before
        assert not session.exec(select(ReconciliationCase).where(ReconciliationCase.status == "pending")).all()


def test_reimport_after_people_and_text_merge_reuses_survivors(db, monkeypatch, tmp_path, item):
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"items": [item, {**item, "id": 124}]}))
    with monkeypatch.context() as patch:
        patch.setattr(theapolis, "_merge_people_by_name", lambda *args: {})
        result = theapolis.import_all(path, dach_only=False)
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
    again = theapolis.import_all(path, dach_only=False)
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
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        for model in (Organization, Person, Production, Text, Source, SourceRecord, EntitySource, SCD2History):
            assert not session.exec(select(model)).all()


@pytest.mark.parametrize("staged", [False, True])
def test_import_removes_nuls_before_domain_and_json_writes(db, tmp_path, item, caplog, staged):
    item["name"] = "Ham\x00let"
    item["organization"]["name"] = "The\x00ater"
    item["organization"]["location"]["city"] = "Ber\x00lin"
    item["directors"][0]["name"] = "Al\x00ex"
    item["creators"][0]["name"] = "Al\x00ex"
    item["tags"] = ["Dra\x00ma"]
    path = tmp_path / "nul.json"
    if staged:
        path.write_text(json.dumps(theapolis.staged_records([item])))
        path.with_suffix(".source.json").write_text(json.dumps({"items": [item]}))
    else:
        path.write_text(json.dumps({"items": [item]}))
    original = path.read_bytes()

    def reject_nul(connection, cursor, statement, parameters, context, executemany):
        assert "\\u0000" not in repr(parameters)
        assert "\\x00" not in repr(parameters)

    event.listen(db, "before_cursor_execute", reject_nul)
    try:
        result = theapolis.import_all(path, dach_only=False)
        assert result == theapolis.import_all(path, dach_only=False)
    finally:
        event.remove(db, "before_cursor_execute", reject_nul)
    assert "production[123].name" in caplog.text
    assert "production[123].directors[0].name" in caplog.text
    assert path.read_bytes() == original
    assert item["name"] == "Ham\x00let"
    with Session(db) as session:
        assert session.exec(select(Text)).one().title == "Hamlet"
        production = session.exec(select(Production)).one()
        assert production.name == "Hamlet" and production.tags == ["Drama"]
        organization = session.exec(select(Organization)).one()
        assert organization.name == "Theater" and organization.city == "Berlin"
        assert {person.name for person in session.exec(select(Person))} == {"Alex", "Other Alex"}


@pytest.mark.parametrize("field", ["name", "id"])
def test_nul_only_name_or_corrupted_identity_fails_before_writes(db, tmp_path, item, field):
    invalid = copy.deepcopy(item)
    invalid[field] = "\x00"
    path = tmp_path / "invalid-nul.json"
    path.write_text(json.dumps({"items": [item, invalid]}))
    with pytest.raises(ValueError, match=r"needs a name|NUL byte in Theapolis source identity"):
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        for model in (Source, Organization, Person, Text, Production, SourceRecord, EntitySource, SCD2History):
            assert not session.exec(select(model)).all()


def test_batches_are_visible_and_failed_production_batch_resumes(db, monkeypatch, tmp_path, item):
    monkeypatch.setattr(theapolis, "BATCH_SIZE", 2)
    items = []
    for identity in range(5):
        entry = copy.deepcopy(item)
        entry["id"] = identity
        entry["organization"]["id"] = identity
        entry["organization"]["name"] = f"Theatre {identity}"
        entry["directors"] = [{"id": identity, "name": f"Director {identity}"}]
        entry["creators"] = [{"id": identity, "name": f"Author {identity}"}]
        items.append(entry)
    path = tmp_path / "batches.json"
    path.write_text(json.dumps({"items": items}))
    original = theapolis.add_credits_batch
    production_batches = 0

    def fail_second_batch(session, kind, credits, *, actor_id):
        nonlocal production_batches
        if kind == EntityType.PRODUCTION:
            production_batches += 1
            assert len(list_entities(Organization)) == 5
            assert browse_entities(Person)[1] == 10
            assert browse_entities(Production)[1] == (production_batches - 1) * 2
        original(session, kind, credits, actor_id=actor_id)
        if kind == EntityType.PRODUCTION and production_batches == 2:
            raise ValueError("injected batch failure")

    monkeypatch.setattr(theapolis, "add_credits_batch", fail_second_batch)
    with pytest.raises(ValueError, match="injected batch failure"):
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert len(session.exec(select(Production)).all()) == 2
        assert len(session.exec(select(Text)).all()) == 2
        assert len(session.exec(select(ProductionMembership)).all()) == 4
        assert len(session.exec(select(TextContribution)).all()) == 2
    monkeypatch.setattr(theapolis, "add_credits_batch", original)
    result = theapolis.import_all(path, dach_only=False)
    assert len(result["production"]) == 5
    assert result == theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert len(session.exec(select(ProductionMembership)).all()) == 10
        assert len(session.exec(select(TextContribution)).all()) == 5
        history = session.exec(select(SCD2History)).all()
        assert len(history) == 40
        assert all(row.version == 1 and row.is_current for row in history)


def test_batch_import_rejects_colliding_source_identities(db, tmp_path, item):
    with Session(db) as session:
        source = Source(name="theapolis.de")
        people = [Person(name="First"), Person(name="Second")]
        session.add_all([source, *people])
        session.flush()
        session.add_all(
            [
                EntitySource(
                    source_id=source.id,
                    entity_type=EntityType.PERSON,
                    entity_id=person.id,
                    external_id="directors:9",
                )
                for person in people
            ]
        )
        session.commit()
    path = tmp_path / "collision.json"
    path.write_text(json.dumps({"items": [item]}))
    with pytest.raises(ValueError, match="Theapolis source identity collision"):
        theapolis.import_all(path, dach_only=False)
    with Session(db) as session:
        assert {person.name for person in session.exec(select(Person))} == {"First", "Second"}
        assert not session.exec(select(Production)).all()
        assert not session.exec(select(SourceRecord).where(SourceRecord.entity_type == EntityType.PERSON)).all()


def test_newer_unchanged_observations_advance_freshness_without_domain_writes(db, tmp_path, item):
    path = tmp_path / "freshness.json"
    path.write_text(json.dumps({"items": [item], "captured_at": "2026-01-01T12:00:00Z"}))
    result = theapolis.import_all(path, dach_only=False)
    writes = []

    def record_write(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(("INSERT ", "UPDATE ", "DELETE ")):
            writes.append(statement)

    event.listen(db, "before_cursor_execute", record_write)
    try:
        assert theapolis.import_all(path, dach_only=False) == result
        assert writes == []
        path.write_text(json.dumps({"items": [item], "captured_at": "2026-03-01T12:00:00Z"}))
        assert theapolis.import_all(path, dach_only=False) == result
        assert writes and all(
            statement.startswith(("UPDATE sourcerecord ", "UPDATE entitysource ")) for statement in writes
        )
        writes.clear()
        item["directors"][0]["profile"] = {"birthDate": "1980"}
        path.write_text(json.dumps({"items": [item], "captured_at": "2026-02-01T12:00:00Z"}))
        assert theapolis.import_all(path, dach_only=False) == result
        assert writes == []
    finally:
        event.remove(db, "before_cursor_execute", record_write)
    with Session(db) as session:
        assert all(person.born is None for person in session.exec(select(Person)))
        assert all(
            row.captured_at is not None and row.captured_at.month == 3 for row in session.exec(select(SourceRecord))
        )


def test_newer_export_adds_only_delta_preserving_missing_records_and_conflicts(db, tmp_path, item):
    path = tmp_path / "delta.json"
    path.write_text(
        json.dumps(
            {
                "items": [item, {**item, "id": 124}],
                "captured_at": "2026-01-01T12:00:00Z",
            }
        )
    )
    result = theapolis.import_all(path, dach_only=False)
    changed = copy.deepcopy(item)
    changed["name"] = "Source title change"
    changed["directors"][0]["profile"] = {"birthDate": "1980"}
    changed["creators"].append({"id": 11, "name": "Other Alex"})
    path.write_text(
        json.dumps(
            {
                "items": [changed, {**item, "id": 125}],
                "captured_at": "2026-02-01T12:00:00Z",
            }
        )
    )
    again = theapolis.import_all(path, dach_only=False)
    assert len(again["person"]) == 3
    assert set(again["production"]) & set(result["production"]) == {result["production"][0]}
    with Session(db) as session:
        assert len(session.exec(select(Production)).all()) == 3
        assert session.get(Production, uuid.UUID(result["production"][1])) is not None
        production = session.get(Production, uuid.UUID(result["production"][0]))
        assert production is not None and production.name == item["name"]
        assert len(production.memberships) == 4
        assert production.text is not None and len(production.text.contributions) == 3
        director = session.exec(select(SourceRecord).where(SourceRecord.external_id == "directors:9")).one()
        history = session.exec(
            select(SCD2History)
            .where(SCD2History.entity_name == "person", SCD2History.entity_id == str(director.entity_id))
            .order_by(col(SCD2History.version))
        ).all()
        assert not history[-2].is_current and history[-2].valid_to is not None
        assert history[-1].is_current and history[-1].snapshot["born_precision"] == "year"
        count = len(session.exec(select(SCD2History)).all())
    assert theapolis.import_all(path, dach_only=False) == again
    with Session(db) as session:
        assert len(session.exec(select(SCD2History)).all()) == count


@pytest.mark.parametrize("size", [50, 200, 600])
def test_import_queries_scale_with_batches_not_records(db, tmp_path, item, size):
    items = [
        {
            **item,
            "id": identity,
            "directors": [{"id": identity, "name": f"Director {identity}"}],
            "creators": [{"id": identity, "name": f"Author {identity}"}],
        }
        for identity in range(size)
    ]
    path = tmp_path / "bounded.json"
    path.write_text(json.dumps({"items": items}))
    statements = []

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db, "before_cursor_execute", record_statement)
    try:
        result = theapolis.import_all(path, dach_only=False)
        assert len(result["production"]) == size
        assert sum(statement.startswith("SELECT ") for statement in statements) < 100
        assert len(statements) < 300
        statements.clear()
        assert theapolis.import_all(path, dach_only=False) == result
        assert sum(statement.startswith("SELECT ") for statement in statements) < 100
        assert len(statements) < 200
        assert not any(statement.startswith(("INSERT ", "UPDATE ", "DELETE ")) for statement in statements)
    finally:
        event.remove(db, "before_cursor_execute", record_statement)


def test_shared_profile_urls_do_not_override_scoped_identity_during_merge(db, monkeypatch, tmp_path, item):
    for person in [*item["directors"], *item["creators"]]:
        person["profile"] = {"url": f"{theapolis.SITE}/de/profil/shared"}
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"items": [item]}))
    with monkeypatch.context() as patch:
        patch.setattr(theapolis, "_merge_people_by_name", lambda *args: {})
        result = theapolis.import_all(path, dach_only=False)
    preview = preview_entity_merge("person", result["person"][:2])
    merge_entities(
        "person", result["person"][:2], target_id=preview["target_id"], revision=preview["revision"], actor_id="test"
    )
    assert len(theapolis.import_all(path, dach_only=False)["person"]) == 2


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
        assert org_id is not None
        credits = [{"case_id": str(persons[key].id), "role": role} for key, role, *_ in raw_credits]
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
    reimport = theapolis.import_all(path, dach_only=False)
    assert {kind: set(ids) for kind, ids in reimport.items() if kind != "person"} == {
        kind: set(ids) for kind, ids in result.items() if kind != "person"
    }
    assert len(reimport["person"]) == 2


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
    result = theapolis.import_all(path, dach_only=False)
    assert len(result["person"]) == 2
    with Session(db) as session:
        qualified = session.exec(
            select(SourceRecord).where(SourceRecord.source_id == source_id, SourceRecord.record_key == "id:directors:9")
        ).one()
        assert str(qualified.entity_id) in result["person"]


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
    result = theapolis.import_all(path, dach_only=False)
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
