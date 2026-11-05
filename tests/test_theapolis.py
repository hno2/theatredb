import asyncio
import copy
import datetime as dt
import json
import re
import uuid
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
from backend.models import EntitySource, Organization, Person, Production, ReconciliationCase, Source, Text
from backend.reconciliation import approve_case, find_candidates, preview_case
from frontend.views.organization import filter_productions, premiere_label, production_dates


@pytest.fixture
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
    ("raw", "date", "year"),
    [
        ({"premierDate": "2026-10-03"}, "2026-10-03T00:00:00+00:00", 2026),
        ({"premierDate": "3.10.2026"}, "2026-10-03T00:00:00+00:00", 2026),
        ({"premierDate": "2026-10-03T19:00:00+02:00"}, "2026-10-03T19:00:00+02:00", 2026),
        ({"premierDate": "2026"}, None, 2026),
        ({"premierYear": 2026}, None, 2026),
        ({"year": "2026"}, None, 2026),
        ({}, None, None),
    ],
)
def test_premiere_precision(raw, date, year):
    fields = theapolis.premiere_fields(raw)
    assert fields == {"premiere": date, "premiere_year": year}
    Production.model_validate({"name": "Test", **fields})


@pytest.mark.parametrize(
    "raw",
    [
        {"premierDate": "2026-02-31"},
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
        pytest.fail("JSON export must not connect to the database")

    monkeypatch.setattr(theapolis, "init_db", reject_database)
    monkeypatch.setattr(theapolis, "get_session", reject_database)

    async def fetch(session, url):
        nonlocal active, peak
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        requested.append(page)
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1
        record = {**item, "id": page}
        if page == 2:
            record["premierDate"] = None
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
    assert validate_records(data) == data
    Draft202012Validator(staging_schema(), format_checker=FormatChecker()).validate(data)
    assert source["partial"] is False
    assert dt.datetime.fromisoformat(source["captured_at"]).tzinfo
    assert data[0]["title"] == "Hamlet"
    assert "id" not in data[0]
    assert "organization" not in data[0]
    assert data[0]["memberships"][0]["role"] == "director"
    assert data[1]["premiere"] is None
    assert data[1]["premiere_year"] == 2026
    requested.clear()
    asyncio.run(theapolis.download(path, max_pages=1))
    assert requested == [1]
    assert json.loads(path.with_suffix(".source.json").read_text())["partial"] is True


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


def test_review_import_is_idempotent_and_approval_preserves_source_only_urls(db, tmp_path, item):
    path = tmp_path / "export.json"
    year_only = {**copy.deepcopy(item), "id": 124, "premierDate": None}
    path.write_text(json.dumps({"items": [item, year_only], "captured_at": "2026-11-05T12:00:00Z"}))
    result = theapolis.import_all(path)
    assert result == theapolis.import_all(path)
    assert {key: len(value) for key, value in result.items()} == {
        "organization": 1,
        "person": 2,
        "text": 2,
        "production": 2,
    }
    with Session(db) as session:
        for model in (Organization, Person, Production, Text):
            assert not session.exec(select(model)).all()
        persons = session.exec(select(ReconciliationCase).where(ReconciliationCase.entity_type == "person")).all()
        assert next(p.incoming for p in persons if p.incoming["name"] == "Alex")["type"] == ["director", "author"]
    for kind in ("organization", "person", "text", "production"):
        for identity in result[kind]:
            preview = preview_case(identity)
            assert not preview["blocked"]
            approve_case(identity, revision=preview["revision"], allow_create=True, actor_id="test")
    with Session(db) as session:
        productions = session.exec(select(Production).order_by(col(Production.premiere))).all()
        assert len(productions) == 2
        assert productions[0].premiere is None and productions[0].premiere_year == 2026
        assert productions[1].premiere is not None
        assert productions[1].premiere.date() == dt.date(2026, 10, 3)
        assert all(p.url is None for p in productions)
        organization = session.exec(select(Organization)).one()
        assert organization.url is None
        assert organization.lat == pytest.approx(52.5) and organization.lon == pytest.approx(13.4)
        assert any("produktionen/123" in (s.external_url or "") for s in session.exec(select(EntitySource)).all())


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
    path = tmp_path / "productions.json"
    path.write_text(json.dumps(theapolis.staged_records([item])))
    path.with_suffix(".source.json").write_text(json.dumps({"items": [item]}))
    result = theapolis.import_all(path)
    assert len(result["production"]) == 1
    with Session(db) as session:
        case = session.get(ReconciliationCase, uuid.UUID(result["production"][0]))
        assert case is not None
        assert case.context["external_id"] == str(item["id"])
        assert "url" not in case.incoming


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
    assert theapolis.import_all() == theapolis.import_all(second)
    assert theapolis.import_all() != theapolis.import_all(limited)


def test_default_import_does_not_select_limited_snapshot(db, monkeypatch, tmp_path, item):
    monkeypatch.setattr(theapolis, "DEFAULT_EXPORT_DIR", tmp_path)
    limited = tmp_path / f"20261105_120000_000001_{theapolis._export_scope(1)}_productions.json"
    limited.write_text(json.dumps(theapolis.staged_records([item])))
    with pytest.raises(ValueError, match="No full Theapolis snapshot"):
        theapolis.import_all()


def test_year_matching_and_display_do_not_invent_date(db):
    with Session(db) as session:
        session.add(Production(name="Hamlet", premiere_year=2026))
        session.commit()
    candidate = find_candidates("production", {"name": "Hamlet", "premiere": "2026-10-03"})[0]
    assert "Gleiches Premierenjahr" in candidate["evidence"]
    candidate = find_candidates("production", {"name": "Hamlet", "premiere_year": 2025})[0]
    assert "Abweichende Premiere" in candidate["conflicts"]
    record = {"name": "Hamlet", "premiere_year": 2026}
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
            Production(
                name="Hamlet",
                organization_id=organization.id,
                premiere_year=2026,
                url="https://www.theapolis.de/de/produktionen/123",
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
        case = session.get(ReconciliationCase, uuid.UUID(result["production"][0]))
        assert case is not None
        assert all(dep["field"] != "organization_id" for dep in case.context["dependencies"])


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
