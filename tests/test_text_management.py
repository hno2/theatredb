import json
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import ai, user_management, wikidata
from backend.models import (
    EntityType,
    LineageRelation,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
)
from backend.texts import resolve_text_case
from frontend.management.texts import text_labels

TEXT_APP = Path(__file__).resolve().parents[1] / "frontend" / "management" / "texts.py"
PRODUCTION_APP = TEXT_APP.with_name("productions.py")


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


def test_text_picker_distinguishes_translations(db):
    root = Text(title="Hamlet", languages=["en"])
    translation = Text(title="Hamlet", languages=["de"])
    translator = Person(name="Schlegel")
    with Session(db) as session:
        session.add_all([root, translation, translator])
        session.flush()
        session.add_all(
            [
                TextLineage(
                    source_text_id=root.id, derived_text_id=translation.id, relation=LineageRelation.TRANSLATION
                ),
                TextContribution(text_id=translation.id, person_id=translator.id, role="translator"),
            ]
        )
        session.commit()
        labels = text_labels([root, translation])
    assert labels[str(root.id)] != labels[str(translation.id)]
    assert "de" in labels[str(translation.id)]
    assert "Schlegel" in labels[str(translation.id)]


def test_text_picker_batches_roots_and_credits(db):
    with Session(db) as session:
        session.add_all(Text(title=f"Work {index}") for index in range(100))
        session.commit()
        texts = list(session.exec(select(Text)).all())
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(db, "before_cursor_execute", capture)
    try:
        assert text_labels([]) == {}
        assert not queries
        assert len(text_labels(texts)) == 100
        assert len(queries) == 3
    finally:
        event.remove(db, "before_cursor_execute", capture)


def test_selected_text_shows_family_and_productions(db):
    root = Text(title="Original", languages=["en"])
    translation = Text(title="Übersetzung", languages=["de"])
    with Session(db) as session:
        session.add_all([root, translation])
        session.flush()
        session.add_all(
            [
                TextLineage(
                    source_text_id=root.id, derived_text_id=translation.id, relation=LineageRelation.TRANSLATION
                ),
                Production(name="Stage version", text_id=translation.id),
            ]
        )
        session.commit()
    app = AppTest.from_file(str(TEXT_APP)).run()
    assert not app.exception
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    assert not app.exception
    assert any("Übersetzung" in item.value for item in app.text)
    assert any("Stage version" in item.value for item in app.caption)
    assert not any("Derived Texts" in item.value or "Source Texts" in item.value for item in app.subheader)


def test_text_table_starts_with_author_and_linked_texts(db):
    original = Text(title="Original", languages=["en"])
    translation = Text(title="Übersetzung", languages=["de"])
    author = Person(name="Autorin")
    with Session(db) as session:
        session.add_all([original, translation, author])
        session.flush()
        session.add_all(
            [
                TextLineage(
                    source_text_id=original.id,
                    derived_text_id=translation.id,
                    relation=LineageRelation.TRANSLATION,
                ),
                TextContribution(text_id=original.id, person_id=author.id, role="author"),
            ]
        )
        session.commit()

    app = AppTest.from_file(str(TEXT_APP)).run()

    assert not app.exception
    table = app.dataframe[0]
    assert list(table.proto.column_order[:4]) == ["Auswahl", "title", "Autoren", "linked_texts"]
    row = table.value.loc[table.value["title"] == "Original"].iloc[0]
    assert row["Autoren"] == ["Autorin"]
    assert row["linked_texts"] == ["Übersetzung"]
    config = json.loads(table.proto.columns)
    assert config["Autoren"]["type_config"]["type"] == "list"
    assert config["linked_texts"]["type_config"]["type"] == "list"


def test_title_variant_editor_preserves_main_title(db):
    text = Text(title="Original", languages=["en"])
    text_id = text.id
    with Session(db) as session:
        session.add(text)
        session.commit()
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    editor_key = f"titles_{text_id}_0"
    app.session_state[editor_key] = {"edited_rows": {}, "added_rows": [], "deleted_rows": [0]}
    next(button for button in app.button if button.label == "Titelvarianten speichern").click()
    app.run()
    assert any("Haupttitel" in error.value for error in app.error)
    with Session(db) as session:
        assert session.exec(select(TextTitle).where(TextTitle.text_id == text_id)).one().title == "Original"


def test_languages_inline_autosave_validates_list(db):
    text = Text(title="Example", languages=["en"])
    text_id = text.id
    with Session(db) as session:
        session.add(text)
        session.commit()
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"languages": "en, de"}}, "added_rows": [], "deleted_rows": []}
    app.run()
    assert not app.exception
    with Session(db) as session:
        saved = session.get(Text, text_id)
        assert saved is not None and saved.languages == ["en", "de"]
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"languages": "english"}}, "added_rows": [], "deleted_rows": []}
    app.run()
    assert app.error
    with Session(db) as session:
        saved = session.get(Text, text_id)
        assert saved is not None and saved.languages == ["en", "de"]


def test_switching_productions_preserves_each_text_choice(db):
    with Session(db) as session:
        original = Text(title="Original", languages=["en"])
        translation = Text(title="Translation", languages=["de"])
        session.add_all(
            [
                Production(name="First", text=original),
                Production(name="Second", text=translation),
            ]
        )
        session.commit()
    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    names = app.dataframe[0].value["name"].tolist()
    app.session_state[key] = {"selection": {"rows": [names.index("First")], "columns": []}}
    app.run()
    assert next(widget for widget in app.multiselect if widget.label == "Text").value == ["Original — en"]
    app.session_state[key] = {"selection": {"rows": [names.index("Second")], "columns": []}}
    app.run()
    assert next(widget for widget in app.multiselect if widget.label == "Text").value == ["Translation — de"]


def test_credit_preview_resolves_translation_and_production_fassung(db):
    text = Text(title="Example")
    text_id = text.id
    production = Production(name="Stage", text_id=text_id)
    production_id = production.id
    with Session(db) as session:
        session.add_all([text, production])
        session.commit()
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    next(widget for widget in app.text_area if widget.label == "Credit einfügen").set_value(
        "von Alice / Deutsch von Bob / Fassung von Carol"
    )
    app.run()
    next(widget for widget in app.selectbox if widget.label == "Produktion für Fassung (optional)").select(
        str(production_id)
    )
    app.run()
    next(button for button in app.button if button.label == "Credit prüfen und speichern").click()
    app.run()
    assert not app.exception
    with Session(db) as session:
        credits = session.exec(select(TextContribution)).all()
        people = {person.id: person.name for person in session.exec(select(Person)).all()}
        assert {(people[c.person_id], c.role) for c in credits} == {("Alice", "author"), ("Bob", "translator")}
        assert next(c for c in credits if c.role == "translator").text_id != text_id
        assert session.exec(select(TextLineage)).one().relation == "translation"
        membership = session.exec(select(ProductionMembership)).one()
        assert (people[membership.person_id], membership.role_name) == ("Carol", "adapter")
        saved = session.get(Production, production_id)
        assert saved is not None and saved.text_id == text_id


def test_ai_preview_is_opt_in_and_does_not_repeat_requests(db, monkeypatch):
    text = Text(title="Example")
    with Session(db) as session:
        session.add(text)
        session.commit()
    calls = []

    def fake_parse(value, *, use_ai=False):
        calls.append(use_ai)
        return {
            "title": None,
            "authors": ["Alice"],
            "translators": [],
            "adapters": [],
            "source_authors": [],
            "source_titles": [],
            "source_notes": [],
            "unparsed": [],
        }

    monkeypatch.setattr(ai, "parse_credits", fake_parse)
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    next(widget for widget in app.text_area if widget.label == "Credit einfügen").set_value("unrecognized")
    app.run()
    assert True not in calls
    next(button for button in app.button if button.label == "Unerkannte Teile mit KI vorschlagen").click()
    app.run()
    app.run()
    assert calls.count(True) == 1


@pytest.mark.parametrize(("credit", "adapters"), [("von Alice / Fassung von Carol", ["Carol"]), ("von Alice", [])])
def test_ambiguous_credit_preserves_production_and_original_input(db, credit, adapters):
    first, second = Text(title="Example"), Text(title="Example")
    production = Production(name="Stage", text_id=first.id)
    first_id, second_id, production_id = first.id, second.id, production.id
    with Session(db) as session:
        session.add_all([first, second, production])
        session.commit()
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    index = list(map(str, app.dataframe[0].value.index)).index(str(first_id))
    app.session_state[key] = {"edited_rows": {index: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    next(widget for widget in app.text_area if widget.label == "Credit einfügen").set_value(credit)
    app.run()
    next(widget for widget in app.selectbox if widget.label == "Produktion für Fassung (optional)").select(
        str(production_id)
    )
    app.run()
    next(button for button in app.button if button.label == "Credit prüfen und speichern").click()
    app.run()
    assert not app.exception
    app.run()
    with Session(db) as session:
        case = session.exec(select(ReconciliationCase)).one()
        assert case.context["production_ids"] == [str(production_id)]
        assert case.context["production_adapter_names"] == {str(production_id): adapters}
        assert case.context["production_text_ids"] == {str(production_id): str(first_id)}
        entry = case.context["credit_inputs"][0]
        assert entry["credit"] == credit and entry["parsed"]["authors"] == ["Alice"]
        assert entry["production_id"] == str(production_id)
        assert not session.exec(select(ProductionMembership)).all()
        resolved = resolve_text_case(session, case.id, target_id=second_id, actor_id="test")
        session.commit()
        saved = session.get(Production, production_id)
        assert saved is not None and saved.text_id == first_id
        memberships = session.exec(select(ProductionMembership)).all()
        people = {person.id: person.name for person in session.exec(select(Person)).all()}
        assert {people[item.person_id] for item in memberships} == set(adapters)
        assert resolved.id == second_id


def test_credit_source_is_linked_only_when_selected(db):
    source, target = Text(title="Source"), Text(title="Target")
    source_id, target_id = source.id, target.id
    with Session(db) as session:
        session.add_all([source, target])
        session.commit()
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    target_index = app.dataframe[0].value["title"].tolist().index("Target")
    app.session_state[key] = {"edited_rows": {target_index: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    next(widget for widget in app.text_area if widget.label == "Credit einfügen").set_value("von Alice / nach Jane")
    app.run()
    next(widget for widget in app.selectbox if widget.label == "Vorlage zum nach-Credit (optional)").select(
        str(source_id)
    )
    app.run()
    next(button for button in app.button if button.label == "Credit prüfen und speichern").click()
    app.run()
    assert not app.exception
    with Session(db) as session:
        edge = session.exec(select(TextLineage)).one()
        assert (edge.source_text_id, edge.derived_text_id, edge.relation) == (source_id, target_id, "based_on")


def test_ai_title_requires_explicit_verification(db, monkeypatch):
    text = Text(title="Example")
    text_id = text.id
    with Session(db) as session:
        session.add(text)
        session.commit()
    monkeypatch.setattr(
        ai,
        "suggest_titles",
        lambda *args, **kwargs: [
            {"title": "Alternative", "language": "de", "verified": False, "source": "unverified_ai"},
        ],
    )
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    next(button for button in app.button if button.label == "KI-Titelvorschläge abrufen").click()
    app.run()
    assert next(button for button in app.button if button.label == "Geprüften Titel übernehmen").disabled
    next(widget for widget in app.checkbox if widget.label == "Titel und Sprache geprüft").check()
    app.run()
    next(button for button in app.button if button.label == "Geprüften Titel übernehmen").click()
    app.run()
    assert not app.exception
    with Session(db) as session:
        assert {title.title for title in session.exec(select(TextTitle).where(TextTitle.text_id == text_id)).all()} == {
            "Example",
            "Alternative",
        }


def test_wikidata_enrichment_requires_button(db, monkeypatch):
    text = Text(title="Example")
    with Session(db) as session:
        session.add(text)
        session.commit()
    calls = []
    hit = {"id": "Q1", "label": "Example", "description": "", "url": "", "record": {"id": "Q1"}}
    monkeypatch.setattr(wikidata, "search", lambda *args: [hit])
    monkeypatch.setattr(wikidata, "refs", lambda *args: [])
    monkeypatch.setattr(
        wikidata, "enrich", lambda *args, **kwargs: calls.append(True) or {"filled": [], "conflicts": []}
    )
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    assert not calls
    buttons = [b for b in app.button if b.label == "Bestätigen & leere Felder füllen"]
    assert len(buttons) == 1  # GND is offline in tests, only the Wikidata hit is offered
    buttons[0].click()
    app.run()
    assert calls == [True]


def test_ai_orphan_proposal_creates_review_case(db, monkeypatch):
    source, target = Text(title="Source"), Text(title="Target")
    source_id, target_id = source.id, target.id
    with Session(db) as session:
        session.add_all([source, target])
        session.commit()
    monkeypatch.setattr(
        ai,
        "complete_json",
        lambda *args: {
            "source_text_id": str(source_id),
            "relation": "adaptation",
            "reason": "Suggested",
        },
    )
    app = AppTest.from_file(str(TEXT_APP)).run()
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    target_index = app.dataframe[0].value["title"].tolist().index("Target")
    app.session_state[key] = {"edited_rows": {target_index: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run()
    next(widget for widget in app.multiselect if widget.label == "Mögliche Vorlagen für KI-Prüfung").select(
        str(source_id)
    )
    app.run()
    next(button for button in app.button if button.label == "KI-Verknüpfung zur Prüfung vorschlagen").click()
    app.run()
    assert not app.exception
    with Session(db) as session:
        case = session.exec(select(ReconciliationCase)).one()
        assert case.status == "pending" and case.target_id == target_id
        assert not session.exec(select(TextLineage)).all()


def test_lineage_inbox_accepts_proposed_target(db):
    source = Text(title="Source")
    target = Text(title="Adaptation")
    case = ReconciliationCase(
        key="lineage-test",
        entity_type=EntityType.TEXT,
        operation="text_lineage",
        target_id=target.id,
        incoming={"source_text_id": str(source.id), "relation": "adaptation"},
        reason="Review suggested lineage",
    )
    with Session(db) as session:
        session.add_all([source, target, case])
        session.commit()
    app = AppTest.from_file(str(TEXT_APP)).run()
    assert not app.exception
    next(button for button in app.button if button.label == "Annehmen / Änderungen speichern").click()
    app.run()
    assert not app.exception
    with Session(db) as session:
        assert session.exec(select(ReconciliationCase)).one().status == "approved"
        assert session.exec(select(TextLineage)).one().relation == "adaptation"
