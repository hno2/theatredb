import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend.ingestion.theapolis import _import_productions, _import_record, _prepare_import, validate_detail
from backend.ingestion.theapolis_import import accept_records
from backend.models import EntityType, Person, ProductionMembership, Source, SourceRecord, TextContribution


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(name="theapolis.de")
        session.add(source)
        session.flush()
        yield session, source.id
    engine.dispose()


def _detail(*credits: dict):
    return {
        "theapolis_detail_status": "parsed" if credits else "confirmed_empty",
        "theapolis_detail": {"credits": list(credits)},
    }


def _credit(person_id=9, name="Alex", role="Darstellerin", **extra: object):
    return {"person_id": person_id, "name": name, "role": role, **extra}


def _import(db, day, *items: dict):
    session, source_id = db

    def accept(kind, records):
        return accept_records(
            session, source_id, kind, records, captured_at=dt.datetime(2026, 1, day, tzinfo=dt.UTC), actor_id="test"
        )

    _, people, productions, _ = _prepare_import({"items": iter(items)}, archived=True)
    ids, _ = accept(
        EntityType.PERSON,
        [
            _import_record(
                {field: value for field, value in payload.items() if field != "profile_url"},
                external_id=key,
                external_url=payload.get("profile_url"),
            )
            for key, payload in people.items()
        ],
    )
    mapping = dict(zip(people, ids, strict=True))
    _import_productions(session, tuple(productions), {}, mapping, people, accept)
    return mapping


def test_detail_roles_characters_and_work_credit_scope(db):
    session, _ = db
    item = {
        "id": 1,
        "name": "Hamlet",
        "directors": [{"id": 9, "name": "Alex", "profile": {"url": "https://example.org/alex"}}],
        "creators": [{"id": 9, "name": "Alex"}],
        **_detail(
            _credit(character="  Hamlet / Prinz  ", profile_url="https://example.org/alex"),
            _credit(character="Hamlet / Prinz"),
            _credit(character="HAMLET / Prinz"),
            _credit(role=" Lichtdesign "),
            _credit(role="Puppenbau"),
            _credit(role="Maskenbau"),
            _credit(role="Autor"),
        ),
    }
    ids = _import(db, 2, item)
    _import(db, 2, item)
    _import(db, 3, {**item, **_detail()})
    assert len(set(ids.values())) == 3  # Name, URL and numeric ID alone do not link.
    assert {
        (row.person_id, row.role_name, row.character_name) for row in session.exec(select(ProductionMembership))
    } == {
        (ids["directors:9"], "director", None),
        (ids["creators:9"], "author", None),
        (ids["detail:9"], "actor", "Hamlet / Prinz"),
        (ids["detail:9"], "actor", "HAMLET / Prinz"),
        (ids["detail:9"], "light_designer", None),
        (ids["detail:9"], "puppenbau", None),
        (ids["detail:9"], "maskenbau", None),
        (ids["detail:9"], "author", None),
    }
    assert len(session.exec(select(ProductionMembership)).all()) == 8
    assert [(row.person_id, row.role) for row in session.exec(select(TextContribution))] == [
        (ids["creators:9"], "author")
    ]
    _import(db, 4, item)
    production = session.exec(select(SourceRecord).where(SourceRecord.entity_type == EntityType.PRODUCTION)).one()
    assert production.payload["context"]["theapolis_detail"]["credits"][4]["role"] == "Puppenbau"


def test_singing_and_music_theatre_direction_keep_credit_identity(db):
    session, _ = db
    item = {
        "id": 1,
        "name": "Opera",
        **_detail(
            _credit(role="Regie"),
            _credit(role="Musiktheaterregie"),
            _credit(role="Gesang (weibliche Solopartien)", character="Sophie"),
        ),
    }
    ids = _import(db, 2, item)
    _import(db, 3, item)
    person = session.get(Person, ids["detail:9"])
    assert set(person.type) == {"director", "singer"}
    assert {(row.role_name, row.character_name) for row in session.exec(select(ProductionMembership))} == {
        ("director", None),
        ("musiktheaterregie", None),
        ("singer", "Sophie"),
    }


def test_preserved_titles_are_distinct_from_their_categories(db):
    session, _ = db
    item = {
        "id": 1,
        "name": "Opera",
        **_detail(
            _credit(role="Gesang", character="Sophie"),
            _credit(role="Sopran", character="Sophie"),
            _credit(role="Choreografie"),
            _credit(role="Choreografie / Direktion"),
            _credit(role="Assistenz Schauspielregie"),
            _credit(role="Assistenz Musiktheaterregie"),
        ),
    }
    ids = _import(db, 2, item)
    _import(db, 3, item)
    assert set(session.get(Person, ids["detail:9"]).type) == {"singer", "choreographer", "assistant_director"}
    assert {row.role_name for row in session.exec(select(ProductionMembership))} == {
        "singer",
        "sopran",
        "choreographer",
        "choreografie / direktion",
        "assistenz schauspielregie",
        "assistenz musiktheaterregie",
    }


def test_profile_credit_name_variants_keep_one_person_and_source_names(db, caplog):
    first = {"id": 1, "name": "Play", **_detail(_credit("profile:89612", name="Alex Example"))}
    second = {"id": 2, "name": "Other", **_detail(_credit("profile:89612", name="A. Example"))}
    ids = _import(db, 1, first, second)
    assert list(ids) == ["detail:profile:89612"]
    assert len(db[0].exec(select(ProductionMembership)).all()) == 2
    names = {
        row.payload["context"]["theapolis_detail"]["credits"][0]["name"]
        for row in db[0].exec(select(SourceRecord).where(SourceRecord.entity_type == EntityType.PRODUCTION))
    }
    assert names == {"Alex Example", "A. Example"}
    assert "retaining one identity and both source names" in caplog.text


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("Elisabeth Clarke", "Elisabeth Clarke-Hasters", "Elisabeth Clarke-Hasters"),
        ("Martina Beate Mann", "Martina Mann", "Martina Mann"),
        ("Hans H. Diehl", "Hans Herbert Diehl", "Hans Herbert Diehl"),
        ("Muller", "Müller", "Müller"),
        ("Rene", "René", "René"),
    ],
)
def test_profile_name_fallback_is_order_independent(db, first, second, expected):
    def item(identity, name):
        return {"id": identity, "name": "Play", **_detail(_credit("profile:9", name=name))}

    mapping = _import(db, 1, item(1, first), item(2, second))
    person = db[0].get(Person, mapping["detail:profile:9"])
    assert person is not None
    assert person.name == expected
    assert set(person.alt_names) == {first, second} - {expected}
    _import(db, 2, item(2, second), item(1, first))
    assert person.name == expected
    assert set(person.alt_names) == {first, second} - {expected}


def test_official_profile_name_wins_and_manual_main_name_is_preserved(db):
    item = {"id": 1, "name": "Play", **_detail(_credit("profile:157005", name="Bernhard Niechotz"))}
    mapping = _import(db, 1, item)
    person = db[0].get(Person, mapping["detail:profile:157005"])
    assert person is not None
    updated = {
        **item,
        **_detail(_credit("profile:157005", name="Bernhard Niechotz", profile_name="Bernhard Bruchhardt")),
    }
    _import(db, 2, updated)
    assert person.name == "Bernhard Bruchhardt"
    assert person.alt_names == ["Bernhard Niechotz"]
    person.name = "Manually reviewed name"
    revised = {**item, **_detail(_credit("profile:157005", name="Bernhard N.", profile_name="Bernhard Bruchhardt"))}
    _import(db, 3, revised)
    assert person.name == "Manually reviewed name"
    assert set(person.alt_names) == {"Bernhard Niechotz", "Bernhard Bruchhardt", "Bernhard N."}


def test_official_name_updates_legacy_person_linked_by_profile_evidence(db):
    url = "https://www.theapolis.de/de/profil/alex"
    item = {"id": 1, "name": "Play", "directors": [{"id": 1, "name": "Alex", "profile": {"url": url}}]}
    legacy_id = _import(db, 1, item)["directors:1"]
    ids = _import(
        db,
        2,
        {
            **item,
            **_detail(_credit("profile:9", role="Regie", profile_url=url, profile_name="Alex Example")),
        },
    )
    person = db[0].get(Person, legacy_id)
    assert person is not None
    assert ids["detail:profile:9"] == legacy_id
    assert person.name == "Alex Example"
    assert person.alt_names == ["Alex"]


def test_explicit_alias_links_to_existing_legacy_person_and_persists(db):
    item = {"id": 1, "name": "Play", "directors": [{"id": 1, "name": "Alex"}]}
    legacy_id = _import(db, 1, item)["directors:1"]
    _import(db, 2, {**item, **_detail(_credit(name="Alex Example", role="Regie", legacy_ids={"directors": 1}))})
    ids = _import(db, 3, {"id": 2, "name": "Other", **_detail(_credit(name="Alex Example"))})
    assert ids["detail:9"] == legacy_id
    assert len(db[0].exec(select(ProductionMembership)).all()) == 2  # Verified director overlap deduplicated.
    with pytest.raises(ValueError, match="Conflicting Theapolis detail IDs"):
        _import(db, 4, {**item, **_detail(_credit(10, legacy_ids={"directors": "1"}))})


def test_alias_claim_does_not_merge_two_preexisting_people(db):
    legacy = {"id": 1, "name": "Play", "directors": [{"id": 1, "name": "Alex"}]}
    assert _import(db, 1, legacy)["directors:1"] != _import(db, 2, {**legacy, **_detail(_credit())})["detail:9"]
    with pytest.raises(ValueError, match="source identity collision"):
        _import(db, 3, {**legacy, **_detail(_credit(legacy_ids={"directors": 1}))})


@pytest.mark.parametrize("existing", ["list_only", "split", "ambiguous"])
def test_archive_links_only_unambiguous_profile_overlap(db, existing, caplog):
    session, _ = db
    url = "https://www.theapolis.de/de/profil/alex"
    item = {
        "id": 1,
        "name": "Macbeth",
        "directors": [{"id": 4656, "name": "Alex", "profile": {"url": url}}],
    }
    key = "detail:profile:130702"
    credit = _credit("profile:130702", role="Schauspielregie", profile_url=f"{url}/")
    legacy_id = _import(db, 1, item)["directors:4656"]
    credits = [credit]
    if existing == "split":
        _import(db, 2, {**item, **_detail(_credit("profile:130702", role="Regie"))})
    elif existing == "ambiguous":
        credits.append({**credit, "person_id": "profile:130703"})
    archive_item = {**item, **_detail(*credits)}
    ids = _import(db, 3, archive_item)
    assert _import(db, 3, archive_item) == ids
    members = session.exec(select(ProductionMembership)).all()
    assert all(member.role_name == "director" for member in members)
    if existing == "list_only":
        assert ids[key] == legacy_id
        assert [member.person_id for member in members] == [legacy_id]
        alias = session.exec(select(SourceRecord).where(SourceRecord.record_key == "alias:directors:4656")).one()
        assert alias.entity_id == legacy_id
    else:
        assert ids[key] != legacy_id
        assert len(members) == (2 if existing == "split" else 3)
        assert "profile overlap" in caplog.text


def test_alias_registered_before_legacy_listing_exists(db):
    detail_id = _import(db, 1, {"id": 2, "name": "Other", **_detail(_credit(legacy_ids={"creators": 9}))})["detail:9"]
    legacy = {"id": 1, "name": "Play", "creators": [{"id": 9, "name": "Alex"}]}
    assert _import(db, 2, legacy)["creators:9"] == detail_id


@pytest.mark.parametrize(
    "fields",
    [
        {"theapolis_detail_status": "parsed", "theapolis_detail": {"credits": []}},
        {"theapolis_detail_status": "confirmed_empty", "theapolis_detail": {"credits": [None]}},
        {"theapolis_detail": {"credits": []}},
        _detail(_credit(True)),
        _detail(_credit(" 9 ")),
        _detail(_credit(legacy_ids={"actors": 9})),
        _detail(_credit(legacy_ids={"creators": "a\x00b"})),
        _detail(_credit(character=9)),
        _detail(_credit(profile_name=9)),
        _detail(_credit(profile_name="Alex\x00")),
        _detail(_credit(role=" ")),
    ],
)
def test_overlay_validation_rejects_malformed_or_ambiguous_evidence(fields):
    with pytest.raises((TypeError, ValueError)):
        validate_detail({"production_id": 1, **fields})
