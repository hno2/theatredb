import datetime as dt
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
    preview_case,
    propose_record,
    queue_duplicate,
    resolve_id,
    search_entities,
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
        AwardNomination(award_id=award.id, person_id=right.id, category="Actor", year=2026, result=AwardResult.WON),
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
    left, right = save(db, Person(name="Same"), Person(name="Same"))
    app = AppTest.from_file(str(REVIEW_APP)).run()
    assert not app.exception
    assert app.title[0].value == "Datenabgleich & Duplikate"
    app.selectbox(key="duplicate_kind").set_value("person").run()
    app.selectbox(key="duplicate_left").set_value(str(left.id)).run()
    app.selectbox(key="duplicate_right").set_value(str(right.id)).run()
    app.text_input(key="duplicate_reason").set_value("Same person").run()
    next(button for button in app.button if button.label == "Duplikat zur Prüfung einreichen").click().run()
    assert not app.exception
    assert count(db, ReconciliationCase) == 1
    assert count(db, Person) == 2
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


def test_merge_keeps_dictionary_lists_and_all_decision_snapshots(db) -> None:
    venue = {"name": "Main", "seats": "100"}
    left, right = save(
        db,
        Organization(name="Theatre", venues=[venue]),
        Organization(name="Theatre", venues=[venue, {"name": "Studio", "seats": "40"}]),
    )
    case = queue_duplicate("organization", left.id, right.id, reason="Same theatre")
    approve(case)
    with Session(db) as session:
        current = session.get(Organization, left.id)
        assert current is not None
        assert len(current.venues) == 2
        current_case = session.get(ReconciliationCase, case.id)
        assert current_case is not None
        decision = current_case.context["decision"]
        assert decision["duplicate"]["record"]["id"] == str(right.id)
        assert decision["before"]["venues"] == [venue]


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
            award_id=award.id, production_id=prod_right.id, category="Show", year=2026, result=AwardResult.WON
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
                award_id=award.id, person_id=person.id, category="Actor", year=2026, result=AwardResult.WON
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
