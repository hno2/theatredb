import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import ColumnElement, Engine, event
from sqlmodel import Session, SQLModel, create_engine, select, update
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.import_status import scrape_overview, theatre_status
from backend.ingestion.staged_to_db import import_staged_productions
from backend.models import (
    EntitySource,
    ImportRun,
    Organization,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Source,
    SourceRecord,
    Text,
    TextContribution,
)
from backend.production_import import (
    TITLE_THRESHOLD,
    ingest_records,
    list_production_cases,
    production_review,
    reject_production_case,
    resolve_production_case,
    scrape_scope,
    title_similarity,
)
from backend.texts import add_title, resolve_staged_text
from frontend.management import reconciliation as review_ui

CAPTURE = dt.datetime(2026, 6, 7, tzinfo=dt.UTC)
REVIEW_APP = Path(__file__).resolve().parents[1] / "frontend" / "management" / "reconciliation.py"


def test_shared_staging_year_survives_upload(db, theatre) -> None:
    record = {"title": "Year only", "url": "https://theatre.example/year-only", "premiere_year": 2026}
    upload(theatre, record)
    production = rows(db, Production)[0]
    assert production.premiere == dt.datetime(2026, 1, 1)
    assert production.premiere_precision == "year"


@pytest.fixture
def db(monkeypatch, tmp_path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'import.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


def save[*Entities](db, *entities: *Entities) -> tuple[*Entities]:
    with Session(db) as session:
        for entity in entities:
            session.add(entity)
            session.flush()
        session.commit()
        for entity in entities:
            session.refresh(entity)
    return entities


def rows(db, model, *where: ColumnElement[bool] | bool) -> list:
    with Session(db) as session:
        return list(session.exec(select(model).where(*where)).all())


@pytest.fixture
def theatre(db) -> Organization:
    return save(db, Organization(name="Staatstheater", city="Karlsruhe"))[0]


def upload(theatre, *records: dict, source="theatre.example", captured_at=CAPTURE):
    return ingest_records(
        list(records), organization_id=theatre.id, source_name=source, actor_id="test", captured_at=captured_at
    )


HAMLET = {
    "title": "Hamlet",
    "url": "https://theatre.example/p/hamlet",
    "description": "Official text",
    "premiere": "2026-02-14",
    "tags": ["drama"],
    "author": "William Shakespeare",
    "memberships": [
        {"name": "Director One", "type": "director", "role": "Regie"},
        {"name": "Actor One", "type": "actor", "role": "Hamlet"},
    ],
}


def existing_hamlet(db, theatre) -> Production:
    other = save(db, Source(name="theapolis"))[0]
    director, author = save(db, Person(name="Director One"), Person(name="William Shakespeare"))
    text = save(db, Text(title="Hamlet"))[0]
    save(db, TextContribution(text_id=text.id, person_id=author.id, role="author"))
    production = save(db, Production(name="Hamlet", organization_id=theatre.id, tags=["classic"], text_id=text.id))[0]
    save(
        db,
        ProductionMembership(production_id=production.id, person_id=director.id, role="director"),
        EntitySource(source_id=other.id, entity_type="production", entity_id=production.id, external_url="https://x"),
    )
    return production


def test_title_similarity_threshold() -> None:
    assert title_similarity("Hamlet", "hamlet!") == 1
    assert title_similarity("Die Räuber", "Die Räuberin") >= TITLE_THRESHOLD
    assert title_similarity("Hamlet", "Hamlet: Prinz von Dänemark") >= TITLE_THRESHOLD
    assert title_similarity("Hamlet", "Macbeth") < TITLE_THRESHOLD
    assert title_similarity("Die", "Die Räuber") < TITLE_THRESHOLD


def test_same_title_without_conflict_is_merged_with_people_text_and_second_source(db, theatre) -> None:
    production = existing_hamlet(db, theatre)
    save(db, Person(name="Actor One"))
    assert upload(theatre, HAMLET)["merged"] == ["Hamlet"]
    merged = rows(db, Production)
    assert len(merged) == 1
    assert merged[0].description == "Official text"
    assert merged[0].premiere.date() == dt.date(2026, 2, 14)
    assert merged[0].tags == ["classic", "drama"]
    assert len(rows(db, EntitySource, EntitySource.entity_id == production.id)) == 2
    assert len(rows(db, Person)) == 3  # director, author and actor were all reused
    assert len(rows(db, Text)) == 1
    roles = {(m.role, m.character) for m in rows(db, ProductionMembership)}
    assert roles == {("director", None), ("actor", "Hamlet"), ("author", None)}
    assert not rows(db, ReconciliationCase)
    assert upload(theatre, HAMLET)["unchanged"] == ["Hamlet"]
    assert len(rows(db, ProductionMembership)) == 3


def test_legacy_null_lists_do_not_block_merge(db, theatre) -> None:
    save(db, Production(name="Hamlet", organization_id=theatre.id), Person(name="Director One"))
    with Session(db) as session:  # rows created before the list columns had defaults
        session.exec(update(Production).values({"tags": None, "critics": None}))
        session.exec(update(Person).values({"type": None, "urls": None}))
        session.commit()
    assert rows(db, Production)[0].tags is None
    record = {key: value for key, value in HAMLET.items() if key != "tags"}
    assert upload(theatre, record)["merged"] == ["Hamlet"]
    production = rows(db, Production)[0]
    assert production.description == "Official text"
    assert not production.tags
    assert rows(db, Person, Person.name == "Director One")[0].type == ["director"]


def test_unknown_title_is_created_and_reupload_is_idempotent(db, theatre) -> None:
    other = save(db, Organization(name="Other theatre"))[0]
    save(db, Production(name="Hamlet", organization_id=other.id))
    assert upload(theatre, HAMLET, {**HAMLET, "url": "https://theatre.example/p/2", "title": "Woyzeck"}) == {
        "merged": [],
        "unchanged": [],
        "created": ["Hamlet", "Woyzeck"],
        "review": [],
    }
    assert len(rows(db, Production, Production.organization_id == theatre.id)) == 2
    assert len(rows(db, Person)) == 3
    assert sorted(text.title for text in rows(db, Text)) == ["Hamlet", "Woyzeck"]
    assert len(rows(db, TextContribution)) == 2
    assert upload(theatre, HAMLET)["unchanged"] == ["Hamlet"]
    assert len(rows(db, Production)) == 3


def test_staged_text_metadata_imports_text_credits_and_keeps_production_adapters(db, theatre) -> None:
    record = {
        **HAMLET,
        "text": {
            "title": "Hamlet (German translation)",
            "original_title": "Hamlet",
            "original_language": "en",
            "language": "de",
            "authors": ["William Shakespeare"],
            "translators": ["Anna Translator"],
        },
        "memberships": [
            *HAMLET["memberships"],
            {"name": "Anna Translator", "type": "translator", "role": "translator"},
            {"name": "Adam Adapter", "type": "adapter", "role": "adapter"},
        ],
    }

    assert upload(theatre, record)["created"] == ["Hamlet"]
    text = rows(db, Text, Text.title == "Hamlet (German translation)")[0]
    assert text.title == "Hamlet (German translation)"
    assert text.languages == ["de"]
    people = {row.id: row.name for row in rows(db, Person)}
    contributions = {(people[row.person_id], row.role) for row in rows(db, TextContribution)}
    assert contributions == {("William Shakespeare", "author"), ("Anna Translator", "translator")}
    assert ("translator", None) in {(row.role, row.character) for row in rows(db, ProductionMembership)}
    assert ("adapter", None) in {(row.role, row.character) for row in rows(db, ProductionMembership)}
    assert upload(theatre, record)["unchanged"] == ["Hamlet"]
    assert len(rows(db, Text)) == 2


def test_ambiguous_staged_text_waits_for_link_review_with_production_context(db, theatre) -> None:
    author = save(db, Person(name="William Shakespeare"))[0]
    with Session(db) as session:
        for _ in range(2):
            text = Text(title="Hamlet")
            session.add(text)
            session.flush()
            add_title(session, text, "Hamlet")
            session.add(TextContribution(text_id=text.id, person_id=author.id, role="author"))
        session.commit()

    assert upload(theatre, HAMLET)["created"] == ["Hamlet"]
    production = rows(db, Production)[0]
    assert production.text_id is None
    case = next(row for row in rows(db, ReconciliationCase) if row.operation == "link_text")
    assert case.context["production_id"] == str(production.id)
    assert case.incoming["title"] == "Hamlet"
    assert upload(theatre, {**HAMLET, "premiere": "2027-02-14"})["created"] == ["Hamlet"]
    case = next(row for row in rows(db, ReconciliationCase) if row.operation == "link_text")
    assert set(case.context["production_ids"]) == {str(row.id) for row in rows(db, Production)}


@pytest.mark.parametrize("explicit_title", [False, True])
def test_author_only_upload_keeps_specific_translation_with_different_production_title(
    db, theatre, explicit_title
) -> None:
    with Session(db) as session:
        translated = resolve_staged_text(
            session,
            {
                "title": "German play title",
                "original_title": "Original play title",
                "original_language": "en",
                "language": "de",
                "authors": ["Original Author"],
                "translators": ["Known Translator"],
            },
        )
        assert isinstance(translated, Text)
        translated_id = translated.id
        production = Production(name="Theatre display title", organization_id=theatre.id, text_id=translated_id)
        session.add(production)
        session.commit()
        production_id = production.id
    metadata: dict = {
        "title": "Theatre display title",
        "url": "https://theatre.example/specific",
        "author": "Original Author",
    }
    if explicit_title:
        metadata["text"] = {"title": "German play title", "authors": ["Original Author"]}

    assert upload(theatre, metadata)["merged"] == ["Theatre display title"]
    assert upload(theatre, metadata)["unchanged"] == ["Theatre display title"]
    assert rows(db, Production, Production.id == production_id)[0].text_id == translated_id
    assert len(rows(db, Text)) == 2
    assert not rows(db, ReconciliationCase)


def test_translator_refinement_without_text_metadata_uses_current_text_title(db, theatre) -> None:
    with Session(db) as session:
        text = resolve_staged_text(session, {"title": "Play text title", "authors": ["Original Author"]})
        assert isinstance(text, Text)
        production = Production(name="Theatre display title", organization_id=theatre.id, text_id=text.id)
        session.add(production)
        session.commit()
        production_id = production.id
    metadata = {
        "title": "Theatre display title",
        "url": "https://theatre.example/refine",
        "author": "Original Author",
        "memberships": [{"name": "New Translator", "role": "translator", "type": "translator"}],
    }

    assert upload(theatre, metadata)["merged"] == ["Theatre display title"]
    translated_id = rows(db, Production, Production.id == production_id)[0].text_id
    assert rows(db, Text, Text.id == translated_id)[0].title == "Play text title"
    assert len(rows(db, Text)) == 2
    assert upload(theatre, metadata)["unchanged"] == ["Theatre display title"]
    assert rows(db, Production, Production.id == production_id)[0].text_id == translated_id
    assert not rows(db, ReconciliationCase)


@pytest.mark.parametrize("title", ["Hamlet", "Hamlet: Prinz von Dänemark"])
@pytest.mark.parametrize("precision", ["year", "day"])
def test_different_premiere_year_is_created_without_review(db, theatre, title, precision) -> None:
    original = save(
        db,
        Production(
            name=title,
            organization_id=theatre.id,
            premiere=dt.datetime(2025, 1, 1),
            premiere_precision=precision,
        ),
    )[0]
    record = {**HAMLET, "premiere": "2026", "premiere_precision": "year"}
    assert upload(theatre, record)["created"] == ["Hamlet"]
    assert len(rows(db, Production)) == 2
    assert rows(db, Production, Production.id == original.id)[0].premiere.year == 2025
    assert not rows(db, ReconciliationCase)
    assert upload(theatre, record)["unchanged"] == ["Hamlet"]
    assert len(rows(db, Production)) == 2


def test_reused_source_page_keeps_different_year_productions_separate(db, theatre) -> None:
    for year, outcome in [(2025, "created"), (2026, "created"), (2025, "unchanged")]:
        record = {**HAMLET, "premiere": f"{year}-02-14"}
        assert upload(theatre, record)[outcome] == ["Hamlet"]
    assert {row.premiere.year for row in rows(db, Production)} == {2025, 2026}
    assert len(rows(db, ProductionMembership)) == 6
    assert not rows(db, ReconciliationCase)


@pytest.mark.parametrize(
    ("existing_date", "incoming_date"),
    [(dt.datetime(2026, 1, 1), "2026-02-14"), (None, "2026-02-14"), (dt.datetime(2025, 1, 1), None)],
)
def test_same_or_unknown_year_still_checks_conflicts(db, theatre, existing_date, incoming_date) -> None:
    save(db, Production(name="Hamlet", organization_id=theatre.id, premiere=existing_date, description="Old"))
    assert upload(theatre, {**HAMLET, "premiere": incoming_date})["review"] == ["Hamlet"]
    assert len(rows(db, Production)) == 1
    assert len(list_production_cases()) == 1


def test_same_title_matches_current_year_instead_of_older_production(db, theatre) -> None:
    save(db, Production(name="Hamlet", organization_id=theatre.id, premiere=dt.datetime(2025, 2, 14)))
    current = save(db, Production(name="Hamlet", organization_id=theatre.id, premiere=dt.datetime(2026, 2, 14)))[0]
    assert upload(theatre, HAMLET)["merged"] == ["Hamlet"]
    assert rows(db, Production, Production.id == current.id)[0].description == HAMLET["description"]
    assert len(rows(db, Production)) == 2
    assert not rows(db, ReconciliationCase)


def test_conflict_is_reviewed_side_by_side_and_resolved_in_backend(db, theatre) -> None:
    production = existing_hamlet(db, theatre)
    with Session(db) as session:
        row = session.get(Production, production.id)
        assert row is not None
        row.description = "Old text"
        session.add(row)
        session.commit()
    assert upload(theatre, HAMLET)["review"] == ["Hamlet"]
    assert rows(db, Production)[0].description == "Old text"
    case = list_production_cases()[0]
    assert case["theatre"] == "Staatstheater"
    review = production_review(case["id"])
    assert review["target"]["id"] == str(production.id)
    assert review["target"]["label"] == "Hamlet · Staatstheater"
    assert review["incoming_label"] == "Hamlet · Premiere 14.02.2026 · Staatstheater"
    assert review["conflicts"] == ["description"]
    status = {row["field"]: row["status"] for row in review["fields"]}
    assert status["premiere"] == "new"
    assert status["tags"] == "extended"
    assert status["description"] == "conflict"
    people = {row["name"]: row["status"] for row in review["people"]}
    assert people == {"Director One": "same", "Actor One": "new_person", "William Shakespeare": "new_known"}
    with pytest.raises(ValueError, match="Data changed"):
        resolve_production_case(case["id"], revision="stale", actor_id="test")
    resolve_production_case(
        case["id"], revision=review["revision"], actor_id="test", choices={"description": "incoming"}
    )
    merged = rows(db, Production)
    assert len(merged) == 1
    assert merged[0].description == "Official text"
    assert len(rows(db, Person)) == 3
    assert len(rows(db, EntitySource, EntitySource.entity_id == production.id)) == 2
    assert not list_production_cases()


def test_conflict_keeps_existing_value_by_default(db, theatre) -> None:
    existing_hamlet(db, theatre)
    upload(theatre, {**HAMLET, "memberships": [{"name": "Someone Else", "role": "Regie"}]})
    case = list_production_cases()[0]
    review = production_review(case["id"])
    assert review["conflicts"] == ["director"]
    resolve_production_case(case["id"], revision=review["revision"], actor_id="test")
    directors = rows(db, ProductionMembership, ProductionMembership.role == "director")
    assert len(directors) == 1
    assert rows(db, Person, Person.id == directors[0].person_id)[0].name == "Director One"


def test_similar_title_can_be_created_or_merged_into_other_production_of_same_theatre(db, theatre) -> None:
    hamlet = save(db, Production(name="Hamlet!", organization_id=theatre.id))[0]
    other = save(db, Production(name="Ham", organization_id=theatre.id))[0]
    foreign = save(db, Production(name="Hamlet", organization_id=save(db, Organization(name="X"))[0].id))[0]
    upload(theatre, {**HAMLET, "title": "Hamlet (Neu)"})
    case = list_production_cases()[0]
    assert case["reason"].startswith("Ähnlicher Titel")
    review = production_review(case["id"])
    assert review["target"]["id"] == str(hamlet.id)
    assert {row["id"] for row in review["candidates"]} == {str(hamlet.id), str(other.id)}
    with pytest.raises(ValueError, match="same theatre"):
        production_review(case["id"], target_id=foreign.id)
    other_review = production_review(case["id"], target_id=other.id)
    assert other_review["target"]["label"] == "Ham · Staatstheater"
    new = production_review(case["id"], create=True)
    assert new["target"] is None
    created = resolve_production_case(case["id"], revision=new["revision"], actor_id="test", create=True)
    assert rows(db, Production, Production.id == created)[0].organization_id == theatre.id
    assert len(rows(db, Production, Production.organization_id == theatre.id)) == 3


def test_rejected_case_is_not_requeued(db, theatre) -> None:
    save(db, Production(name="Hamlet", organization_id=theatre.id, description="Other"))
    upload(theatre, HAMLET)
    reject_production_case(list_production_cases()[0]["id"], actor_id="test")
    assert upload(theatre, HAMLET)["unchanged"] == ["Hamlet"]
    assert not list_production_cases()


def test_theatre_is_required(db) -> None:
    with pytest.raises(ValueError, match="Theatre not found"):
        ingest_records([HAMLET], organization_id=Source().id, source_name="s", actor_id="t")
    assert not rows(db, Source)


@pytest.mark.parametrize("person_type", [None, "actor", ["actor"], "theatre_educator"])
def test_vermittlung_imports_as_educator_even_from_legacy_actor_snapshot(db, theatre, person_type) -> None:
    member = {"name": "Example Educator", "role": "Vermittlung"}
    if person_type is not None:
        member["type"] = person_type
    upload(
        theatre,
        {
            "title": "Education",
            "url": "https://theatre.example/p/education",
            "memberships": [member],
        },
    )
    person = rows(db, Person, Person.name == "Example Educator")[0]
    assert person.type == ["theatre_educator"]
    membership = rows(db, ProductionMembership, ProductionMembership.person_id == person.id)[0]
    assert membership.role == "theatre_educator"
    assert membership.character is None


def test_person_and_production_fields_survive_import(db, theatre) -> None:
    upload(
        theatre,
        {
            "title": "Current fields",
            "url": "https://theatre.example/p/current",
            "tags": ["drama"],
            "critics": ["https://reviews.example/current"],
            "memberships": [
                {
                    "name": "Multi-role Person",
                    "type": ["actor", "director"],
                    "role": "Regie",
                    "url": "https://theatre.example/people/1",
                    "urls": ["https://profile.example/1"],
                    "born": "1980-01-02",
                    "nationality": "Example",
                },
                {"name": "Actor", "type": "actor", "role": "source label", "character": "Hamlet"},
            ],
        },
    )
    person = rows(db, Person, Person.name == "Multi-role Person")[0]
    assert person.type == ["actor", "director"]
    assert person.born == dt.datetime(1980, 1, 2)
    assert person.born_precision == "day"
    assert person.urls == ["https://theatre.example/people/1", "https://profile.example/1"]
    assert rows(db, ProductionMembership, ProductionMembership.person_id == person.id)[0].role == "director"
    assert rows(db, ProductionMembership, ProductionMembership.role == "actor")[0].character == "Hamlet"
    production = rows(db, Production)[0]
    assert (production.tags, production.critics) == (["drama"], ["https://reviews.example/current"])
    # The person page URL identifies the person again, even with a changed spelling.
    upload(
        theatre,
        {
            "title": "Other",
            "url": "https://theatre.example/p/other",
            "memberships": [{"name": "Multi Role Person", "url": "https://theatre.example/people/1", "role": "Regie"}],
        },
    )
    assert len(rows(db, Person)) == 2


def test_invalid_upload_writes_nothing(db, theatre) -> None:
    with pytest.raises(ValueError):
        upload(
            theatre,
            {"title": "Valid", "url": "https://theatre.example/1"},
            {"title": "Bad", "url": "https://theatre.example/2", "memberships": [{"name": "P", "url": "javascript:x"}]},
        )
    assert not rows(db, Source)
    assert not rows(db, Production)


def test_cli_import_requires_existing_theatre_and_infers_capture_date(db, theatre, tmp_path) -> None:
    path = tmp_path / "20260607_productions.json"
    path.write_text('[{"title": "Work", "url": "https://theatre.example/1"}]')
    with pytest.raises(ValueError, match="exactly one theatre"):
        import_staged_productions(json_path=path, actor_id="test", organization_name="Unknown")
    assert import_staged_productions(json_path=path, actor_id="test", organization_name="staatstheater")["created"]
    with Session(db) as session:
        record = session.exec(select(SourceRecord)).one()
        assert record.captured_at is not None
        assert record.captured_at.date() == dt.date(2026, 6, 7)


def test_conflict_ui_shows_both_sides_and_merges(db, theatre) -> None:
    save(db, Production(name="Hamlet", organization_id=theatre.id, description="Old text"))
    upload(theatre, HAMLET)
    app = AppTest.from_file(str(REVIEW_APP)).run()
    assert not app.exception
    assert app.tabs[0].label == "Produktionskonflikte (1)"
    markdown = " ".join(element.value for element in app.markdown)
    assert "Hamlet · Staatstheater" in markdown
    assert "Hamlet · Premiere 14.02.2026 · Staatstheater" in markdown
    shown = " ".join(" ".join(element.options) for element in app.selectbox) + markdown
    assert str(theatre.id) not in shown
    radio = next(r for r in app.radio if (r.key or "").endswith("_description"))
    radio.set_value("incoming").run()
    next(button for button in app.button if button.label == "Zusammenführen").click().run()
    assert not app.exception
    assert rows(db, Production)[0].description == "Official text"
    assert len(rows(db, Production)) == 1
    assert app.tabs[0].label == "Produktionskonflikte (0)"


def test_review_tabs_render_only_active_panel(db, monkeypatch) -> None:
    calls = []
    for name in ("_conflict_tab", "_import_tab", "_review_tab", "_duplicates_tab"):
        monkeypatch.setattr(review_ui, name, lambda name=name: calls.append(name))
    monkeypatch.setattr(review_ui, "list_production_cases", lambda: pytest.fail("Badge must not load case payloads"))
    app = AppTest.from_string("from frontend.management.reconciliation import render\nrender()").run()
    assert not app.exception
    assert calls == ["_conflict_tab"]
    app.session_state["show_import_tab"] = True
    app.run()
    assert not app.exception
    assert calls == ["_conflict_tab", "_import_tab"]
    assert app.session_state["reconciliation_tab"] == "Theater-JSON hochladen"


def test_conflict_ui_adds_false_positive_upload_anyway(db, theatre) -> None:
    original = save(db, Production(name="Fallen", organization_id=theatre.id))[0]
    record = {**HAMLET, "title": "Mann kann auch in Höhe Fallen", "url": "https://theatre.example/p/hoehe-fallen"}
    assert upload(theatre, record)["review"] == [record["title"]]
    app = AppTest.from_file(str(REVIEW_APP)).run()
    assert not app.exception
    next(button for button in app.button if button.label == "Upload trotzdem hinzufügen").click().run()
    assert not app.exception
    assert {row.name for row in rows(db, Production)} == {"Fallen", record["title"]}
    assert rows(db, Production, Production.id == original.id)[0].description is None
    created = rows(db, Production, Production.name == record["title"])[0]
    assert created.description == HAMLET["description"]
    assert len(rows(db, ProductionMembership, ProductionMembership.production_id == created.id)) == 3
    assert rows(db, EntitySource, EntitySource.entity_id == created.id)
    assert app.tabs[0].label == "Produktionskonflikte (0)"
    assert upload(theatre, record)["unchanged"] == [record["title"]]
    assert len(rows(db, Production)) == 2


def test_upload_ui_updates_conflict_count_without_reload(db, theatre) -> None:
    save(db, Production(name="Hamlet", organization_id=theatre.id, description="Old text"))
    app = AppTest.from_file(str(REVIEW_APP))
    app.session_state["reconciliation_tab"] = "Theater-JSON hochladen"
    app.run()
    assert app.tabs[0].label == "Produktionskonflikte (0)"
    app.selectbox(key="upload_theatre").set_value(str(theatre.id))
    app.file_uploader[0].upload("20260607_productions.json", json.dumps([HAMLET]).encode(), "application/json")
    app.run()
    next(button for button in app.button if button.label == "Importieren").click().run()
    assert not app.exception
    assert app.tabs[0].label == "Produktionskonflikte (1)"
    assert any("Import für Staatstheater abgeschlossen" in element.value for element in app.success)


FULL_SCRAPE = "20260607_120000_000000_all-people-3c26446d1d4f_productions.json"


def test_scrape_scope_from_snapshot_filename() -> None:
    assert scrape_scope(FULL_SCRAPE) == "all"
    assert scrape_scope("20261105_161042_783216_limit-1-no-people-3c26446d1d4f_productions.json") == "limit-1"
    assert scrape_scope("20260607_productions.json") is None
    assert scrape_scope(None) is None


def test_theatre_status_reports_latest_upload_coverage_and_fields(db, theatre) -> None:
    existing_hamlet(db, theatre)  # linked to another source only
    save(db, Production(name="Ohne Quelle", organization_id=theatre.id))
    macbeth = {"title": "Macbeth", "url": "https://www.theatre.example/p/macbeth", "description": "Neu"}
    ingest_records(
        [HAMLET, macbeth],
        organization_id=theatre.id,
        source_name="theatre.example",
        actor_id="test",
        captured_at=CAPTURE,
        file_name=FULL_SCRAPE,
    )
    status = theatre_status(theatre.id, now=CAPTURE + dt.timedelta(days=10))
    latest = status["latest"]
    assert (latest["scope"], latest["total"], latest["merged"], latest["created"]) == ("all", 2, 1, 1)
    assert latest["data_date"] == CAPTURE
    assert latest["age_days"] == 10
    assert latest["source"] == "theatre.example"
    assert status["own_hosts"] == ["theatre.example"]  # no website stored: the scraped site counts
    assert (status["productions"], status["scraped"], status["own_website"]) == (3, 2, 2)
    assert status["fields"] == {"description": 2, "premiere": 1, "director": 1, "cast": 1, "text": 2}
    assert rows(db, ImportRun)[0].file_name == FULL_SCRAPE


def test_own_website_uses_stored_theatre_url(db, theatre) -> None:
    with Session(db) as session:
        stored = session.get(Organization, theatre.id)
        assert stored is not None
        stored.url = "https://www.other-site.example/"
        session.add(stored)
        session.commit()
    upload(theatre, HAMLET)
    status = theatre_status(theatre.id)
    assert status["own_hosts"] == ["other-site.example"]
    assert (status["scraped"], status["own_website"]) == (1, 0)


def test_overview_counts_theatres_with_recent_uploads(db, theatre) -> None:
    stale, never = save(db, Organization(name="Altes Theater"), Organization(name="Nie hochgeladen"))
    save(db, Production(name="Alt", organization_id=never.id))
    upload(theatre, HAMLET, captured_at=CAPTURE)
    upload(stale, {**HAMLET, "url": "https://old.example/p/hamlet"}, captured_at=CAPTURE - dt.timedelta(days=200))
    overview = scrape_overview(recent_days=90, now=CAPTURE + dt.timedelta(days=5))
    assert (overview["theatres"], overview["uploaded"], overview["recent"]) == (3, 2, 1)
    assert [row["theatre"] for row in overview["rows"]] == ["Altes Theater", "Staatstheater"]


def test_upload_tab_shows_theatre_status_and_overview(db, theatre) -> None:
    upload(theatre, HAMLET, captured_at=dt.datetime.now(dt.UTC))
    app = AppTest.from_file(str(REVIEW_APP))
    app.session_state["reconciliation_tab"] = "Theater-JSON hochladen"
    app.run()
    app.selectbox(key="upload_theatre").set_value(str(theatre.id)).run()
    assert not app.exception
    markdown = " ".join(element.value for element in app.markdown)
    assert "**Staatstheater** · Stand" in markdown
    assert "Quelle theatre.example" in markdown
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["mit Upload-Quelle"] == "1 (100%)"
    assert metrics["Aktuell (≤ 90 Tage)"] == "1 (100%)"
