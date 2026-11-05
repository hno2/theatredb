import datetime as dt
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.entity_management import get_entity_graph
from backend.ingestion.staged_to_db import import_staged_productions, stage_records
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
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


def save(db, *entities):
    with Session(db) as session:
        for entity in entities:
            session.add(entity)
            session.flush()
        session.commit()
        for entity in entities:
            session.refresh(entity)
    return entities


def count(db, model):
    with Session(db) as session:
        return len(session.exec(select(model)).all())


def approve(case, target=None, **kwargs):
    preview = preview_case(case.id, target_id=target)
    return approve_case(case.id, revision=preview["revision"], target_id=target, actor_id="test", **kwargs)


@pytest.fixture
def source(db):
    return save(db, Source(name="theatre.example"))[0]


def test_staging_only_creates_cases_and_is_idempotent(db, source):
    records = [
        {
            "title": "Hamlet",
            "url": "https://theatre.example/p/1/",
            "premiere": "2026-02-14",
            "author": "William Shakespeare",
            "memberships": [
                {"name": "Director One", "type": "director", "role": "Regie"},
                {"name": "Actor One", "type": "actor", "role": "Hamlet", "description": "Bio"},
            ],
        }
    ]
    kwargs = {"source_name": source.name, "organization_name": "Theatre", "actor_id": "test", "captured_at": CAPTURE}
    result = stage_records(records, **kwargs)
    assert result == stage_records(records, **kwargs)
    assert count(db, ReconciliationCase) == 6
    assert all(count(db, model) == 0 for model in ENTITY_MODELS.values())
    production_case = result["production"][0]
    assert preview_case(production_case)["blocked"]
    for kind in ("organization", "person", "text", "production"):
        for case_id in result[kind]:
            preview = preview_case(case_id)
            approve_case(case_id, revision=preview["revision"], actor_id="test", allow_create=True)
    assert count(db, Production) == 1
    assert count(db, Person) == 3
    assert count(db, TextContribution) == 1
    with Session(db) as session:
        actor = session.exec(select(ProductionMembership).where(ProductionMembership.role == "actor")).one()
        assert actor.character == "Hamlet"
        assert session.exec(select(Person).where(Person.name == "Actor One")).one().description == "Bio"
    history = count(db, SCD2History)
    assert stage_records(records, **kwargs) == result
    assert count(db, SCD2History) == history
    assert not list_cases()


def test_staging_current_person_and_production_fields_survive_review(db, source):
    cases = stage_records(
        [
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
                        "died": "2025-03-04",
                        "nationality": "Example",
                    },
                    {"name": "Actor", "type": "actor", "role": "source label", "character": "Hamlet"},
                ],
            }
        ],
        source_name=source.name,
        organization_name="Theatre",
        actor_id="test",
    )
    for kind in ("organization", "person", "text", "production"):
        for case_id in cases[kind]:
            preview = preview_case(case_id)
            approve_case(case_id, revision=preview["revision"], actor_id="test", allow_create=True)
    with Session(db) as session:
        person = session.exec(select(Person).where(Person.name == "Multi-role Person")).one()
        assert person.type == ["actor", "director"]
        assert person.born == dt.date(1980, 1, 2)
        assert person.died == dt.date(2025, 3, 4)
        assert person.nationality == "Example"
        assert person.urls == ["https://theatre.example/people/1", "https://profile.example/1"]
        production = session.exec(select(Production)).one()
        assert production.tags == ["drama"]
        assert production.critics == ["https://reviews.example/current"]
        assert (
            session.exec(select(ProductionMembership).where(ProductionMembership.person_id == person.id)).one().role
            == "director"
        )
        assert (
            session.exec(select(ProductionMembership).where(ProductionMembership.role == "actor")).one().character
            == "Hamlet"
        )


def test_staging_validates_every_record_before_creating_source_or_cases(db):
    with pytest.raises(ValueError):
        stage_records(
            [
                {"title": "Valid", "url": "https://theatre.example/1"},
                {
                    "title": "Invalid",
                    "url": "https://theatre.example/2",
                    "memberships": [
                        {"name": "Person", "url": "javascript:alert(1)"},
                    ],
                },
            ],
            source_name="new-source",
            organization_name="Theatre",
            actor_id="test",
        )
    assert count(db, Source) == 0
    assert count(db, ReconciliationCase) == 0


def test_production_matching_requires_org_and_directors(db):
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


def test_premiere_conflict_and_missing_dates(db):
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


def test_update_preserves_conflicts_empty_fields_and_unions_lists(db, source):
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


def test_explicit_overwrite_and_invalid_field_selection(db, source):
    person = save(db, Person(name="Person", description="Old"))[0]
    case = propose_record("person", {"name": "Person", "description": "New"}, source_id=source.id, external_id="1")
    with pytest.raises(ValueError, match="Selected fields"):
        approve(case, person.id, fields=["id"])
    approve(case, person.id, fields=["description"])
    assert get_entity_graph(Person, str(person.id))["description"] == "New"


def test_no_implicit_creation_and_source_cannot_be_reassigned(db, source):
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


def test_stale_preview_and_rejection_reopening(db, source):
    person = save(db, Person(name="Person"))[0]
    case = propose_record("person", {"name": "Person", "description": "New"}, source_id=source.id, external_id="1")
    preview = preview_case(case.id, target_id=person.id)
    with Session(db) as session:
        current = session.get(Person, person.id)
        current.description = "Other editor"
        session.add(current)
        session.commit()
    with pytest.raises(ValueError, match="Data changed"):
        approve_case(case.id, revision=preview["revision"], target_id=person.id, actor_id="test")
    decide_case(case.id, status="rejected", actor_id="test", note="Not the same person")
    assert len(list_cases(status="rejected")) == 1
    decide_case(case.id, status="pending", actor_id="test", note="Additional evidence")
    assert len(list_cases()) == 1


def test_older_source_snapshot_is_blocked(db, source):
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
def test_all_entity_kinds_merge_and_redirect(db, kind):
    model = ENTITY_MODELS[kind]
    label = "title" if kind == EntityType.TEXT else "name"
    left, right = save(db, model(**{label: "Same"}), model(**{label: "Same"}))
    case = queue_duplicate(kind, left.id, right.id, reason="Manual")
    assert queue_duplicate(kind, right.id, left.id, reason="Programmatic").id == case.id
    approve(case)
    with Session(db) as session:
        assert session.get(model, right.id) is None
        assert resolve_id(session, kind, right.id) == left.id
    assert count(db, model) == 1
    assert get_entity_graph(model, str(right.id))["id"] == str(left.id)


def test_person_merge_rewires_all_links_and_deduplicates_roles(db, source):
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


def test_merge_survivor_can_be_swapped(db):
    left, right = save(db, Person(name="Left"), Person(name="Right"))
    case = queue_duplicate("person", left.id, right.id, reason="Manual")
    approve(case, right.id)
    with Session(db) as session:
        assert session.get(Person, left.id) is None
        assert resolve_id(session, "person", left.id) == right.id


def test_text_merge_rewires_productions_and_both_lineage_directions(db):
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
        assert session.get(Production, prod.id).text_id == left.id
        lineages = session.exec(select(TextLineage)).all()
        assert len(lineages) == 2
        assert all(right.id not in {r.source_text_id, r.derived_text_id} for r in lineages)


def test_merge_failure_rolls_back_everything(db, source):
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


def test_person_and_text_title_only_matches_remain_review(db):
    author, work, person = save(db, Person(name="Author"), Text(title="Hamlet"), Person(name="Namesake"))
    save(db, TextContribution(text_id=work.id, person_id=author.id, role="author"))
    assert find_candidates("person", {"name": person.name})[0]["recommendation"] == "review"
    assert find_candidates("text", {"title": "Hamlet"})[0]["recommendation"] == "review"
    assert (
        find_candidates("text", {"title": "Hamlet"}, context={"authors": ["Author"]})[0]["recommendation"] == "strong"
    )
    assert find_candidates("text", {"title": "Hamlet"}, context={"authors": ["Other"]})[0]["conflicts"]


def test_search_manual_pair_with_different_names_and_discovery(db):
    left, right = save(db, Organization(name="Same", city="Berlin"), Organization(name="Same", city="Hamburg"))
    assert len(search_entities("organization", "Same")) == 2
    assert search_entities("organization", str(left.id))[0]["id"] == str(left.id)
    assert search_entities("organization", "%") == []
    pairs = discover_duplicates("organization", "Same")
    assert len(pairs) == 1
    assert "Abweichend: city" in pairs[0]["right"]["conflicts"]
    case = queue_duplicate("organization", left.id, right.id, reason="Manual investigation")
    assert case.status == "pending"


def test_invalid_payloads_do_not_create_records(db, source):
    with pytest.raises(ValueError, match="protected"):
        propose_record("person", {"name": "Person", "id": str(uuid.uuid4())}, source_id=source.id, external_id="1")
    with pytest.raises(ValueError, match="Invalid source URL"):
        propose_record("person", {"name": "Person"}, source_id=source.id, external_url="javascript:bad")
    with pytest.raises(ValueError, match="stable"):
        propose_record("person", {"name": "Person"}, source_id=source.id)
    assert count(db, ReconciliationCase) == 0


def test_import_requires_organization_and_infers_capture_date(db, tmp_path):
    path = tmp_path / "20260607_productions.json"
    path.write_text('[{"title": "Work", "url": "https://theatre.example/1"}]')
    with pytest.raises(ValueError, match="organization-name"):
        import_staged_productions(json_path=path, actor_id="test")
    result = import_staged_productions(json_path=path, actor_id="test", organization_name="Theatre")
    case = preview_case(result["production"][0])["case"]
    assert case["captured_at"].startswith("2026-06-07")


@pytest.mark.parametrize(
    ("raw", "expected"), [("Regie", "director"), ("Inszenierung", "director"), ("creator", "author")]
)
def test_canonical_roles(raw, expected):
    assert canonical_role(raw) == expected


def test_review_ui_empty_and_manual_submission(db):
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


def test_staged_enrichment_reuses_existing_theapolis_graph(db, source):
    org, director, author, text = save(
        db,
        Organization(name="Theatre"),
        Person(name="Director", type=["director"]),
        Person(name="Author", type=["author"]),
        Text(title="Hamlet"),
    )
    prod = save(db, Production(name="Hamlet", organization_id=org.id, text_id=text.id))[0]
    save(
        db,
        ProductionMembership(production_id=prod.id, person_id=director.id, role="director"),
        ProductionMembership(production_id=prod.id, person_id=author.id, role="creator"),
        TextContribution(text_id=text.id, person_id=author.id, role="author"),
    )
    records = [
        {
            "title": "Hamlet",
            "url": "https://theatre.example/p/1",
            "description": "Official description",
            "premiere": "2026-02-14",
            "author": "Author",
            "memberships": [{"name": "Director", "type": "director", "role": "Regie"}],
        }
    ]
    cases = stage_records(
        records, source_name=source.name, organization_name="Theatre", actor_id="test", captured_at=CAPTURE
    )
    identities = {"organization": org.id, "text": text.id, "production": prod.id}
    for kind in ("organization", "person", "text", "production"):
        for case_id in cases[kind]:
            preview = preview_case(case_id)
            if kind == "person":
                target = director.id if preview["incoming"]["name"] == "Director" else author.id
            else:
                target = identities[kind]
            preview = preview_case(case_id, target_id=target)
            if kind == "production":
                assert preview["candidates"][0]["recommendation"] == "strong"
            approve_case(case_id, revision=preview["revision"], target_id=target, actor_id="test")
    assert count(db, Organization) == 1
    assert count(db, Person) == 2
    assert count(db, Text) == 1
    assert count(db, Production) == 1
    assert count(db, ProductionMembership) == 2
    assert count(db, TextContribution) == 1
    data = get_entity_graph(Production, str(prod.id))
    assert data["description"] == "Official description"
    assert data["premiere"].startswith("2026-02-14")


def test_matching_uses_approved_person_identity_not_scraped_spelling(db, source):
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


def test_merge_keeps_dictionary_lists_and_all_decision_snapshots(db):
    venue = {"name": "Main", "seats": "100"}
    left, right = save(
        db,
        Organization(name="Theatre", venues=[venue]),
        Organization(name="Theatre", venues=[venue, {"name": "Studio", "seats": "40"}]),
    )
    case = queue_duplicate("organization", left.id, right.id, reason="Same theatre")
    approve(case)
    with Session(db) as session:
        assert len(session.get(Organization, left.id).venues) == 2
        decision = session.get(ReconciliationCase, case.id).context["decision"]
        assert decision["duplicate"]["record"]["id"] == str(right.id)
        assert decision["before"]["venues"] == [venue]


def test_org_and_production_merges_rewire_graph(db):
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
        assert session.get(Production, prod_left.id).organization_id == left.id
        assert session.exec(select(OrganizationMembership)).one().organization_id == left.id
        assert session.exec(select(ProductionMembership)).one().production_id == prod_left.id
        assert session.exec(select(AwardNomination)).one().production_id == prod_left.id


def test_award_merge_deduplicates_identical_nominations(db):
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


def test_stale_merge_preview_includes_relationship_changes(db):
    left, right, prod = save(db, Person(name="Same"), Person(name="Same"), Production(name="Work"))
    case = queue_duplicate("person", left.id, right.id, reason="Same person")
    preview = preview_case(case.id)
    save(db, ProductionMembership(production_id=prod.id, person_id=right.id, role="director"))
    with pytest.raises(ValueError, match="Data changed"):
        approve_case(case.id, revision=preview["revision"], actor_id="test")
    assert count(db, Person) == 2
    assert count(db, EntityRedirect) == 0


def test_legacy_actor_character_credit_is_updated_not_duplicated(db, source):
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
        assert (row.role, row.character) == ("actor", "Hamlet")
        assert count(db, ProductionMembership) == 1


def test_redirect_chain_and_unrelated_merge_target(db):
    first, second, third = save(db, Person(name="One"), Person(name="Two"), Person(name="Three"))
    case = queue_duplicate("person", first.id, second.id, reason="Same")
    with pytest.raises(ValueError, match="submitted pair"):
        preview_case(case.id, target_id=third.id)
    approve(case)
    approve(queue_duplicate("person", third.id, first.id, reason="Same"))
    with Session(db) as session:
        assert resolve_id(session, "person", second.id) == third.id
        assert resolve_id(session, "person", first.id) == third.id


def test_queue_search_pagination_and_duplicate_discovery_do_not_write(db, source):
    for index in range(3):
        propose_record("person", {"name": f"Person {index}"}, source_id=source.id, external_id=str(index))
    cases = list_cases(query="Person 1")
    assert len(cases) == 1
    assert list_cases(query=cases[0]["id"])[0]["id"] == cases[0]["id"]
    assert len(list_cases(limit=2)) == 2
    assert len(list_cases(limit=2, offset=2)) == 1
    assert count(db, Person) == 0
    assert discover_duplicates("person") == []


def test_capture_time_is_normalized_to_utc(db, source):
    local = dt.datetime(2026, 6, 7, 2, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    first = propose_record("person", {"name": "Person"}, source_id=source.id, external_id="1", captured_at=local)
    again = propose_record("person", {"name": "Person"}, source_id=source.id, external_id="1", captured_at=CAPTURE)
    assert first.id == again.id


def test_review_ui_merges_only_after_explicit_confirmation(db):
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


def test_review_ui_preserves_stale_preview_until_refreshed(db, source):
    person = save(db, Person(name="Person"))[0]
    case = propose_record(
        "person", {"name": "Person", "description": "Source bio"}, source_id=source.id, external_id="1"
    )
    app = AppTest.from_file(str(REVIEW_APP)).run()
    app.selectbox(key=f"target_{case.id}").set_value(str(person.id)).run()
    next(box for box in app.checkbox if box.label.startswith("Identität")).check().run()
    with Session(db) as session:
        row = session.get(Person, person.id)
        row.description = "Concurrent edit"
        session.add(row)
        session.commit()
    next(button for button in app.button if button.label == "Genehmigen und speichern").click().run()
    assert not app.exception
    assert any("Data changed" in error.value for error in app.error)
    assert get_entity_graph(Person, str(person.id))["description"] == "Concurrent edit"
    assert list_cases()[0]["status"] == "pending"


def test_duplicate_merge_allows_same_source_identity_on_submitted_pair(db, source):
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


def test_pending_merge_can_choose_survivor_after_other_merge_redirects(db):
    left, right, third = save(db, Person(name="Left"), Person(name="Right"), Person(name="Third"))
    pending = queue_duplicate("person", left.id, right.id, reason="Same person")
    approve(queue_duplicate("person", third.id, right.id, reason="Same person"))
    approve(pending, third.id)
    with Session(db) as session:
        assert resolve_id(session, "person", left.id) == third.id
        assert resolve_id(session, "person", right.id) == third.id
