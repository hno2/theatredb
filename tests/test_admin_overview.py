import datetime as dt
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.models import CrawlRun, EntityType, ImportRun, IngestionRun, Organization, ReconciliationCase, Source
from backend.production_import import OPERATION
from frontend import auth

APP = Path(__file__).resolve().parents[1] / "frontend" / "app.py"
PAGE = "management/overview.py"
NOW = dt.datetime(2025, 2, 1, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def admin_app(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'admin.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    monkeypatch.setattr(auth, "current_user", lambda: auth.Principal(UUID(int=1), "Admin", is_admin=True))
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not app.exception
    yield app, engine
    engine.dispose()


def test_empty_overview(admin_app):
    app, _ = admin_app
    app.switch_page(PAGE).run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["0", "0", "0", "0"]
    assert any("Noch kein erfasster Import" in message.value for message in app.info)
    assert any("Noch kein Crawl" in message.value for message in app.info)


def test_latest_activity_and_open_review_counts(admin_app):
    app, engine = admin_app
    with Session(engine) as session:
        theatre = Organization(
            name="Bühne",
            update_scope="productions",
            update_reason="Programm prüfen",
            update_flagged_at=NOW,
            update_flagged_by="editor",
        )
        source = Source(name="Quelle")
        theapolis = Source(name="theapolis.de")
        session.add_all([theatre, source, theapolis])
        session.flush()
        session.add_all(
            [
                ImportRun(
                    organization_id=theatre.id,
                    source_id=source.id,
                    imported_at=NOW - dt.timedelta(days=1),
                    total=99,
                ),
                ImportRun(
                    organization_id=theatre.id,
                    source_id=source.id,
                    imported_at=NOW,
                    captured_at=NOW - dt.timedelta(days=2),
                    file_name="aktuell.json",
                    total=5,
                    created=2,
                    merged=1,
                    review=1,
                    unchanged=1,
                ),
                CrawlRun(source_id=source.id, definition={}, created_at=NOW, status="failed", error="Fehlerdetails"),
                *[
                    ReconciliationCase(
                        key=f"case-{index}",
                        entity_type=EntityType.PRODUCTION,
                        operation=operation,
                        source_id=source_id,
                        status=status,
                        reason="Prüfen",
                    )
                    for index, (operation, source_id, status) in enumerate(
                        [
                            (OPERATION, source.id, "pending"),
                            ("import", source.id, "pending"),
                            ("merge", None, "pending"),
                            ("link_text", source.id, "pending"),
                            ("text_lineage", theapolis.id, "pending"),
                            ("import", theapolis.id, "pending"),
                            (OPERATION, source.id, "approved"),
                        ]
                    )
                ],
            ]
        )
        session.commit()

    app.switch_page(PAGE).run()
    assert not app.exception
    assert [(item.label, item.value) for item in app.metric] == [
        ("Ergänzt", "1"),
        ("Neu angelegt", "2"),
        ("Zur Prüfung", "1"),
        ("Unverändert", "1"),
        ("Produktionskonflikte", "1"),
        ("Andere Prüffälle", "2"),
        ("Text-Prüffälle", "2"),
        ("Aktualisierungen", "1"),
    ]
    assert any("aktuell.json" in item.value and "30.01.2025" in item.value for item in app.caption)
    assert any("Theapolis-Prüffälle" in item.value for item in app.caption)
    assert any("Fehlerdetails" in item.value for item in app.error)
    assert any("Bühne · Programm prüfen" in item.value for item in app.text)
    assert any("Ältester offener Prüffall" in item.value for item in app.caption)


@pytest.mark.parametrize("latest_kind", ["theatre", "publisher", "theapolis", "crawl"])
def test_latest_import_across_all_sources(admin_app, latest_kind):
    app, engine = admin_app
    with Session(engine) as session:
        theatre = Organization(name="Bühne")
        source = Source(name="Quelle")
        session.add_all([theatre, source])
        session.flush()
        times = {
            kind: NOW + dt.timedelta(hours=1 if kind == latest_kind else 0)
            for kind in ("theatre", "publisher", "theapolis", "crawl")
        }
        session.add_all(
            [
                ImportRun(organization_id=theatre.id, source_id=source.id, imported_at=times["theatre"]),
                IngestionRun(
                    source_name="Rowohlt",
                    kind="publisher",
                    imported_at=times["publisher"],
                    stats={"observations": 7, "review": 2},
                ),
                IngestionRun(
                    source_name="theapolis.de",
                    kind="theapolis",
                    imported_at=times["theapolis"],
                    stats={"production": 8},
                ),
                CrawlRun(
                    source_id=source.id,
                    definition={},
                    status="succeeded",
                    created_at=NOW,
                    finished_at=times["crawl"],
                    stats={"records": 9, "changed": 1, "unchanged": 8},
                ),
                CrawlRun(
                    source_id=source.id,
                    definition={},
                    status="failed",
                    created_at=NOW + dt.timedelta(hours=2),
                    finished_at=NOW + dt.timedelta(hours=2),
                    error="Crawl failed",
                ),
            ]
        )
        session.commit()
    app.switch_page(PAGE).run()
    assert not app.exception
    labels = {
        "theatre": "Theater-Import",
        "publisher": "Verlagsimport",
        "theapolis": "Theapolis-Import",
        "crawl": "Crawl-Import",
    }
    assert any(labels[latest_kind] in item.value for item in app.markdown)
    assert any("Crawl failed" in item.value for item in app.error)
    if latest_kind == "publisher":
        assert any("Beobachtungen: 7" in item.value and "Zur Prüfung: 2" in item.value for item in app.caption)
    if latest_kind == "crawl":
        assert any("Datensätze: 9" in item.value and "Geändert: 1" in item.value for item in app.caption)


def test_failed_sources_are_reported_even_if_latest_crawl_succeeded(admin_app):
    app, engine = admin_app
    with Session(engine) as session:
        failed, recovered, latest = [Source(name=name) for name in ("Fehler", "Erholt", "Neu")]
        session.add_all([failed, recovered, latest])
        session.flush()
        session.add_all(
            [
                CrawlRun(source_id=failed.id, definition={}, status="failed", created_at=NOW, error="Timeout"),
                CrawlRun(source_id=recovered.id, definition={}, status="failed", created_at=NOW),
                CrawlRun(
                    source_id=recovered.id,
                    definition={},
                    status="succeeded",
                    created_at=NOW + dt.timedelta(hours=1),
                    finished_at=NOW + dt.timedelta(hours=1),
                ),
                CrawlRun(source_id=latest.id, definition={}, status="running", created_at=NOW + dt.timedelta(hours=2)),
            ]
        )
        session.commit()
    app.switch_page(PAGE).run()
    assert not app.exception
    assert any("Aktive Crawls: 1" in item.value for item in app.caption)
    assert any("fehlgeschlagen: 1" in item.value for item in app.warning)
    assert any("Fehler · Timeout" in item.value for item in app.text)
    assert not any("Erholt" in item.value for item in app.text)


@pytest.mark.parametrize("user", [None, auth.Principal(UUID(int=2), "Reader", is_admin=False)])
def test_overview_not_registered_for_non_admins(admin_app, monkeypatch, user):
    app, _ = admin_app
    monkeypatch.setattr(auth, "current_user", lambda: user)
    app.run()
    assert not app.exception
    with pytest.raises(ValueError):
        app.switch_page(PAGE)


def test_ingestion_history_migration_matches_models(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    tables = [table for table in SQLModel.metadata.sorted_tables if table.name != "ingestionrun"]
    SQLModel.metadata.create_all(engine, tables=tables)
    with engine.begin() as connection:
        config = user_management.alembic_config(connection)
        command.stamp(config, "2fbde2f2ab45")
        command.upgrade(config, "head")
        assert "ingestionrun" in inspect(connection).get_table_names()
        command.check(config)
        command.downgrade(config, "2fbde2f2ab45")
        assert "ingestionrun" not in inspect(connection).get_table_names()
    engine.dispose()
