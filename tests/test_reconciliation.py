import datetime as dt
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.entity_management import get_entity_graph
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
    TextLineage,
)
from backend.reconciliation import (
    ENTITY_MODELS,
    approve_case,
    canonical_role,
    decide_case,
    discover_duplicates,
    find_candidates,
    list_cases,
    merge_entities,
    merge_people,
    preview_case,
    preview_entity_merge,
    preview_people_merge,
    propose_record,
    queue_duplicate,
    resolve_id,
    search_entities,
    search_people_for_merge,
    union_values,
)
from backend.scd2 import SCD2History

CAPTURE = dt.datetime(2026, 6, 7, tzinfo=dt.UTC)
REVIEW_APP = Path(__file__).resolve().parents[1] / "frontend" / "management" / "reconciliation.py"


@pytest.fixture
def db(monkeypatch, tmp_path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}")

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


def count(db, model) -> int:
    with Session(db) as session:
        return len(session.exec(select(model)).all())


def approve(
    case: ReconciliationCase,
    target: uuid.UUID | str | None = None,
    *,
    fields: list[str] | None = None,
    allow_create: bool = False,
) -> uuid.UUID:
    preview = preview_case(case.id, target_id=target)
    return approve_case(
        case.id,
        revision=preview["revision"],
        target_id=target,
        actor_id="test",
        fields=fields,
        allow_create=allow_create,
    )


@pytest.fixture
def source(db) -> Source:
    return save(db, Source(name="theatre.example"))[0]


def test_production_matching_requires_org_and_directors(db) -> None:
    org, other_org, director, wrong_director = save(
        db,
        Organization(name="Theatre"),
        Organization(name="Other Theatre"),
        Person(name="Adrian Figueroa"),
        Person(name="Someone Else"),
    )
    one, two, foreign = save(
        db,
        Production(name="Hamlet", organization_id=org.id),
        Production(name="Hamlet", organization_id=org.id),
        Production(name="Hamlet", organization_id=other_org.id),
    )
    save(
        db,
        ProductionMembership(production_id=one.id, person_id=director.id, role="director"),
        ProductionMembership(production_id=two.id, person_id=wrong_director.id, role="Regie"),
    )
    candidates = find_candidates(
        "production",
        {"name": "Hamlet", "organization_id": str(org.id)},
        context={"directors": ["Adrian Figueroa"]},
    )
    by_id = {c["id"]: c for c in candidates}
    assert str(foreign.id) not in by_id
    assert by_id[str(one.id)]["recommendation"] == "strong"
    assert "Abweichende Regie" in by_id[str(two.id)]["conflicts"]
    assert by_id[str(two.id)]["recommendation"] == "review"
    assert all(c["recommendation"] == "review" for c in find_candidates("production", {"name": "Hamlet"}))


def test_premiere_conflict_and_missing_dates(db) -> None:
    org, director = save(db, Organization(name="Theatre"), Person(name="Director"))
    prod = save(db, Production(name="Hamlet", organization_id=org.id, premiere=dt.datetime(2020, 1, 1, tzinfo=dt.UTC)))[
        0
    ]
    save(db, ProductionMembership(production_id=prod.id, person_id=director.id, role="Regie"))
    incoming = {"name": "Hamlet", "organization_id": str(org.id), "premiere": "2026-01-01"}
    candidate = find_candidates("production", incoming, context={"directors": ["Director"]})[0]
    assert "Abweichende Premiere" in candidate["conflicts"]
    incoming.pop("premiere")
    assert find_candidates("production", incoming, context={"directors": ["Director"]})[0]["recommendation"] == "strong"


def test_update_preserves_conflicts_empty_fields_and_unions_lists(db, source) -> None:
    person = save(db, Person(name="Person", description="Manual biography", type=["director"]))[0]
    case = propose_record(
        "person",
        {"name": "Person", "description": "New biography", "type": ["author"], "nationality": None},
        source_id=source.id,
        external_id="1",
        captured_at=CAPTURE,
    )
    approve(case, person.id)
    with Session(db) as session:
        current = session.get(Person, person.id)
        assert current is not None
        assert current.description == "Manual biography"
        assert current.type == ["director", "author"]
    assert count(db, Person) == 1
    assert count(db, SourceRecord) == 1
    assert count(db, SCD2History) == 1
    later = propose_record(
        "person",
        {"name": "Person", "description": None, "type": ["author"]},
        source_id=source.id,
        external_id="1",
        captured_at=CAPTURE + dt.timedelta(days=1),
    )
    approve(later, person.id)
    assert count(db, SCD2History) == 1


def test_explicit_overwrite_and_invalid_field_selection(db, source) -> None:
    person = save(db, Person(name="Person", description="Old"))[0]
    case = propose_record("person", {"name": "Person", "description": "New"}, source_id=source.id, external_id="1")
    with pytest.raises(ValueError, match="Selected fields"):
        approve(case, person.id, fields=["id"])
    approve(case, person.id, fields=["description"])
    assert get_entity_graph(Person, str(person.id))["description"] == "New"


def test_no_implicit_creation_and_source_cannot_be_reassigned(db, source) -> None:
    case = propose_record("person", {"name": "One"}, source_id=source.id, external_id="1")
    with pytest.raises(ValueError, match="explicit approval"):
        approve(case)
    first_id = approve(case, allow_create=True)
    other = save(db, Person(name="Other"))[0]
    next_case = propose_record(
        "person",
        {"name": "Renamed"},
        source_id=source.id,
        external_id="1",
        captured_at=CAPTURE,
    )
    assert preview_case(next_case.id)["candidates"][0]["id"] == str(first_id)
    with pytest.raises(ValueError, match="Quellidentität"):
        approve(next_case, other.id)
    with pytest.raises(ValueError, match="Quellidentität"):
        approve(next_case, allow_create=True)


def test_stale_preview_and_rejection_reopening(db, source) -> None:
    person = save(db, Person(name="Person"))[0]
    case = propose_record("person", {"name": "Person", "description": "New"}, source_id=source.id, external_id="1")
    preview = preview_case(case.id, target_id=person.id)
    with Session(db) as session:
        current = session.get(Person, person.id)
        assert current is not None
        current.description = "Other editor"
        session.add(current)
        session.commit()
    with pytest.raises(ValueError, match="Data changed"):
        approve_case(case.id, revision=preview["revision"], target_id=person.id, actor_id="test")
    decide_case(case.id, status="rejected", actor_id="test", note="Not the same person")
    assert len(list_cases(status="rejected")) == 1
    decide_case(case.id, status="pending", actor_id="test", note="Additional evidence")
    assert len(list_cases()) == 1


def test_older_source_snapshot_is_blocked(db, source) -> None:
    case = propose_record("person", {"name": "Person"}, source_id=source.id, external_id="1", captured_at=CAPTURE)
    identity = approve(case, allow_create=True)
    old = propose_record(
        "person", {"name": "Old"}, source_id=source.id, external_id="1", captured_at=CAPTURE - dt.timedelta(days=1)
    )
    with pytest.raises(ValueError, match="Quellstand"):
        approve(old, identity, fields=["name"])
    undated = propose_record("person", {"name": "Undated"}, source_id=source.id, external_id="1")
    assert preview_case(undated.id, target_id=identity)["blocked"]


@pytest.mark.parametrize("kind", list(ENTITY_MODELS))
def test_all_entity_kinds_merge_and_redirect(db, kind) -> None:
    model = ENTITY_MODELS[kind]
    label = "title" if kind == EntityType.TEXT else "name"
    left, right = save(db, model.model_validate({label: "Same"}), model.model_validate({label: "Same"}))
    case = queue_duplicate(kind, left.id, right.id, reason="Manual")
    assert queue_duplicate(kind, right.id, left.id, reason="Programmatic").id == case.id
    approve(case)
    with Session(db) as session:
        assert session.get(model, right.id) is None
        assert resolve_id(session, kind, right.id) == left.id
    assert count(db, model) == 1
    assert get_entity_graph(model, str(right.id))["id"] == str(left.id)


def test_person_merge_rewires_all_links_and_deduplicates_roles(db, source) -> None:
    left, right, org, text, prod, award = save(
        db,
        Person(name="Same"),
        Person(name="Same"),
        Organization(name="Theatre"),
        Text(title="Work"),
        Production(name="Staging"),
        Award(name="Award"),
    )
    save(
        db,
        ProductionMembership(production_id=prod.id, person_id=left.id, role="director"),
        ProductionMembership(production_id=prod.id, person_id=right.id, role="Regie"),
        TextContribution(text_id=text.id, person_id=right.id, role="author"),
        OrganizationMembership(organization_id=org.id, person_id=right.id, role="actor"),
        AwardNomination(
            award_id=award.id,
            person_id=right.id,
            category="Actor",
            date=dt.datetime(2026, 1, 1),
            date_precision="year",
            result=AwardResult.WON,
        ),
        EntitySource(source_id=source.id, entity_type=EntityType.PERSON, entity_id=left.id, external_id="1"),
        EntitySource(source_id=source.id, entity_type=EntityType.PERSON, entity_id=right.id, external_id="2"),
    )
    approve(queue_duplicate("person", left.id, right.id, reason="Same identity"))
    with Session(db) as session:
        assert session.exec(select(ProductionMembership)).one().person_id == left.id
        assert session.exec(select(TextContribution)).one().person_id == left.id
        assert session.exec(select(OrganizationMembership)).one().person_id == left.id
        assert session.exec(select(AwardNomination)).one().person_id == left.id
        mappings = session.exec(select(SourceRecord)).all()
        assert {r.external_id for r in mappings} == {"1", "2"}
        assert all(r.entity_id == left.id for r in mappings)
        assert len(session.exec(select(EntitySource)).all()) == 1
    assert find_candidates("person", {"name": "Renamed"}, source_id=source.id, context={"external_id": "2"})[0][
        "id"
    ] == str(left.id)


def test_merge_survivor_can_be_swapped(db) -> None:
    left, right = save(db, Person(name="Left"), Person(name="Right"))
    case = queue_duplicate("person", left.id, right.id, reason="Manual")
    approve(case, right.id)
    with Session(db) as session:
        assert session.get(Person, left.id) is None
        assert resolve_id(session, "person", left.id) == right.id


def test_text_merge_rewires_productions_and_both_lineage_directions(db) -> None:
    left, right, third = save(db, Text(title="Work"), Text(title="Work"), Text(title="Derived"))
    prod = save(db, Production(name="Staging", text_id=right.id))[0]
    save(
        db,
        TextLineage(source_text_id=right.id, derived_text_id=third.id, relation="adaptation"),
        TextLineage(source_text_id=third.id, derived_text_id=right.id, relation="translation"),
        TextLineage(source_text_id=left.id, derived_text_id=right.id, relation="same"),
    )
    approve(queue_duplicate("text", left.id, right.id, reason="Manual"))
    with Session(db) as session:
        current = session.get(Production, prod.id)
        assert current is not None
        assert current.text_id == left.id
        lineages = session.exec(select(TextLineage)).all()
        assert len(lineages) == 2
        assert all(right.id not in {r.source_text_id, r.derived_text_id} for r in lineages)


def test_merge_failure_rolls_back_everything(db, source) -> None:
    left, right, third = save(db, Person(name="Same"), Person(name="Same"), Person(name="Third"))
    save(
        db,
        EntitySource(source_id=source.id, entity_type=EntityType.PERSON, entity_id=right.id, external_id="2"),
        SourceRecord(
            source_id=source.id, entity_type=EntityType.PERSON, entity_id=third.id, record_key="id:2", external_id="2"
        ),
    )
    case = queue_duplicate("person", left.id, right.id, reason="Manual")
    with pytest.raises(ValueError, match="third entity"):
        approve(case)
    assert count(db, Person) == 3
    assert count(db, EntityRedirect) == 0
    assert count(db, SCD2History) == 0
    assert list_cases()[0]["status"] == "pending"


def test_person_and_text_title_only_matches_remain_review(db) -> None:
    author, work, person = save(db, Person(name="Author"), Text(title="Hamlet"), Person(name="Namesake"))
    save(db, TextContribution(text_id=work.id, person_id=author.id, role="author"))
    assert find_candidates("person", {"name": person.name})[0]["recommendation"] == "review"
    assert find_candidates("text", {"title": "Hamlet"})[0]["recommendation"] == "review"
    assert (
        find_candidates("text", {"title": "Hamlet"}, context={"authors": ["Author"]})[0]["recommendation"] == "strong"
    )
    assert find_candidates("text", {"title": "Hamlet"}, context={"authors": ["Other"]})[0]["conflicts"]


def test_search_manual_pair_with_different_names_and_discovery(db) -> None:
    left, right = save(db, Organization(name="Same", city="Berlin"), Organization(name="Same", city="Hamburg"))
    assert len(search_entities("organization", "Same")) == 2
    assert search_entities("organization", str(left.id))[0]["id"] == str(left.id)
    assert search_entities("organization", "%") == []
    pairs = discover_duplicates("organization", "Same")
    assert len(pairs) == 1
    assert "Abweichend: city" in pairs[0]["right"]["conflicts"]
    case = queue_duplicate("organization", left.id, right.id, reason="Manual investigation")
    assert case.status == "pending"


def test_invalid_payloads_do_not_create_records(db, source) -> None:
    with pytest.raises(ValueError, match="protected"):
        propose_record("person", {"name": "Person", "id": str(uuid.uuid4())}, source_id=source.id, external_id="1")
    with pytest.raises(ValueError, match="Invalid source URL"):
        propose_record("person", {"name": "Person"}, source_id=source.id, external_url="javascript:bad")
    with pytest.raises(ValueError, match="stable"):
        propose_record("person", {"name": "Person"}, source_id=source.id)
    assert count(db, ReconciliationCase) == 0


@pytest.mark.parametrize(
    ("raw", "expected"), [("Regie", "director"), ("Inszenierung", "director"), ("creator", "author")]
)
def test_canonical_roles(raw, expected) -> None:
    assert canonical_role(raw) == expected


def test_review_ui_empty_and_manual_submission(db) -> None:
    left, right = save(db, Organization(name="Same"), Organization(name="Same"))
    app = AppTest.from_file(str(REVIEW_APP)).run()
    assert not app.exception
    assert app.title[0].value == "Datenabgleich & Duplikate"
    app.selectbox(key="duplicate_kind").set_value("organization").run()
    app.selectbox(key="duplicate_left").set_value(str(left.id)).run()
    app.selectbox(key="duplicate_right").set_value(str(right.id)).run()
    app.text_input(key="duplicate_reason").set_value("Same organization").run()
    next(button for button in app.button if button.label == "Duplikat zur Prüfung einreichen").click().run()
    assert not app.exception
    assert count(db, ReconciliationCase) == 1
    assert count(db, Organization) == 2
    app.run()
    assert not app.exception
    assert any("Genehmigen" in button.label and button.disabled for button in app.button)


def test_matching_uses_approved_person_identity_not_scraped_spelling(db, source) -> None:
    org, director = save(db, Organization(name="Theatre"), Person(name="Canonical Name"))
    prod = save(db, Production(name="Hamlet", organization_id=org.id))[0]
    save(db, ProductionMembership(production_id=prod.id, person_id=director.id, role="director"))
    person_case = propose_record("person", {"name": "Alternate Spelling"}, source_id=source.id, external_id="person")
    approve(person_case, director.id)
    case = propose_record(
        "production",
        {"name": "Hamlet", "organization_id": str(org.id)},
        source_id=source.id,
        external_id="prod",
        context={
            "directors": ["Alternate Spelling"],
            "credits": [{"case_id": str(person_case.id), "role": "director"}],
        },
    )
    assert preview_case(case.id)["candidates"][0]["recommendation"] == "strong"


def test_union_keeps_dictionary_lists() -> None:
    venue = {"name": "Main", "seats": "100"}
    studio = {"name": "Studio", "seats": "40"}
    assert union_values([venue], [venue, studio]) == [venue, studio]


def test_merge_keeps_lists_and_all_decision_snapshots(db) -> None:
    left, right = save(
        db,
        Person(name="Same", urls=["https://example.org/main"]),
        Person(name="Same", urls=["https://example.org/main", "https://example.org/studio"]),
    )
    case = queue_duplicate("person", left.id, right.id, reason="Same person")
    approve(case)
    with Session(db) as session:
        current = session.get(Person, left.id)
        assert current is not None
        assert current.urls == ["https://example.org/main", "https://example.org/studio"]
        current_case = session.get(ReconciliationCase, case.id)
        assert current_case is not None
        decision = current_case.context["decision"]
        assert decision["duplicate"]["record"]["id"] == str(right.id)
        assert decision["before"]["urls"] == ["https://example.org/main"]


def test_org_and_production_merges_rewire_graph(db) -> None:
    left, right, person, text, award = save(
        db,
        Organization(name="Theatre"),
        Organization(name="Theatre"),
        Person(name="Actor"),
        Text(title="Work"),
        Award(name="Award"),
    )
    prod_left, prod_right = save(
        db,
        Production(name="Show", organization_id=right.id, text_id=text.id),
        Production(name="Show", organization_id=right.id, text_id=text.id),
    )
    save(
        db,
        OrganizationMembership(organization_id=right.id, person_id=person.id, role="actor"),
        ProductionMembership(production_id=prod_right.id, person_id=person.id, role="actor", character="King"),
        AwardNomination(
            award_id=award.id,
            production_id=prod_right.id,
            category="Show",
            date=dt.datetime(2026, 1, 1),
            date_precision="year",
            result=AwardResult.WON,
        ),
    )
    approve(queue_duplicate("organization", left.id, right.id, reason="Same theatre"))
    approve(queue_duplicate("production", prod_left.id, prod_right.id, reason="Same staging"))
    with Session(db) as session:
        current = session.get(Production, prod_left.id)
        assert current is not None
        assert current.organization_id == left.id
        assert session.exec(select(OrganizationMembership)).one().organization_id == left.id
        assert session.exec(select(ProductionMembership)).one().production_id == prod_left.id
        assert session.exec(select(AwardNomination)).one().production_id == prod_left.id


def test_award_merge_deduplicates_identical_nominations(db) -> None:
    left, right, person = save(db, Award(name="Award"), Award(name="Award"), Person(name="Actor"))
    for award in (left, right):
        save(
            db,
            AwardNomination(
                award_id=award.id,
                person_id=person.id,
                category="Actor",
                date=dt.datetime(2026, 1, 1),
                date_precision="year",
                result=AwardResult.WON,
            ),
        )
    approve(queue_duplicate("award", left.id, right.id, reason="Same award"))
    assert count(db, AwardNomination) == 1


def test_stale_merge_preview_includes_relationship_changes(db) -> None:
    left, right, prod = save(db, Person(name="Same"), Person(name="Same"), Production(name="Work"))
    case = queue_duplicate("person", left.id, right.id, reason="Same person")
    preview = preview_case(case.id)
    save(db, ProductionMembership(production_id=prod.id, person_id=right.id, role="director"))
    with pytest.raises(ValueError, match="Data changed"):
        approve_case(case.id, revision=preview["revision"], actor_id="test")
    assert count(db, Person) == 2
    assert count(db, EntityRedirect) == 0


def test_legacy_actor_character_credit_is_updated_not_duplicated(db, source) -> None:
    person, production = save(db, Person(name="Actor"), Production(name="Hamlet"))
    credit = save(db, ProductionMembership(production_id=production.id, person_id=person.id, role="Hamlet"))[0]
    person_case = propose_record("person", {"name": "Actor"}, source_id=source.id, external_id="person")
    approve(person_case, person.id)
    case = propose_record(
        "production",
        {"name": "Hamlet"},
        source_id=source.id,
        external_id="prod",
        context={"credits": [{"case_id": str(person_case.id), "role": "actor", "character": "Hamlet"}]},
    )
    approve(case, production.id)
    with Session(db) as session:
        row = session.get(ProductionMembership, credit.id)
        assert row is not None
        assert (row.role, row.character) == ("actor", "Hamlet")
        assert count(db, ProductionMembership) == 1


def test_redirect_chain_and_unrelated_merge_target(db) -> None:
    first, second, third = save(db, Person(name="One"), Person(name="Two"), Person(name="Three"))
    case = queue_duplicate("person", first.id, second.id, reason="Same")
    with pytest.raises(ValueError, match="submitted pair"):
        preview_case(case.id, target_id=third.id)
    approve(case)
    approve(queue_duplicate("person", third.id, first.id, reason="Same"))
    with Session(db) as session:
        assert resolve_id(session, "person", second.id) == third.id
        assert resolve_id(session, "person", first.id) == third.id


def test_queue_search_pagination_and_duplicate_discovery_do_not_write(db, source) -> None:
    for index in range(3):
        propose_record("person", {"name": f"Person {index}"}, source_id=source.id, external_id=str(index))
    cases = list_cases(query="Person 1")
    assert len(cases) == 1
    assert list_cases(query=cases[0]["id"])[0]["id"] == cases[0]["id"]
    assert len(list_cases(limit=2)) == 2
    assert len(list_cases(limit=2, offset=2)) == 1
    assert count(db, Person) == 0
    assert discover_duplicates("person") == []


def test_capture_time_is_normalized_to_utc(db, source) -> None:
    local = dt.datetime(2026, 6, 7, 2, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    first = propose_record("person", {"name": "Person"}, source_id=source.id, external_id="1", captured_at=local)
    again = propose_record("person", {"name": "Person"}, source_id=source.id, external_id="1", captured_at=CAPTURE)
    assert first.id == again.id


def test_review_ui_merges_only_after_explicit_confirmation(db) -> None:
    left, right = save(db, Person(name="Same"), Person(name="Same"))
    case = queue_duplicate("person", left.id, right.id, reason="Same person")
    app = AppTest.from_file(str(REVIEW_APP)).run()
    assert not app.exception
    assert count(db, Person) == 2
    next(box for box in app.checkbox if box.label.startswith("Identität")).check().run()
    next(button for button in app.button if button.label == "Genehmigen und speichern").click().run()
    assert not app.exception
    assert count(db, Person) == 1
    assert list_cases(status="approved")[0]["id"] == str(case.id)


def test_review_ui_preserves_stale_preview_until_refreshed(db, source) -> None:
    person = save(db, Person(name="Person"))[0]
    case = propose_record(
        "person", {"name": "Person", "description": "Source bio"}, source_id=source.id, external_id="1"
    )
    app = AppTest.from_file(str(REVIEW_APP)).run()
    app.selectbox(key=f"target_{case.id}").set_value(str(person.id)).run()
    next(box for box in app.checkbox if box.label.startswith("Identität")).check().run()
    with Session(db) as session:
        row = session.get(Person, person.id)
        assert row is not None
        row.description = "Concurrent edit"
        session.add(row)
        session.commit()
    next(button for button in app.button if button.label == "Genehmigen und speichern").click().run()
    assert not app.exception
    assert any("Data changed" in error.value for error in app.error)
    assert get_entity_graph(Person, str(person.id))["description"] == "Concurrent edit"
    assert list_cases()[0]["status"] == "pending"


def test_duplicate_merge_allows_same_source_identity_on_submitted_pair(db, source) -> None:
    left, right = save(db, Person(name="Same"), Person(name="Same"))
    for person in (left, right):
        save(
            db,
            EntitySource(
                source_id=source.id, entity_type=EntityType.PERSON, entity_id=person.id, external_id="same-source-id"
            ),
        )
    approve(queue_duplicate("person", left.id, right.id, reason="Same source record"))
    assert count(db, Person) == 1
    assert count(db, EntitySource) == 1
    with Session(db) as session:
        record = session.exec(select(SourceRecord)).one()
        assert len(record.payload["merged_sources"]) == 2
        assert record.entity_id == left.id


def test_pending_merge_can_choose_survivor_after_other_merge_redirects(db) -> None:
    left, right, third = save(db, Person(name="Left"), Person(name="Right"), Person(name="Third"))
    pending = queue_duplicate("person", left.id, right.id, reason="Same person")
    approve(queue_duplicate("person", third.id, right.id, reason="Same person"))
    approve(pending, third.id)
    with Session(db) as session:
        assert resolve_id(session, "person", left.id) == third.id
        assert resolve_id(session, "person", right.id) == third.id


def test_people_merge_table_batches_context_and_paginates(db) -> None:
    people = save(db, *(Person(name=f"Person {index}") for index in range(5)))
    theatre, production, unknown = save(
        db, Organization(name="Theater", city="Berlin"), Production(name="Ohne Theater"), Production(name="Unbekannt")
    )
    show = save(db, Production(name="Hamlet", organization_id=theatre.id))[0]
    save(
        db,
        ProductionMembership(person_id=people[0].id, production_id=show.id, role="actor"),
        ProductionMembership(person_id=people[0].id, production_id=show.id, role="director"),
        ProductionMembership(person_id=people[0].id, production_id=production.id),
        ProductionMembership(person_id=people[4].id, production_id=unknown.id),
    )
    statements = []

    def record_statement(_, __, statement, *___: object) -> None:
        statements.append(statement)

    event.listen(db, "before_cursor_execute", record_statement)
    try:
        rows = search_people_for_merge("Person", limit=2)
    finally:
        event.remove(db, "before_cursor_execute", record_statement)
    assert len(statements) == 2
    assert [row["id"] for row in rows] == [str(person.id) for person in people[:2]]
    assert rows[0]["productions"] == ["Hamlet · Theater (Berlin)", "Ohne Theater · Theater nicht hinterlegt"]
    assert rows[1]["productions"] == []
    assert [row["id"] for row in search_people_for_merge("Person", limit=2, offset=2)] == [
        str(person.id) for person in people[2:4]
    ]
    assert search_people_for_merge("%") == []
    assert search_people_for_merge(str(people[0].id))[0]["id"] == str(people[0].id)
    assert count(db, ReconciliationCase) == 0


def test_people_merge_table_includes_text_credits_without_duplicate_productions(db) -> None:
    person, author, text, theatre = save(
        db,
        Person(name="Author and director"),
        Person(name="Author only"),
        Text(title="Text"),
        Organization(name="Theater"),
    )
    show = save(db, Production(name="Hamlet", organization_id=theatre.id, text_id=text.id))[0]
    save(
        db,
        TextContribution(person_id=person.id, text_id=text.id, role="author"),
        TextContribution(person_id=person.id, text_id=text.id, role="adapter"),
        TextContribution(person_id=author.id, text_id=text.id, role="author"),
        ProductionMembership(person_id=person.id, production_id=show.id, role="director"),
    )
    assert [row["productions"] for row in search_people_for_merge()] == [["Hamlet · Theater"], ["Hamlet · Theater"]]


def test_bulk_people_merge_closes_pending_pairs_within_selection(db) -> None:
    left, right, third = save(db, Person(name="One"), Person(name="Two"), Person(name="Three"))
    old_id = uuid.uuid4()
    save(db, EntityRedirect(entity_type=EntityType.PERSON, old_id=old_id, target_id=right.id))
    case = queue_duplicate("person", right.id, third.id, reason="Same")
    with Session(db) as session:
        stored = session.get(ReconciliationCase, case.id)
        assert stored is not None
        stored.target_id = old_id
        session.add(stored)
        session.commit()
    ids = [left.id, right.id, third.id]
    preview = preview_people_merge(ids, target_id=left.id)
    merge_people(ids, target_id=left.id, revision=preview["revision"], actor_id="test")
    assert list_cases(status="pending") == []
    with Session(db) as session:
        stored = session.get(ReconciliationCase, case.id)
        assert stored is not None
        assert stored.status == "approved"
        assert stored.target_id == left.id
        assert stored.context["bulk_merge"]["revision"] == preview["revision"]


def test_bulk_people_merge_rewires_every_reference_and_preserves_audit(db, source) -> None:
    left, right, third, org, text, prod, award = save(
        db,
        Person(name="Same", description="Keep", type=["director"]),
        Person(name="Same", description="Other", type=["actor"], urls=["https://person.example"]),
        Person(name="Variant", description="Chosen", type=["author"], nationality="DE"),
        Organization(name="Theater"),
        Text(title="Text"),
        Production(name="Show"),
        Award(name="Award"),
    )
    alias = uuid.uuid4()
    save(db, EntityRedirect(entity_type=EntityType.PERSON, old_id=alias, target_id=third.id))
    pending = queue_duplicate("person", left.id, right.id, reason="Already reported")
    for person in (right, third):
        save(
            db,
            ProductionMembership(person_id=person.id, production_id=prod.id, role="director"),
            TextContribution(person_id=person.id, text_id=text.id, role="author"),
            OrganizationMembership(person_id=person.id, organization_id=org.id, role="actor"),
            AwardNomination(
                person_id=person.id,
                award_id=award.id,
                category="Actor",
                date=dt.datetime(2026, 1, 1),
                date_precision="year",
                result=AwardResult.WON,
            ),
            EntitySource(
                source_id=source.id, entity_type=EntityType.PERSON, entity_id=person.id, external_id=str(person.id)
            ),
            SourceRecord(
                source_id=source.id, entity_type=EntityType.PERSON, entity_id=person.id, record_key=str(person.id)
            ),
        )
    ids = [str(person.id) for person in (left, right, third)]
    preview = preview_people_merge(ids, target_id=left.id)
    assert count(db, Person) == 3
    assert count(db, SCD2History) == 0
    result = merge_people(
        ids,
        target_id=left.id,
        revision=preview["revision"],
        actor_id="test",
        choices={"description": str(third.id), "name": str(left.id)},
    )
    assert result == left.id
    with Session(db) as session:
        survivor = session.exec(select(Person)).one()
        assert survivor.description == "Chosen"
        assert survivor.nationality == "DE"
        assert set(survivor.type) == {"director", "actor", "author"}
        assert survivor.urls == ["https://person.example"]
        for model in (ProductionMembership, TextContribution, OrganizationMembership, AwardNomination):
            assert session.exec(select(model)).one().person_id == left.id
        assert all(record.entity_id == left.id for record in session.exec(select(SourceRecord)).all())
        assert session.exec(select(EntitySource)).one().entity_id == left.id
        assert resolve_id(session, "person", alias) == left.id
        assert resolve_id(session, "person", right.id) == left.id
        assert resolve_id(session, "person", third.id) == left.id
        case = session.get(ReconciliationCase, pending.id)
        assert case is not None
        assert case.status == "approved"
    decisions = list_cases(status="approved")
    assert len(decisions) == 2
    assert {case["context"]["decision"]["duplicate"]["record"]["id"] for case in decisions} == set(ids[1:])
    assert count(db, SCD2History) > 0


def test_bulk_people_merge_allows_shared_source_identity_within_selection(db, source) -> None:
    people = save(db, *(Person(name="Same") for _ in range(3)))
    for person in people:
        save(
            db,
            EntitySource(source_id=source.id, entity_type=EntityType.PERSON, entity_id=person.id, external_id="same"),
        )
    ids = [person.id for person in people]
    preview = preview_people_merge(ids, target_id=ids[1])
    merge_people(ids, target_id=ids[1], revision=preview["revision"], actor_id="test")
    assert count(db, Person) == 1
    with Session(db) as session:
        mapping = session.exec(select(SourceRecord)).one()
        assert mapping.entity_id == ids[1]
        assert {entry["entity_id"] for entry in mapping.payload["merged_sources"]} == {str(value) for value in ids}


def test_bulk_people_merge_failure_rolls_back_earlier_merges(db, source) -> None:
    left, right, third, outsider = save(
        db,
        Person(id=uuid.UUID(int=1), name="One"),
        Person(id=uuid.UUID(int=2), name="Two", nationality="DE"),
        Person(id=uuid.UUID(int=3), name="Three"),
        Person(name="Other"),
    )
    save(
        db,
        EntitySource(source_id=source.id, entity_type=EntityType.PERSON, entity_id=third.id, external_id="collision"),
        SourceRecord(
            source_id=source.id,
            entity_type=EntityType.PERSON,
            entity_id=outsider.id,
            record_key="id:collision",
            external_id="collision",
        ),
    )
    ids = [left.id, right.id, third.id]
    preview = preview_people_merge(ids, target_id=left.id)
    with pytest.raises(ValueError, match="third entity"):
        merge_people(ids, target_id=left.id, revision=preview["revision"], actor_id="test")
    assert count(db, Person) == 4
    assert count(db, EntityRedirect) == 0
    assert count(db, ReconciliationCase) == 0
    assert count(db, SCD2History) == 0
    assert get_entity_graph(Person, str(left.id))["nationality"] is None


@pytest.mark.parametrize("change", ["person", "reference", "source"])
def test_bulk_people_merge_rejects_stale_preview(db, source, change) -> None:
    left, right, prod = save(db, Person(name="Same"), Person(name="Same"), Production(name="Show"))
    ids = [left.id, right.id]
    preview = preview_people_merge(ids, target_id=left.id)
    if change == "person":
        with Session(db) as session:
            person = session.get(Person, right.id)
            assert person is not None
            person.description = "Concurrent edit"
            session.add(person)
            session.commit()
    elif change == "reference":
        save(db, ProductionMembership(person_id=right.id, production_id=prod.id))
    else:
        save(db, EntitySource(source_id=source.id, entity_type=EntityType.PERSON, entity_id=right.id))
    with pytest.raises(ValueError, match="Data changed"):
        merge_people(ids, target_id=left.id, revision=preview["revision"], actor_id="test")
    assert count(db, Person) == 2
    assert count(db, ReconciliationCase) == 0


def test_bulk_people_merge_validates_selection_and_choices(db) -> None:
    left, right = save(db, Person(name="Left"), Person(name="Right"))
    for ids, target in [
        ([left.id], left.id),
        ([left.id, left.id], left.id),
        ([left.id, right.id], uuid.uuid4()),
        ([left.id, uuid.uuid4()], left.id),
    ]:
        with pytest.raises(ValueError, match=r"Select|no longer exist"):
            preview_people_merge(ids, target_id=target)
    ids = [left.id, right.id]
    preview = preview_people_merge(ids, target_id=left.id)
    for choices in [{"id": str(right.id)}, {"name": str(uuid.uuid4())}]:
        with pytest.raises(ValueError, match="Selected fields"):
            merge_people(ids, target_id=left.id, revision=preview["revision"], actor_id="test", choices=choices)
    assert count(db, Person) == 2
    assert count(db, ReconciliationCase) == 0


def _management_app(kind: str) -> AppTest:
    return AppTest.from_file(
        str(REVIEW_APP.parent / ("persons.py" if kind == "person" else "texts.py")), default_timeout=10
    ).run()


def _select_rows(app: AppTest, rows: list[int]) -> AppTest:
    app.session_state[app.dataframe[0].proto.id.rsplit("-", 1)[1]] = {
        "edited_rows": {index: {"Auswahl": index in rows} for index in range(len(app.dataframe[0].value))},
        "added_rows": [],
        "deleted_rows": [],
    }
    return app.run()


def _edit_cell(app: AppTest, row: int, field: str, value: str | None) -> AppTest:
    app.session_state[app.dataframe[0].proto.id.rsplit("-", 1)[1]] = {
        "edited_rows": {row: {field: value}},
        "added_rows": [],
        "deleted_rows": [],
    }
    return app.run()


@pytest.mark.parametrize(
    "kind,model,payload,field,value,expected",
    [
        ("person", Person, {"name": "Person"}, "born", "1980-06", dt.datetime(1980, 6, 1)),
        ("person", Person, {"name": "Person"}, "died", "2020", dt.datetime(2020, 1, 1)),
        ("person", Person, {"name": "Person"}, "nationality", "DE", "DE"),
        ("text", Text, {"title": "Text"}, "published", "2001-02-03", dt.datetime(2001, 2, 3)),
        ("text", Text, {"title": "Text"}, "language", "de", "de"),
    ],
)
def test_inline_table_autosaves_simple_fields(db, kind, model, payload, field, value, expected) -> None:
    entity = save(db, model(**payload))[0]
    app = _management_app(kind)
    _edit_cell(app, 0, field, value)
    assert not app.exception and not app.error
    with Session(db) as session:
        saved = session.get(model, entity.id)
        assert saved is not None and getattr(saved, field) == expected
        if field in model.date_fields:
            assert (
                getattr(saved, f"{field}_precision")
                == {
                    "born": "month",
                    "died": "year",
                    "published": "day",
                }[field]
            )
    assert count(db, SCD2History) == 1
    app.run()
    assert count(db, SCD2History) == 1
    _edit_cell(app, 0, field, "")
    assert not app.exception and not app.error
    with Session(db) as session:
        saved = session.get(model, entity.id)
        assert saved is not None and getattr(saved, field) is None
        if field in model.date_fields:
            assert getattr(saved, f"{field}_precision") is None


def test_inline_table_rejects_invalid_dates_and_preserves_selection(db) -> None:
    entity = save(db, Person.model_validate({"name": "Person", "born": "1980"}))[0]
    app = _management_app("person")
    _select_rows(app, [0])
    _edit_cell(app, 0, "born", "invalid")
    assert not app.exception
    assert any("Automatisches Speichern fehlgeschlagen" in error.value for error in app.error)
    with Session(db) as session:
        saved = session.get(Person, entity.id)
        assert saved is not None and saved.born == dt.datetime(1980, 1, 1) and saved.born_precision == "year"
    assert count(db, SCD2History) == 0
    _edit_cell(app, 0, "born", "1980-06")
    assert not app.exception and not app.error
    assert any(button.label == "Save" for button in app.button)


def test_inline_table_column_order_and_readonly_names(db) -> None:
    save(db, Person(name="Person"))
    app = _management_app("person")
    assert list(app.dataframe[0].proto.column_order)[:2] == ["Auswahl", "name"]
    config = json.loads(app.dataframe[0].proto.columns)
    assert config["name"]["disabled"]
    assert not config["born"].get("disabled", False)
    assert config["type"]["disabled"]
    _edit_cell(app, 0, "name", "Not allowed")
    assert not app.exception
    with Session(db) as session:
        assert session.exec(select(Person)).one().name == "Person"


@pytest.mark.parametrize(
    "kind,model,payload",
    [
        ("person", Person, {"name": "Same"}),
        ("text", Text, {"title": "Same"}),
    ],
)
def test_entity_table_merges_without_dialog_when_no_overwrites(db, kind, model, payload) -> None:
    save(db, model(**payload), model(**payload))
    app = _management_app(kind)
    assert not app.exception
    assert app.button(key=f"merge_{kind}").disabled
    assert app.dataframe[0].proto.editing_mode != 0
    _select_rows(app, [0, 1])
    assert not app.button(key=f"merge_{kind}").disabled
    app.button(key=f"merge_{kind}").click().run()
    assert not app.exception and not app.error
    assert count(db, model) == 1
    assert f"merge_preview_{kind}" not in app.session_state
    assert app.button(key=f"merge_{kind}").disabled
    assert not any(button.label == "Zusammenführung speichern" for button in app.button)


@pytest.mark.parametrize("kind,model,label", [("person", Person, "name"), ("text", Text, "title")])
def test_entity_table_conflict_dialog_choices_survivor_and_cancel(db, kind, model, label) -> None:
    records = save(db, model(**{label: "One"}), model(**{label: "Two"}))
    app = _management_app(kind)
    _select_rows(app, [0, 1])
    app.button(key=f"merge_{kind}").click().run()
    assert not app.exception
    assert count(db, model) == 2
    assert next(button for button in app.button if button.label == "Zusammenführung speichern").disabled
    app.selectbox(key=f"merge_survivor_{kind}").select(str(records[1].id)).run()
    assert not app.exception
    preview = app.session_state[f"merge_preview_{kind}"]
    app.selectbox(key=f"merge_choice_{preview['revision']}_{label}").select(str(records[0].id)).run()
    next(button for button in app.button if button.label == "Abbrechen").click().run()
    assert not app.exception
    assert count(db, model) == 2
    assert f"merge_preview_{kind}" not in app.session_state
    app.button(key=f"merge_{kind}").click().run()
    app.selectbox(key=f"merge_survivor_{kind}").select(str(records[1].id)).run()
    preview = app.session_state[f"merge_preview_{kind}"]
    app.selectbox(key=f"merge_choice_{preview['revision']}_{label}").select(str(records[0].id)).run()
    next(button for button in app.button if button.label == "Zusammenführung speichern").click().run()
    assert not app.exception and not app.error
    assert count(db, model) == 1
    with Session(db) as session:
        survivor = session.exec(select(model)).one()
        assert survivor.id == records[1].id and getattr(survivor, label) == "One"


def test_entity_dialog_rejects_stale_sources_and_clears_selection(db, source) -> None:
    people = save(db, Person(name="One"), Person(name="Two"))
    app = _management_app("person")
    _select_rows(app, [0, 1])
    app.button(key="merge_person").click().run()
    preview = app.session_state["merge_preview_person"]
    save(db, EntitySource(source_id=source.id, entity_type=EntityType.PERSON, entity_id=people[0].id))
    app.selectbox(key=f"merge_choice_{preview['revision']}_name").select(str(people[0].id)).run()
    next(button for button in app.button if button.label == "Zusammenführung speichern").click().run()
    assert not app.exception
    assert any("Data changed" in error.value for error in app.error)
    assert count(db, Person) == 2
    _select_rows(app, [])
    assert "merge_preview_person" not in app.session_state
    assert app.button(key="merge_person").disabled


def test_text_merge_rewires_productions_contributions_lineages_and_sources(db, source) -> None:
    texts = save(db, Text(title="Same"), Text(title="Same", language="de"), Text(title="Same"))
    author, other, production = save(
        db, Person(name="Author"), Text(title="Other"), Production(name="Show", text_id=texts[1].id)
    )
    for text in texts[1:]:
        save(
            db,
            TextContribution(text_id=text.id, person_id=author.id, role="author"),
            TextLineage(source_text_id=text.id, derived_text_id=other.id, relation="adaptation", notes="Keep"),
            TextLineage(source_text_id=other.id, derived_text_id=text.id, relation="translation"),
            EntitySource(source_id=source.id, entity_type=EntityType.TEXT, entity_id=text.id, external_id=str(text.id)),
        )
    save(db, TextLineage(source_text_id=texts[1].id, derived_text_id=texts[2].id, relation="adaptation"))
    alias = uuid.uuid4()
    save(db, EntityRedirect(entity_type=EntityType.TEXT, old_id=alias, target_id=texts[2].id))
    ids = [text.id for text in texts]
    preview = preview_entity_merge("text", ids, target_id=texts[0].id)
    merge_entities("text", ids, target_id=texts[0].id, revision=preview["revision"], actor_id="test")
    with Session(db) as session:
        stored_production = session.get(Production, production.id)
        assert stored_production is not None and stored_production.text_id == texts[0].id
        assert session.exec(select(TextContribution)).one().text_id == texts[0].id
        lineages = session.exec(select(TextLineage)).all()
        assert len(lineages) == 2
        assert all(lineage.source_text_id != lineage.derived_text_id for lineage in lineages)
        assert {lineage.notes for lineage in lineages} == {"Keep", None}
        assert session.exec(select(EntitySource)).one().entity_id == texts[0].id
        assert all(record.entity_id == texts[0].id for record in session.exec(select(SourceRecord)).all())
        assert resolve_id(session, "text", alias) == texts[0].id
        survivor = session.get(Text, texts[0].id)
        assert survivor is not None and survivor.language == "de"
    assert count(db, Text) == 2
    assert count(db, SCD2History) > 0


@pytest.mark.parametrize("kind,model,label", [("person", Person, "name"), ("text", Text, "title")])
def test_table_merge_requires_all_conflict_choices(db, kind, model, label) -> None:
    entities = save(db, model(**{label: "One"}), model(**{label: "Two"}))
    ids = [entity.id for entity in entities]
    preview = preview_entity_merge(kind, ids)
    with pytest.raises(ValueError, match="every conflicting field"):
        merge_entities(kind, ids, target_id=preview["target_id"], revision=preview["revision"], actor_id="test")
    assert count(db, model) == 2
    assert count(db, ReconciliationCase) == 0


def test_other_source_review_remains_and_theapolis_is_hidden(db) -> None:
    sources = save(db, Source(name="theapolis.de"), Source(name="Other"))
    for source in sources:
        propose_record("person", {"name": source.name}, source_id=source.id, external_id="1")
    app = AppTest.from_file(str(REVIEW_APP), default_timeout=10).run()
    assert not app.exception
    assert "Weitere Prüffälle" not in [tab.label for tab in app.tabs]
    assert "Andere Quellen & gemeldete Duplikate" in [tab.label for tab in app.tabs]
    review_selector = next(box for box in app.selectbox if box.label == "Prüffall")
    assert len(review_selector.options) == 1
    assert "Other" in review_selector.options[0]


@pytest.mark.parametrize(
    "kind,model,label,date",
    [
        ("person", Person, "name", "born"),
        ("text", Text, "title", "published"),
    ],
)
def test_table_merge_refines_compatible_dates_without_conflicts(db, kind, model, label, date) -> None:
    entities = save(
        db, model.model_validate({label: "Same", date: "1980"}), model.model_validate({label: "Same", date: "1980-06"})
    )
    ids = [entity.id for entity in entities]
    preview = preview_entity_merge(kind, ids)
    assert not any(field["conflict"] for field in preview["fields"])
    merge_entities(kind, ids, target_id=preview["target_id"], revision=preview["revision"], actor_id="test")
    with Session(db) as session:
        survivor = session.exec(select(model)).one()
        assert getattr(survivor, date) == dt.datetime(1980, 6, 1)
        assert getattr(survivor, f"{date}_precision") == "month"


def test_text_table_merge_rejects_stale_reference_atomically(db) -> None:
    texts = save(db, Text(title="Same"), Text(title="Same"))
    ids = [text.id for text in texts]
    preview = preview_entity_merge("text", ids)
    save(db, Production(name="Concurrent", text_id=texts[1].id))
    with pytest.raises(ValueError, match="Data changed"):
        merge_entities("text", ids, target_id=preview["target_id"], revision=preview["revision"], actor_id="test")
    assert count(db, Text) == 2
    assert count(db, SCD2History) == 0
