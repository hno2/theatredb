import json
from collections.abc import Iterator
from datetime import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.models import (
    Award,
    EntitySource,
    Organization,
    Person,
    Production,
    ProductionMembership,
    Source,
    Text,
    TextContribution,
)
from frontend.management.filters import field_value_options
from frontend.management.uifactory import _build_fields, _production_choices, _rename_existing_fk_if_changed

PRODUCTION_APP = Path(__file__).resolve().parents[1] / "frontend" / "management" / "productions.py"
PERSON_APP = PRODUCTION_APP.with_name("persons.py")


@pytest.fixture
def db(monkeypatch, tmp_path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'production-management.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


def test_selected_production_integer_field_is_rendered_without_decimal(db) -> None:
    with Session(db) as session:
        session.add_all([Production(name="With duration", duration=90), Production(name="No duration")])
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    assert not app.exception

    dataframe = app.dataframe[0].value
    selected_row = next(index for index, value in enumerate(dataframe["duration"]) if value == 90)
    dataframe_key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[dataframe_key] = {"selection": {"rows": [selected_row], "columns": []}}
    app.run()

    assert not app.exception
    duration = next(widget for widget in app.text_input if widget.label == "Duration")
    assert duration.value == "90"


def test_production_table_resolves_foreign_keys_to_names(db) -> None:
    organization = Organization(name="Example Theatre")
    text = Text(title="Example Play")
    production = Production(
        name="Example Production",
        organization=organization,
        text=text,
    )
    organization_id = organization.id
    text_id = text.id
    with Session(db) as session:
        session.add(production)
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()

    assert not app.exception
    table = app.dataframe[0].value
    assert "organization" in table.columns
    assert "text" in table.columns
    assert ["Example Theatre"] in table["organization"].values
    assert ["Example Play"] in table["text"].values
    assert str(organization_id) not in str(table)
    assert str(text_id) not in str(table)
    assert list(app.dataframe[0].proto.column_order[:4]) == ["name", "premiere", "organization", "text"]
    config = json.loads(app.dataframe[0].proto.columns)
    assert config["organization"]["type_config"]["type"] == "list"
    assert config["text"]["type_config"]["type"] == "list"


def test_search_covers_productions_beyond_first_page(db) -> None:
    with Session(db) as session:
        session.add_all([Production(name=f"Alpha {i:03}") for i in range(60)] + [Production(name="Zeta 100%")])
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 50
    assert app.text_input(key="crud_search_production").proto.live_debounce_ms == 500
    app.text_input(key="crud_search_production").set_value("100%").run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Zeta 100%"]
    assert app.caption[0].value == "61 Produktionen insgesamt"


def test_filter_section_applies_sql_filters(db) -> None:
    venue = Organization(name="Schauspielhaus")
    with Session(db) as session:
        session.add_all(
            [
                Production(name="Match", organization=venue, duration=90, tags=["Komödie"], premiere=dt(2020, 5, 1)),
                Production(name="Other venue", organization=Organization(name="Oper"), duration=90, tags=["Komödie"]),
                Production(name="Too long", organization=venue, duration=200, tags=["Komödie"]),
                Production(name="Wrong tag", organization=venue, duration=90, tags=["Drama"]),
            ]
            + [Production(name=f"Filler {i:03}") for i in range(60)]
        )
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    assert not app.exception
    assert not app.expander[0].proto.expanded
    assert app.text_input(key="filter_production_organization_id").proto.live_debounce_ms == 500
    assert app.multiselect(key="filter_production_tags").options == ["Drama", "Komödie"]
    app.text_input(key="filter_production_organization_id").set_value("schauspiel")
    app.number_input(key="filter_production_duration_to").set_value(120)
    app.multiselect(key="filter_production_tags").set_value(["Komödie"])
    app.run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Match"]
    app.number_input(key="filter_production_premiere_from").set_value(2021).run()
    assert "Keine Einträge gefunden." in [info.value for info in app.info]


def test_person_filter_section_filters_json_types(db) -> None:
    with Session(db) as session:
        session.add_all(
            [
                Person(name="Ada", type=["director"], nationality="DE"),
                Person(name="Bea", type=["actor"], nationality="AT"),
                Person(name="Cora", type=["assistant_director"], nationality="DE"),
            ]
        )
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP.with_name("persons.py"))).run()
    app.multiselect(key="filter_person_type").set_value(["director", "actor"]).run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Ada", "Bea"]
    app.multiselect(key="filter_person_nationality").set_value(["DE"]).run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Ada"]
    app.button(key="filter_person__reset").click().run()
    assert len(app.dataframe[0].value) == 3


@pytest.mark.parametrize("count", [200, 201])
def test_category_filters_bound_options_and_leave_free_text_alone(db, count) -> None:
    with Session(db) as session:
        session.add_all(
            Organization(
                name=f"Theatre {i}", address_locality=f"City {i}", url="https://example.org", description="Same"
            )
            for i in range(count)
        )
        session.commit()
    app = AppTest.from_file(str(PRODUCTION_APP.with_name("organizations.py"))).run()
    assert not app.exception
    if count == 200:
        assert len(app.multiselect(key="filter_organization_address_locality").options) == 200
    else:
        assert app.text_input(key="filter_organization_address_locality")
    assert app.text_input(key="filter_organization_url")
    assert app.text_input(key="filter_organization_description")


def test_category_options_cache_bounded_flattened_values(db) -> None:
    with Session(db) as session:
        session.add(Person(name="Many types", type=[f"type{i:03}" for i in range(201)]))
        session.add(Organization(name="Many tags", tags=[f"tag{i:03}" for i in range(201)]))
        session.commit()
    queries = []

    def record_query(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        queries.append(statement)

    event.listen(db, "before_cursor_execute", record_query)
    try:
        assert field_value_options(Organization, "tags") is None
        assert field_value_options(Organization, "tags") is None
    finally:
        event.remove(db, "before_cursor_execute", record_query)
    assert len(queries) == 1
    assert "LIMIT" in queries[0]
    assert len(field_value_options(Person, "type") or []) == 201


def _select_person(app: AppTest) -> AppTest:
    key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    return app.run()


@pytest.mark.parametrize("selected_rows", [[0], [0, 1]])
def test_person_selection_roundtrips_through_url(db, selected_rows) -> None:
    people = [Person(name=name) for name in ("Ada", "Bea")]
    ids = [str(person.id) for person in people]
    with Session(db) as session:
        session.add_all(people)
        session.commit()
    app = AppTest.from_file(str(PERSON_APP))
    app.query_params["keep"] = "value"
    app.run()
    key = app.dataframe[0].proto.id.split("-", 2)[2]
    app.session_state[key] = {
        "edited_rows": {index: {"Auswahl": True} for index in selected_rows},
        "added_rows": [],
        "deleted_rows": [],
    }
    app.run()
    assert not app.exception
    expected = [ids[index] for index in selected_rows]
    saved_params = dict(app.query_params)
    app.query_params["person"] = ids[1]
    app.run()
    assert not app.exception
    table = app.dataframe[0].value
    assert table.index[table["Auswahl"]].tolist() == [ids[1]]
    restored = AppTest.from_file(str(PERSON_APP))
    restored.query_params.update(saved_params)
    restored.run()
    assert not restored.exception
    table = restored.dataframe[0].value
    assert table.index[table["Auswahl"]].tolist() == expected
    assert restored.query_params["keep"] == "value"
    key = restored.dataframe[0].proto.id.split("-", 2)[2]
    restored.session_state[key] = {
        "edited_rows": {index: {"Auswahl": False} for index in selected_rows},
        "added_rows": [],
        "deleted_rows": [],
    }
    restored.run()
    assert not restored.exception
    assert "person" not in restored.query_params
    assert restored.query_params["keep"] == "value"


def test_person_url_selection_loads_beyond_first_page_and_survives_search(db) -> None:
    selected_person = Person(name="Z Selected")
    selected_id = str(selected_person.id)
    with Session(db) as session:
        session.add_all([Person(name=f"Person {i:03}") for i in range(60)] + [selected_person])
        session.commit()
    app = AppTest.from_file(str(PERSON_APP))
    app.query_params["person"] = selected_id
    app.run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["name"] == "Z Selected"
    assert app.dataframe[0].value.iloc[0]["Auswahl"]
    assert app.tabs[0].label == "Edit"
    app.text_input(key="crud_search_person").set_value("no matching names").run()
    assert not app.exception
    assert app.dataframe[0].value.index.tolist() == [selected_id]
    assert app.dataframe[0].value.iloc[0]["Auswahl"]
    app.run()
    assert app.dataframe[0].value.iloc[0]["Auswahl"]


def test_person_url_selection_rejects_bad_and_deleted_ids(db) -> None:
    app = AppTest.from_file(str(PERSON_APP))
    app.query_params["person"] = ["not-a-uuid", "00000000-0000-0000-0000-000000000001"]
    app.run()
    assert not app.exception
    assert "person" not in app.query_params
    assert len(app.warning) == 2


def test_person_edit_keeps_current_types_when_cached_options_are_stale(db) -> None:
    person = Person(name="Ada", type=["director"])
    person_id = person.id
    with Session(db) as session:
        session.add(person)
        session.commit()
    app = AppTest.from_file(str(PERSON_APP)).run()
    assert field_value_options(Person, "type") == ["director"]
    with Session(db) as session:
        saved = session.get(Person, person_id)
        assert saved is not None
        saved.type = ["author"]
        session.add(saved)
        session.commit()
    app.run()
    app = _select_person(app)
    assert not app.exception
    widget = app.multiselect(key=f"edit_person_{person_id}_field_type")
    assert widget.value == ["author"]
    assert "author" in widget.options


def test_production_suggestions_are_bounded_cached_and_escape_search(db) -> None:
    with Session(db) as session:
        session.add_all(
            [Production(name=f"Hamlet {i:03}", premiere=f"{1900 + i}") for i in range(60)]
            + [Production(name="100% Hamlet", premiere="2026"), Production(name="100X Hamlet")]
        )
        session.commit()
    queries = []

    def record_query(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        queries.append(statement)

    event.listen(db, "before_cursor_execute", record_query)
    try:
        assert _production_choices("H") == {}
        assert queries == []
        choices = _production_choices("Ha")
        assert len(choices) == 50
        assert next(iter(choices.values()))[0] == "100% Hamlet"
        assert _production_choices("Ha") == choices
        assert len(queries) == 1
        assert [info[0] for info in _production_choices("100%").values()] == ["100% Hamlet"]
    finally:
        event.remove(db, "before_cursor_execute", record_query)


def test_production_add_searches_while_typing_and_saves_existing_choice(db) -> None:
    person = Person(name="Ada")
    production = Production(name="Hamlet", organization=Organization(name="Example Theatre"), premiere="2026")
    person_id, production_id = person.id, production.id
    with Session(db) as session:
        session.add_all([person, production])
        session.commit()
    app = _select_person(AppTest.from_file(str(PERSON_APP)).run())
    app.session_state[f"detail_tabs_Person_{person_id}"] = "Productions :grey-badge[0]"
    app.run()
    form_key = f"add_ProductionMembership_{person_id}"
    choice_key = f"{form_key}_fk_pick_production_id"
    search_key = f"{form_key}_fk_search_production_id"
    assert app.multiselect(key=choice_key).options == []
    app.text_input(key=search_key).set_value("H").run()
    assert app.multiselect(key=choice_key).options == []
    app.text_input(key=search_key).set_value("Ha").run()
    assert not app.exception
    options = app.multiselect(key=choice_key).options
    assert len(options) == 1
    assert "Example Theatre" in options[0] and "2026" in options[0]
    app.multiselect(key=choice_key).set_value(options)
    app.text_input(key=f"{form_key}_field_role_name").set_value("director")
    next(button for button in app.button if button.label == "Add to Productions").click().run()
    assert not app.exception
    assert not app.error
    assert app.tabs[1].label == "Productions :grey-badge[1]"
    with Session(db) as session:
        membership = session.exec(select(ProductionMembership)).one()
        assert membership.person_id == person_id
        assert membership.production_id == production_id


def test_person_relation_tabs_show_latest_productions_with_context(db, monkeypatch) -> None:
    person = Person(name="Example Person")
    person_id = person.id
    with Session(db) as session:
        session.add_all(
            ProductionMembership(
                person=person,
                production=Production(name=name, premiere=premiere, organization=Organization(name=name + " Theatre")),
                role_name="actor",
            )
            for name, premiere in [("Old", "2000"), ("Unknown", None), ("Latest", "2026-09"), ("Earlier", "2026-01")]
        )
        session.commit()
    app = _select_person(AppTest.from_file(str(PERSON_APP)).run())
    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Edit",
        "Productions :grey-badge[4]",
        "Text Contributions :grey-badge[0]",
    ]
    assert len(app.dataframe) == 1
    app.query_params["tab"] = "Productions"
    app.run()
    assert not app.exception
    editor = app.dataframe[1]
    assert editor.value["production_id"].tolist() == ["Latest", "Earlier", "Old", "Unknown"]
    assert editor.value["theatre"].tolist() == ["Latest Theatre", "Earlier Theatre", "Old Theatre", "Unknown Theatre"]
    assert editor.value["year"].iloc[:3].tolist() == [2026, 2026, 2000]
    assert editor.proto.editing_mode == editor.proto.EditingMode.FIXED
    app.session_state[editor.proto.id.split("-", 2)[2]] = {
        "edited_rows": {0: {"role_name": "director"}},
        "added_rows": [],
        "deleted_rows": [],
    }
    app.button(key=f"save_ProductionMembership_person_id_{person_id}").click().run()
    editor_key = app.dataframe[1].proto.id.split("-", 2)[2]
    app.session_state[editor_key] = {"edited_rows": {1: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run().button(key=f"remove_ProductionMembership_person_id_{person_id}").click().run()
    assert not app.exception
    assert not app.error
    assert app.tabs[1].label == "Productions :grey-badge[3]"
    assert app.dataframe[1].value["production_id"].tolist() == ["Latest", "Old", "Unknown"]
    assert app.dataframe[1].value["role_name"].tolist() == ["director", "actor", "actor"]
    open_key = f"open_ProductionMembership_person_id_{person_id}"
    assert app.button(key=open_key).disabled
    editor_key = app.dataframe[1].proto.id.split("-", 2)[2]
    app.session_state[editor_key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    switched = []
    monkeypatch.setattr("streamlit.switch_page", lambda page, query_params: switched.append((page, query_params)))
    app.run().button(key=open_key).click().run()
    with Session(db) as session:
        latest = session.exec(select(Production).where(Production.name == "Latest")).one()
    assert switched == [("management/productions.py", {"production": [str(latest.id)]})]


def test_management_pages_deeplink_selected_record_and_tab(db) -> None:
    production = Production(name="Deep Link")
    production_id = production.id
    with Session(db) as session:
        session.add_all([Production(name=f"Production {i:03}") for i in range(60)] + [production])
        session.commit()
    app = AppTest.from_file(str(PRODUCTION_APP))
    app.query_params["production"] = str(production_id)
    app.run()
    assert not app.exception
    assert app.text_input(key=f"edit_production_{production_id}_field_name").value == "Deep Link"
    person = Person(name="Ada")
    person_id = person.id
    with Session(db) as session:
        session.add(person)
        session.commit()
    app = AppTest.from_file(str(PERSON_APP))
    app.query_params.update(person=str(person_id), tab="Text Contributions")
    app.run()
    assert not app.exception
    assert app.session_state[f"detail_tabs_Person_{person_id}"] == "Text Contributions :grey-badge[0]"
    assert app.tabs[2].label == "Text Contributions :grey-badge[0]"


def test_person_text_editor_edits_and_deletes_only_links(db) -> None:
    person = Person(name="Example Person")
    text = Text(title="Example Text")
    other = Person(name="Z Other")
    person_id, text_id, other_id = person.id, text.id, other.id
    with Session(db) as session:
        session.add_all(
            [
                TextContribution(person=person, text=text, role="author"),
                TextContribution(person=person, text=text, role="editor"),
                TextContribution(person=other, text=text, role="author"),
            ]
        )
        session.commit()
    app = _select_person(AppTest.from_file(str(PERSON_APP)).run())
    tab_key = f"detail_tabs_Person_{person_id}"
    app.session_state[tab_key] = "Text Contributions :grey-badge[2]"
    app.run()
    assert not app.exception
    editor = app.dataframe[1]
    assert editor.proto.editing_mode == editor.proto.EditingMode.FIXED
    editor_key = editor.proto.id.split("-", 2)[2]
    app.session_state[editor_key] = {"edited_rows": {1: {"role": "translator"}}, "added_rows": [], "deleted_rows": []}
    app.button(key=f"save_TextContribution_person_id_{person_id}").click().run()
    editor_key = app.dataframe[1].proto.id.split("-", 2)[2]
    app.session_state[editor_key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run().button(key=f"remove_TextContribution_person_id_{person_id}").click().run()
    assert not app.exception
    assert not app.error, [error.value for error in app.error]
    assert app.tabs[2].label == "Text Contributions :grey-badge[1]"
    assert app.dataframe[1].value["role"].tolist() == ["translator"]
    with Session(db) as session:
        credits = session.exec(select(TextContribution)).all()
        assert {(credit.person_id, credit.role) for credit in credits} == {
            (person_id, "translator"),
            (other_id, "author"),
        }
        assert session.get(Text, text_id) is not None
    editor_key = app.dataframe[1].proto.id.split("-", 2)[2]
    app.session_state[editor_key] = {"edited_rows": {0: {"Auswahl": True}}, "added_rows": [], "deleted_rows": []}
    app.run().button(key=f"remove_TextContribution_person_id_{person_id}").click().run()
    assert not app.exception
    assert app.tabs[2].label == "Text Contributions :grey-badge[0]"
    assert app.dataframe[1].value.empty
    app.multiselect(key=f"add_TextContribution_{person_id}_fk_pick_text_id").set_value(["Example Text"])
    app.text_input(key=f"add_TextContribution_{person_id}_field_role").set_value("author")
    next(button for button in app.button if button.label == "Add to Text Contributions").click().run()
    assert not app.exception
    assert not app.error
    assert app.tabs[2].label == "Text Contributions :grey-badge[1]"
    assert app.dataframe[1].value["role"].tolist() == ["author"]


def test_person_text_contributions_sorted_and_text_choices_cached(db) -> None:
    person = Person(name="Example Person")
    person_id = person.id
    with Session(db) as session:
        session.add_all(
            [
                TextContribution(person=person, text=Text(title=title), role=role)
                for title, role in (("Zeta", "author"), ("alpha", "editor"), ("Beta", "author"))
            ]
        )
        session.commit()
    app = _select_person(AppTest.from_file(str(PERSON_APP)).run())
    app.session_state[f"detail_tabs_Person_{person_id}"] = "Text Contributions :grey-badge[3]"
    queries = []

    def record_query(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        if "text.title" in statement and "WHERE" not in statement:
            queries.append(statement)

    event.listen(db, "before_cursor_execute", record_query)
    try:
        app.run()
        app.run()
    finally:
        event.remove(db, "before_cursor_execute", record_query)
    assert not app.exception
    assert app.dataframe[1].value["text_id"].tolist() == ["alpha", "Beta", "Zeta"]
    assert len(queries) == 1


def test_person_text_contributions_merge_selected_texts(db) -> None:
    person = Person(name="Example Person")
    keep, duplicate = Text(title="Faust"), Text(title="Faust I")
    person_id, keep_id, duplicate_id = person.id, keep.id, duplicate.id
    with Session(db) as session:
        session.add_all([TextContribution(person=person, text=text, role="author") for text in (keep, duplicate)])
        session.commit()
    app = _select_person(AppTest.from_file(str(PERSON_APP)).run())
    app.session_state[f"detail_tabs_Person_{person_id}"] = "Text Contributions :grey-badge[2]"
    app.run()
    assert app.button(key="merge_text").disabled
    editor_key = app.dataframe[1].proto.id.split("-", 2)[2]
    app.session_state[editor_key] = {
        "edited_rows": {0: {"Auswahl": True}, 1: {"Auswahl": True}},
        "added_rows": [],
        "deleted_rows": [],
    }
    app.run()
    assert not app.button(key="merge_text").disabled
    app.button(key="merge_text").click().run()
    assert not app.exception
    survivor = app.selectbox(key="merge_survivor_text")
    survivor.set_value(str(keep_id)).run()
    next(box for box in app.selectbox if box.label == "Titel: Wert behalten").set_value(str(keep_id)).run()
    next(button for button in app.button if button.label == "Zusammenführung speichern").click().run()
    assert not app.exception
    assert app.tabs[2].label == "Text Contributions :grey-badge[1]"
    assert len(app.dataframe[1].value) == 1
    with Session(db) as session:
        assert session.get(Text, duplicate_id) is None
        survivor_text = session.get(Text, keep_id)
        assert survivor_text is not None
        assert survivor_text.title == "Faust"
        credits = session.exec(select(TextContribution)).all()
        assert [(credit.person_id, credit.text_id, credit.role) for credit in credits] == [
            (person_id, keep_id, "author")
        ]


@pytest.mark.parametrize(
    ("model", "label"),
    [
        (Organization, "Organisationen"),
        (Person, "Personen"),
        (Production, "Produktionen"),
        (Text, "Texte"),
        (Award, "Auszeichnungen"),
        (Source, "Quellen"),
    ],
)
def test_management_pages_show_item_count(db, model, label) -> None:
    field = "title" if model is Text else "name"
    with Session(db) as session:
        session.add_all([model(**{field: f"Example {index}"}) for index in range(2)])
        session.commit()

    app = AppTest.from_string(
        f"from backend.models import {model.__name__}\n"
        "from frontend.management.uifactory import render_sqlmodel_crud\n"
        f"render_sqlmodel_crud(model_cls={model.__name__})"
    ).run()

    assert not app.exception
    assert app.caption[0].value == f"2 {label} insgesamt"


@pytest.mark.parametrize(
    "model,record_url",
    [
        (Organization, "https://example.org/records/123"),
        (Person, "https://example.org/records/123"),
        (Production, "https://example.org/records/123"),
        (Text, "https://example.org/records/123"),
        (Award, "https://example.org/records/123"),
        (Production, None),
        (Production, "javascript:alert(1)"),
    ],
)
def test_selected_entity_sources_have_clickable_record_urls(db, model, record_url) -> None:
    entity = model(**({"title": "Example"} if model is Text else {"name": "Example"}))
    source = Source(name="Example Source", base_url="https://example.org")
    with Session(db) as session:
        session.add(entity)
        session.add(source)
        session.flush()
        session.add(
            EntitySource(
                source_id=source.id,
                entity_type=model._entity_type,
                entity_id=entity.id,
                external_url=record_url,
            )
        )
        session.commit()

    app = AppTest.from_string(
        f"from backend.models import {model.__name__}\n"
        "from frontend.management.uifactory import render_sqlmodel_crud\n"
        f"render_sqlmodel_crud(model_cls={model.__name__})"
    ).run()
    dataframe_key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[dataframe_key] = {"selection": {"rows": [0], "columns": []}}
    app.run()

    assert not app.exception
    sources = app.dataframe[1]
    if record_url and record_url.startswith("https://"):
        assert sources.value["external_url"].tolist() == [record_url]
    else:
        assert sources.value["external_url"].isna().all()
    assert len(app.warning) == int(bool(record_url and not record_url.startswith("https://")))
    assert sources.value["source"].tolist() == ["Example Source"]
    assert json.loads(sources.proto.columns)["external_url"]["type_config"]["type"] == "link"


@pytest.mark.parametrize("url", [None, "javascript:alert(1)", "https://example.org"])
def test_source_management_links_only_valid_web_urls(db, url) -> None:
    with Session(db) as session:
        session.add(Source(name="Example", base_url=url))
        session.commit()
    app = AppTest.from_string(
        "from backend.models import Source\n"
        "from frontend.management.uifactory import render_sqlmodel_crud\n"
        "render_sqlmodel_crud(model_cls=Source)"
    ).run()

    assert not app.exception
    table = app.dataframe[0]
    assert json.loads(table.proto.columns)["base_url"]["type_config"]["type"] == "link"
    if url and url.startswith("https://"):
        assert table.value["base_url"].tolist() == [url]
    else:
        assert table.value["base_url"].isna().all()
    assert len(app.warning) == int(bool(url and not url.startswith("https://")))


def test_organization_table_starts_with_name_and_clickable_websites(db) -> None:
    with Session(db) as session:
        session.add(
            Organization(name="Example Theatre", url="https://example.org", url_plays="https://example.org/plays")
        )
        session.commit()
    app = AppTest.from_file(str(PRODUCTION_APP.with_name("organizations.py"))).run()

    assert not app.exception
    table = app.dataframe[0]
    assert list(table.proto.column_order[:3]) == ["name", "url", "url_plays"]
    config = json.loads(table.proto.columns)
    assert config["url"]["type_config"]["type"] == "link"
    assert config["url_plays"]["type_config"]["type"] == "link"
    assert config["url"]["label"] == "Website"
    assert config["url_plays"]["label"] == "Spielplan"


def test_person_table_starts_with_name_type_and_life_dates(db) -> None:
    with Session(db) as session:
        session.add(Person(name="Example Person", type=["actor"]))
        session.commit()
    app = AppTest.from_file(str(PERSON_APP)).run()

    assert not app.exception
    table = app.dataframe[0]
    assert list(table.proto.column_order[:5]) == ["Auswahl", "name", "type", "born", "died"]
    assert table.value["type"].tolist() == [["actor"]]
    assert json.loads(table.proto.columns)["type"]["type_config"]["type"] == "list"


def test_cast_and_crew_editor_shows_person_name_without_ids(db) -> None:
    production = Production(name="Example Production")
    person = Person(name="Example Person")
    person_id = person.id
    with Session(db) as session:
        session.add(ProductionMembership(production=production, person=person, role_name="actor"))
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    dataframe_key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[dataframe_key] = {"selection": {"rows": [0], "columns": []}}
    app.run()

    assert not app.exception
    editor = app.dataframe[1]
    assert "Example Person" in editor.value["person_id"].values
    assert str(person_id) not in str(editor.value)
    column_config = json.loads(editor.proto.columns)
    assert column_config["__id"]["hidden"]
    assert column_config["person_id"]["label"] == "Person"


def test_renaming_cast_and_crew_name_updates_shared_person(db) -> None:
    production = Production(name="Example Production")
    other_production = Production(name="Other Production")
    production_id = production.id
    person = Person(name="Original Name")
    person_id = person.id
    with Session(db) as session:
        session.add_all(
            [
                ProductionMembership(production=production, person=person, role_name="actor"),
                ProductionMembership(production=other_production, person=person, role_name="director"),
            ]
        )
        session.commit()
    with Session(db) as session:
        membership = session.exec(
            select(ProductionMembership).where(ProductionMembership.production_id == production_id)
        ).one()
    person_field = next(
        field
        for field in _build_fields(
            ProductionMembership,
            field_overrides={
                "person_id": {"kind": "fk_select", "fk_model": Person, "fk_display": "name"},
                "production_id": {"exclude": True},
            },
        )
        if field.name == "person_id"
    )
    resolved_id = _rename_existing_fk_if_changed(
        person_field,
        "Renamed Person",
        membership,
        {str(person_id): "Original Name"},
        {"payload_field": "name", "rename_existing": True},
        "test",
    )

    assert resolved_id == str(person_id)
    with Session(db) as session:
        saved_person = session.get(Person, person_id)
        memberships = session.exec(select(ProductionMembership)).all()
        assert saved_person is not None and saved_person.name == "Renamed Person"
        assert len(memberships) == 2
        assert {membership.person_id for membership in memberships} == {person_id}
