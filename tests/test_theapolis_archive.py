import asyncio
import copy
import json
from time import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.ingestion import theapolis
from backend.ingestion.theapolis_archive import open_archive
from backend.models import (
    Organization,
    Person,
    Production,
    ProductionMembership,
    TheapolisImportCheckpoint,
    TheapolisProfileCache,
)


def production(identity, *, name="Hamlet", country="DE", organization=42) -> dict:
    return {
        "id": identity,
        "name": name,
        "unknown": None,
        "organization": {
            "id": organization,
            "name": "Theater",
            "location": {"country": country},
            "description": "<p>Profil</p>",
        },
        "directors": [{"id": 5, "name": "Alex", "profile": {"description": "<p>Bio</p>"}}],
    }


def write_run(archive, rows, *, partial=False):
    run = archive.start("test", 1, len(rows), partial=partial)
    archive.write_page(run, 1, {"items": rows}, ready=True)
    archive.seal(run)
    return run


def test_archive_dedup_roundtrip_delta_and_scope(tmp_path):
    with open_archive(tmp_path / "archive.sqlite3", write=True) as archive:
        rows = [production(1), production(2, organization=43)]
        first = write_run(archive, rows)
        assert list(archive.items(first)) == rows
        before = archive.db.execute("select count(*) from body").fetchone()[0]
        second = write_run(archive, list(reversed(rows)))
        assert archive.db.execute("select count(*) from body").fetchone()[0] == before
        assert list(archive.items(second, since=first)) == []
        rows.append(production(3))
        third = write_run(archive, rows)
        assert list(archive.items(third, since=second)) == rows[2:]
        assert list(archive.items(third, since=second, countries=tuple(theapolis.DACH_COUNTRIES))) == [rows[0], rows[2]]
        changed = copy.deepcopy(rows)
        del changed[0]["unknown"]
        changed[0]["organization"]["description"] = "Another observation, same ID"
        fourth = write_run(archive, changed)
        assert list(archive.items(fourth, since=third)) == changed[:1]
        assert list(archive.items(fourth)) == changed
        write_run(archive, [production(4)], partial=True)
        assert archive.latest() == fourth


def test_archive_download_resumes_only_incomplete_production_pages(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite3"
    requested = []
    fail = True

    async def fetch(_session, url):
        await asyncio.sleep(0)
        parsed = urlsplit(url)
        page = int(parse_qs(parsed.query)["page"][0])
        if parsed.path.endswith("participations"):
            return json.dumps(
                {
                    "success": True,
                    "data": {
                        "currentPageNumber": 1,
                        "totalCount": 0,
                        "numItemsPerPage": 1000,
                        "items": [],
                    },
                }
            ).encode()
        if parsed.path.endswith("organizations"):
            return json.dumps({"success": True, "data": {"totalCount": 0, "numItemsPerPage": 1, "items": []}}).encode()
        requested.append(page)
        if page == 2 and fail:
            raise RuntimeError("interrupted")
        return json.dumps(
            {"success": True, "data": {"totalCount": 2, "numItemsPerPage": 1, "items": [production(page)]}}
        ).encode()

    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(theapolis, "_fetch", fetch)
    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)
    with pytest.raises(RuntimeError, match="interrupted"):
        asyncio.run(theapolis.download_archive(path, concurrency=1))
    with open_archive(path) as archive:
        assert archive.latest() is None
    fail = False
    run = asyncio.run(theapolis.download_archive(path, concurrency=1))
    assert requested == [1, 2, 2]
    with open_archive(path) as archive:
        assert archive.latest() == run
        assert [x["id"] for x in archive.items(run)] == [1, 2]
    asyncio.run(theapolis.download_archive(path, concurrency=1))
    lookup.assert_awaited_once()


def test_api_credits_backfill_resumes_and_skips_fresh_productions(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite3"
    requested = []
    failing = True
    with open_archive(path, write=True) as archive:
        write_run(archive, [production(1), production(2)])

    async def fetch(_session, url):
        await asyncio.sleep(0)
        if urlsplit(url).path == "/api/de/profiles/888":
            return json.dumps({"success": True, "data": {"id": 888, "name": "Performer"}}).encode()
        identity = urlsplit(url).path.split("/")[-2]
        requested.append(identity)
        if identity == "2" and failing:
            raise RuntimeError("interrupted")
        return json.dumps(
            {
                "success": True,
                "data": {
                    "currentPageNumber": 1,
                    "totalCount": 1,
                    "numItemsPerPage": 1000,
                    "items": [
                        {
                            "id": int(identity) + 100,
                            "name": "Performer",
                            "profile": {"id": 888},
                            "occupation": {"title": "Schauspiel (weibliche Rollen)"},
                            "roleName": "Lady Macbeth",
                        }
                    ],
                },
            }
        ).encode()

    monkeypatch.setattr(theapolis, "_fetch", fetch)
    with pytest.raises(RuntimeError, match="interrupted"):
        asyncio.run(theapolis.download_credits(path, concurrency=1))
    failing = False
    run = asyncio.run(theapolis.download_credits(path, concurrency=1))
    assert requested == ["1", "2", "2"]
    with open_archive(path) as archive:
        rows = list(archive.items(run))
        assert len(rows) == 2
        assert rows[0]["theapolis_detail"]["credits"][0]["person_id"] == "profile:888"
        _, people, productions, _ = theapolis._prepare_import({"items": rows})
        assert "detail:profile:888" in people
        assert ("detail:profile:888", "actor", "Lady Macbeth") in productions[0][3]
    newer = asyncio.run(theapolis.download_credits(path, concurrency=1))
    assert requested == ["1", "2", "2"]
    with open_archive(path) as archive:
        assert list(archive.items(newer, since=run)) == []


def test_api_credit_writes_are_batched_and_tail_is_flushed(tmp_path, monkeypatch):
    count = 35

    async def participations(_session, identity):
        await asyncio.sleep(0)
        return {
            "production_id": identity,
            "theapolis_detail_status": "confirmed_empty",
            "theapolis_detail": {"credits": []},
        }

    monkeypatch.setattr(theapolis, "_participations", participations)
    with open_archive(tmp_path / "archive.sqlite3", write=True) as archive:
        write_run(archive, [production(identity) for identity in range(count)])
        run = archive.begin_details()
        statements = []
        archive.db.set_trace_callback(statements.append)

        async def download():
            async with theapolis._session(32) as session:
                await theapolis._download_participations(archive, session, run, 32)

        asyncio.run(download())
        assert statements.count("COMMIT") == 3  # Cache attachment, full batch, final partial batch.
        assert len(archive.cached("participations-v2", 0)) == count
        assert not list(archive.detail_targets(run, 0))
        archive.seal(run)
        assert len(list(archive.items(run))) == count


def test_api_participations_pagination_preserves_crew_and_rejects_incomplete(monkeypatch):
    requested = []
    broken = False

    async def fetch(_session, url):
        await asyncio.sleep(0)
        number = int(parse_qs(urlsplit(url).query)["page"][0])
        requested.append(number)
        rows = [
            {
                "id": 100 + number,
                "name": "Musician" if number == 1 else "Costume assistant",
                "profile": {"id": 800 + number},
                "occupation": {"title": "Bühnenmusik" if number == 1 else "Kostümbild"},
                "roleName": "",
                "assistant": number == 2,
                "description": "Additional source detail",
            }
        ]
        return json.dumps(
            {
                "success": True,
                "data": {
                    "currentPageNumber": number,
                    "numItemsPerPage": 1,
                    "totalCount": 2,
                    "items": [] if broken else rows,
                },
            }
        ).encode()

    monkeypatch.setattr(theapolis, "_fetch", fetch)

    async def run():
        async with theapolis._session(1) as session:
            return await theapolis._participations(session, "123")

    entry = asyncio.run(run())
    assert requested == [1, 2]
    assert entry["theapolis_detail"]["credits"][1]["role"] == "Assistenz Kostümbild"
    assert entry["theapolis_detail"]["api_pages"][0]["data"]["items"][0]["description"] == "Additional source detail"
    broken = True
    with pytest.raises(ValueError, match="Incomplete"):
        asyncio.run(run())


def test_profile_main_name_is_cached_once_and_attached_to_credits(tmp_path, monkeypatch):
    requested = []

    async def participations(_session, identity):
        await asyncio.sleep(0)
        return {
            "production_id": identity,
            "theapolis_detail_status": "parsed",
            "theapolis_detail": {
                "credits": [{"person_id": "profile:157005", "name": "Bernhard Niechotz", "role": "Spiel"}]
            },
        }

    async def fetch(_session, url):
        requested.append(url)
        await asyncio.sleep(0)
        return json.dumps({"success": True, "data": {"id": 157005, "name": "Bernhard Bruchhardt"}}).encode()

    monkeypatch.setattr(theapolis, "_participations", participations)
    monkeypatch.setattr(theapolis, "_fetch", fetch)
    with open_archive(tmp_path / "archive.sqlite3", write=True) as archive:
        write_run(archive, [production(1), production(2)])

        async def download(run):
            async with theapolis._session(2) as session:
                await theapolis._download_participations(archive, session, run, 2)

        run = archive.begin_details()
        asyncio.run(download(run))
        archive.seal(run)
        for item in archive.items(run):
            credit = item["theapolis_detail"]["credits"][0]
            assert credit["name"] == "Bernhard Niechotz"
            assert credit["profile_name"] == "Bernhard Bruchhardt"
        archive.db.execute("delete from cache where kind = 'participations-v2'")
        archive.db.commit()
        run = archive.begin_details()
        asyncio.run(download(run))
        assert requested == ["https://www.theapolis.de/api/de/profiles/157005"]


def test_archive_reuses_positive_and_negative_legacy_cache(tmp_path, monkeypatch):
    profile = {"description": "Bio", "slug": "person"}
    captured_at = time() - 3 * 86400
    monkeypatch.setattr(
        theapolis,
        "_read_profile_cache",
        lambda: {
            name: TheapolisProfileCache(name=name, profile=body, captured_at=captured_at)
            for name, body in {"Matched": profile, "No match": None, "Incomplete": {"slug": "old"}}.items()
        },
    )
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(theapolis, "_profile_for_name", lookup)

    async def run(archive, *, reuse):
        async with theapolis._session(1) as session:
            return await theapolis._archive_profiles(
                archive, session, {"Matched", "No match", "Incomplete"}, 1, reuse_profile_cache=reuse
            )

    with open_archive(tmp_path / "archive.sqlite3", write=True) as archive:
        assert asyncio.run(run(archive, reuse=True)) == {"Matched": profile}
        assert asyncio.run(run(archive, reuse=False)) == {"Matched": profile}
        assert archive.cached("no_profile", captured_at - 1) == {"No match": None, "Incomplete": None}
    lookup.assert_awaited_once()
    assert lookup.await_args is not None and lookup.await_args.args[-1] == "Incomplete"


def test_archive_bulk_resolves_only_unique_exact_names(tmp_path, monkeypatch):
    monkeypatch.setattr(theapolis, "PROFILE_DIRECTORY_THRESHOLD", 2)
    directory = {
        1: [{"slug": "matched", "name": " Matched "}, {"slug": "duplicate-1", "name": "Ambiguous"}],
        2: [{"slug": "duplicate-2", "name": "Ambiguous"}, {"slug": "similar", "name": "Missing Jr."}],
    }

    async def fetch(_session, url):
        await asyncio.sleep(0)
        query = parse_qs(urlsplit(url).query)
        assert "search" not in query
        page = int(query["page"][0])
        return json.dumps(
            {
                "success": True,
                "data": {
                    "currentPageNumber": page,
                    "totalCount": 4,
                    "numItemsPerPage": 2,
                    "items": directory[page],
                },
            }
        ).encode()

    def profile_fetch(slug):
        return {"slug": slug, "description": None}

    lookup = AsyncMock(side_effect=lambda _session, slug: profile_fetch(slug))
    monkeypatch.setattr(theapolis, "_fetch", fetch)
    monkeypatch.setattr(theapolis, "_profile_for_slug", lookup)

    async def run(archive):
        async with theapolis._session(2) as session:
            return await theapolis._archive_profiles(
                archive, session, {"Matched", "Missing", "Ambiguous"}, 2, reuse_profile_cache=False
            )

    with open_archive(tmp_path / "archive.sqlite3", write=True) as archive:
        assert asyncio.run(run(archive)) == {"Matched": profile_fetch("matched")}
        assert asyncio.run(run(archive)) == {"Matched": profile_fetch("matched")}
    lookup.assert_awaited_once()
    assert lookup.await_args is not None and lookup.await_args.args[-1] == "matched"


def test_archive_import_checkpoint_skips_unchanged_and_retains_manual_description(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fixture-db.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    path = tmp_path / "archive.sqlite3"
    rows = [production(1), production(2, name="Macbeth")]
    with open_archive(path, write=True) as archive:
        first = write_run(archive, rows)
    assert len(theapolis.import_archive(path)["production"]) == 2
    with Session(engine) as session:
        person = session.exec(select(Person)).one()
        assert person.description == "Bio"
        person.description = "Edited biography"
        session.add(person)
        assert session.exec(select(Organization)).one().description == "Profil"
        session.commit()
    assert not any(theapolis.import_archive(path).values())
    with open_archive(path, write=True) as archive:
        rows[1]["description"] = "New description"
        run = write_run(archive, rows)
    assert run > first
    result = theapolis.import_archive(path)
    assert len(result["production"]) == 1
    with Session(engine) as session:
        assert session.exec(select(Person)).one().description == "Edited biography"
        assert {p.name: p.description for p in session.exec(select(Production))} == {
            "Hamlet": None,
            "Macbeth": "New description",
        }
        assert session.exec(select(TheapolisImportCheckpoint)).one().run == run
    feed = tmp_path / "credits.jsonl"
    feed.write_text(
        json.dumps(
            {
                "production_id": 1,
                "theapolis_detail_status": "parsed",
                "theapolis_detail": {
                    "credits": [
                        {"person_id": "p9", "name": "Performer", "role": "Spiel", "character": "Hamlet"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    detail_run = theapolis.archive_details(path, feed)
    with open_archive(path) as archive:
        assert [row["id"] for row in archive.items(detail_run, since=run)] == [1]
    assert len(theapolis.import_archive(path)["production"]) == 1
    assert not any(theapolis.import_archive(path).values())
    with Session(engine) as session:
        credit = session.exec(select(ProductionMembership).where(ProductionMembership.character_name == "Hamlet")).one()
        assert credit.role_name == "actor"
    with open_archive(path, write=True) as archive:
        refresh = archive.start("test", 1, len(rows), partial=False)
        archive.write_page(refresh, 1, {"items": rows}, ready=True)
        archive.seal(refresh, carry_details=True)
        assert list(archive.items(refresh, since=detail_run)) == []
    engine.dispose()


def test_person_header_displays_biography():
    app = AppTest.from_string(
        "from frontend.views.person import render_person_header\n"
        'render_person_header({"name": "Alex", "description": "**Biography**"})'
    ).run()
    assert not app.exception
    assert app.subheader[0].value == "Biografie"
    assert app.markdown[0].value == "**Biography**"
