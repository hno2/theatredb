import datetime as dt
from collections.abc import Iterator
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from backend import config, user_management
from backend.data_quality import (
    QUALITY_FIELDS,
    QUALITY_LABELS,
    apply_quality_action,
    public_summary,
    season_checks_many,
    season_label,
)
from backend.entity_management import (
    catalog_page,
    create_entity,
    delete_entity,
    get_organization_profile,
    get_people_productions,
    update_entity,
)
from backend.models import EntitySource, EntityType, Organization, Person, Production, ProductionMembership, Source
from backend.reconciliation import approve_case, preview_case, queue_duplicate, validate_payload
from backend.scd2 import SCD2History


@pytest.fixture
def db(monkeypatch, tmp_path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'quality.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


def save(db: Engine, *entities: Organization) -> tuple[Organization, ...]:
    with Session(db) as session:
        session.add_all(entities)
        session.commit()
        for entity in entities:
            session.refresh(entity)
    return entities


def test_summary_quality_precedence_and_rollover(monkeypatch) -> None:
    org = Organization(name="Theater")
    checks = []
    summary = public_summary(org, checks)
    assert summary["quality"] == "unverified"
    assert summary["color"] == [107, 114, 128, 200]
    assert season_label(1999) == "1999/00"
    assert QUALITY_LABELS["history"] == "Allgemeine Angaben und Historie geprüft"

    org.general_verified_at = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
    org.general_verified_by = "internal-user"
    org.general_verification_evidence = "Official site"
    assert public_summary(org, checks)["quality"] == "general"

    org.history_from_season = 1990
    org.history_through_season = 2025
    org.history_verified_at = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
    org.history_verified_by = "internal-user"
    org.history_verification_evidence = "Archive"
    history_only = Organization.model_validate(
        org.model_dump(exclude={"general_verified_at", "general_verified_by", "general_verification_evidence"})
    )
    assert public_summary(history_only, checks)["quality"] == "unverified"
    assert cast(str, public_summary(history_only, checks)["history_label"]).endswith("1990/91\u20132025/26")
    assert public_summary(org, checks)["quality"] == "history"
    org.update_scope = "productions"
    org.update_reason = "Current programme missing"
    assert public_summary(org, checks)["quality"] == "update"
    assert public_summary(org, checks)["color"] == [217, 119, 6, 200]

    check = {
        "season_start_year": 2026,
        "checked_at": "2026-11-19T10:00:00+00:00",
        "evidence": "https://example.org/programme",
        "checked_by": "private-reviewer",
    }
    summary = public_summary(org, [check])
    assert summary["season_current"] is True
    assert "Aktuelle Spielzeit geprüft" in cast(str, summary["season_label"])
    assert "private-reviewer" not in repr(summary)
    monkeypatch.setattr(config, "CURRENT_SEASON_START_YEAR", 2027)
    assert public_summary(org, [check])["season_current"] is False
    assert cast(str, public_summary(org, [check])["season_label"]).startswith("Nur frühere Spielzeiten geprüft")


def test_nested_public_productions_do_not_expose_organization_reviewers(db: Engine) -> None:
    organization = save(db, Organization(name="House"))[0]
    apply_quality_action(organization.id, "verify_general", actor_id="private-reviewer", evidence="Official site")
    with Session(db) as session:
        person = Person(name="Actor")
        production = Production(name="Play", organization_id=organization.id)
        session.add_all([person, production])
        session.flush()
        session.add(ProductionMembership(person_id=person.id, production_id=production.id, role_name="actor"))
        session.commit()
        person_id = str(person.id)
    discovery, _ = catalog_page(Production, "", [], limit=100)
    for data in (discovery, get_people_productions([person_id])):
        assert data[0]["organization"]["general_verified_at"]
        assert "general_verified_by" not in data[0]["organization"]
        assert "private-reviewer" not in repr(data)


def test_actions_validate_and_audit_full_snapshots(db: Engine) -> None:
    organization = save(db, Organization(name="House"))[0]
    invalid_actions = (
        lambda: apply_quality_action(organization.id, "verify_general", actor_id="editor", evidence="  "),
        lambda: apply_quality_action(
            organization.id, "flag_update", actor_id="editor", update_scope="both", update_reason=" "
        ),
        lambda: apply_quality_action(
            organization.id, "check_season", actor_id="editor", season_start_year=2027, evidence="archive"
        ),
        lambda: apply_quality_action(
            organization.id,
            "verify_history",
            actor_id="editor",
            history_from_season=2025,
            history_through_season=2024,
            evidence="archive",
        ),
        lambda: apply_quality_action(organization.id, "verify_general", actor_id=" ", evidence="source"),
    )
    for action in invalid_actions:
        with pytest.raises(ValueError):
            action()

    with pytest.raises(ValueError, match="Actor"):
        apply_quality_action(organization.id, "verify_general", actor_id=None, evidence="source")
    with Session(db) as session:
        unchanged = session.get(Organization, organization.id)
        assert unchanged and unchanged.general_verified_at is None

    apply_quality_action(organization.id, "verify_general", actor_id="editor", evidence="site")
    apply_quality_action(
        organization.id,
        "verify_history",
        actor_id="editor",
        evidence="archive",
        history_from_season=1990,
        history_through_season=2025,
    )
    apply_quality_action(organization.id, "check_season", actor_id="editor", season_start_year=2026, evidence="book")
    apply_quality_action(
        organization.id, "check_season", actor_id="editor", season_start_year=2026, evidence="updated book"
    )
    checks = season_checks_many([organization.id])
    assert len(checks[organization.id]) == 1
    assert checks[organization.id][0].evidence == "updated book"

    with Session(db) as session:
        events = session.exec(
            select(SCD2History).where(
                SCD2History.entity_name == "organizationseasoncheck",
                SCD2History.entity_id == str(checks[organization.id][0].id),
            )
        ).all()
        assert [event.operation for event in events] == ["insert", "update"]
        assert [event.version for event in events] == [1, 2]
        current_org = session.get(Organization, organization.id)
        assert current_org and current_org.history_verified_by == "editor"
    apply_quality_action(
        organization.id, "flag_update", actor_id="editor", update_scope="both", update_reason="Known gap"
    )
    apply_quality_action(organization.id, "resolve_update", actor_id="editor")
    with Session(db) as session:
        current_org = session.get(Organization, organization.id)
        assert current_org and current_org.history_verified_by == "editor"
        assert current_org.general_verified_by == "editor"
        assert current_org.update_scope is None


def test_get_if_exists_rejects_ambiguous_source_attachment(db: Engine) -> None:
    with Session(db) as session:
        source = Source(name="Publisher")
        session.add_all([source, Person(name="Twin"), Person(name="Twin")])
        session.commit()
        source_id = source.id

    with pytest.raises(ValueError, match="Multiple Person rows match"):
        create_entity(
            Person,
            payload={"name": "Twin"},
            actor_id="editor",
            get_if_exists={"name": "Twin"},
            source_links=[{"source_id": source_id}],
        )

    with Session(db) as session:
        assert not session.exec(select(EntitySource)).all()


def test_generic_crud_cannot_write_assessments_and_profile_hides_reviewers(db: Engine) -> None:
    organization = create_entity(Organization, payload={"name": "House"}, actor_id="editor")
    assert QUALITY_FIELDS
    with pytest.raises(ValueError, match="apply_quality_action"):
        update_entity(
            Organization,
            entity_id=str(organization.id),
            payload={"general_verified_at": dt.datetime.now(dt.UTC)},
            actor_id="editor",
        )
    with pytest.raises(ValueError, match="apply_quality_action"):
        create_entity(
            Organization,
            payload={
                "name": "Invalid",
                "general_verified_at": dt.datetime.now(dt.UTC),
                "general_verified_by": "editor",
                "general_verification_evidence": "site",
            },
            actor_id="editor",
        )
    with pytest.raises(ValueError, match="protected fields"):
        validate_payload(EntityType.ORGANIZATION, {"name": "Import", "update_scope": "both"}, complete=True)
    apply_quality_action(organization.id, "verify_general", actor_id="editor", evidence="site")
    apply_quality_action(
        organization.id,
        "verify_history",
        actor_id="history-editor",
        evidence="archive",
        history_from_season=1990,
        history_through_season=2025,
    )
    apply_quality_action(organization.id, "check_season", actor_id="editor", season_start_year=2026, evidence="book")
    with Session(db) as session:
        current = session.get(Organization, organization.id)
        assert current
        verified_at = current.general_verified_at
    update_entity(
        Organization,
        entity_id=str(organization.id),
        payload={"name": "Updated House"},
        actor_id="editor",
    )
    with Session(db) as session:
        updated = session.get(Organization, organization.id)
        assert updated and updated.general_verified_at == verified_at
    profile = get_organization_profile(str(organization.id))
    assert profile["season_checks"][0].keys() == {"season_start_year", "checked_at", "evidence"}
    assert "general_verified_by" not in profile
    assert "history_verified_by" not in profile
    assert "checked_by" not in profile["season_checks"][0]
    summary = public_summary(profile, profile["season_checks"])
    assert summary["quality"] == "history"
    assert "1990/91\u20132025/26" in summary["history_label"]
    assert "history-editor" not in repr(summary)
    assert not summary["update_reason"]


def test_deletion_audits_and_removes_season_checks(db: Engine) -> None:
    organization = save(db, Organization(name="House"))[0]
    check = apply_quality_action(
        organization.id,
        "check_season",
        actor_id="editor",
        season_start_year=2026,
        evidence="book",
    )
    assert check is not None
    delete_entity(Organization, entity_id=str(organization.id), actor_id="editor")
    assert season_checks_many([organization.id])[organization.id] == []
    with Session(db) as session:
        events = session.exec(
            select(SCD2History).where(
                SCD2History.entity_name == "organizationseasoncheck",
                SCD2History.entity_id == str(check.id),
            )
        ).all()
        assert [event.operation for event in events] == ["insert", "delete"]
        assert events[-1].actor_id == "editor"


def test_organization_merge_requires_assessment_choice_and_carries_separate_checks(db: Engine) -> None:
    target, duplicate = save(db, Organization(name="House"), Organization(name="House duplicate"))
    apply_quality_action(target.id, "verify_general", actor_id="one", evidence="source A")
    apply_quality_action(duplicate.id, "verify_general", actor_id="two", evidence="source B")
    apply_quality_action(target.id, "check_season", actor_id="one", season_start_year=2025, evidence="target archive")
    apply_quality_action(
        duplicate.id, "check_season", actor_id="two", season_start_year=2025, evidence="duplicate archive"
    )
    apply_quality_action(duplicate.id, "check_season", actor_id="two", season_start_year=2024, evidence="old archive")
    case = queue_duplicate(EntityType.ORGANIZATION, target.id, duplicate.id, reason="Duplicate", actor_id="editor")
    preview = preview_case(case.id, target_id=target.id)
    assert {row["key"] for row in preview["assessment_conflicts"]} == {"general", "season:2025"}
    assert not QUALITY_FIELDS.intersection(row["field"] for row in preview["fields"])
    assert preview["target_state"]["record"]["assessment:general"]["general_verification_evidence"] == "source A"
    assert preview["duplicate_state"]["record"]["assessment:season:2025"]["evidence"] == "duplicate archive"
    assert preview["assessment_result"]["season_checks"]["season:2024"]["evidence"] == "old archive"
    with pytest.raises(ValueError, match="Choose an assessment"):
        approve_case(case.id, revision=preview["revision"], target_id=target.id, actor_id="editor")
    approve_case(
        case.id,
        revision=preview["revision"],
        target_id=target.id,
        actor_id="editor",
        assessment_choices={"general": str(duplicate.id), "season:2025": str(duplicate.id)},
    )
    with Session(db) as session:
        merged = session.get(Organization, target.id)
        assert merged and merged.general_verification_evidence == "source B"
    checks = season_checks_many([target.id])[target.id]
    assert {check.season_start_year: check.evidence for check in checks} == {
        2024: "old archive",
        2025: "duplicate archive",
    }


def test_season_checks_batch_preserves_empty_ids(db: Engine) -> None:
    organization = save(db, Organization(name="House"))[0]
    missing_id = uuid4()
    result = season_checks_many([organization.id, missing_id])
    assert result == {organization.id: [], missing_id: []}


def test_quality_migration_preserves_existing_data_and_audit(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    org_id, production_id, audit_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    with engine.begin() as connection:
        migration = user_management.alembic_config(connection)
        command.upgrade(migration, "0006")
        connection.execute(
            text("INSERT INTO organization (id, name, city, lat, lon) VALUES (:id, 'House', 'Wien', 48.2, 16.4)"),
            {"id": org_id.hex},
        )
        connection.execute(
            text("INSERT INTO production (id, name, organization_id) VALUES (:id, 'Show', :org_id)"),
            {"id": production_id.hex, "org_id": org_id.hex},
        )
        connection.execute(
            text(
                "INSERT INTO scd2history (id, entity_name, entity_id, operation, version, valid_from, "
                "is_current, actor_id, snapshot) "
                "VALUES (:id, 'organization', :entity_id, 'insert', 1, '2026-01-01', 1, 'legacy', '{}')"
            ),
            {"id": audit_id.hex, "entity_id": str(org_id)},
        )
        command.upgrade(migration, "head")
        row = connection.execute(
            text(
                "SELECT name, address_locality, latitude, longitude, general_verified_at, history_from_season, update_scope "
                "FROM organization WHERE id = :id"
            ),
            {"id": org_id.hex},
        ).one()
        assert tuple(row) == ("House", "Wien", 48.2, 16.4, None, None, None)
        assert connection.execute(text("SELECT count(*) FROM organizationseasoncheck")).scalar_one() == 0
        assert connection.execute(text("SELECT organization_id FROM production")).scalar_one() == org_id.hex
        for update in (
            text("UPDATE organization SET general_verified_at = '2026-01-01' WHERE id = :id"),
            text("UPDATE organization SET history_from_season = 2000 WHERE id = :id"),
            text("UPDATE organization SET update_scope = 'both' WHERE id = :id"),
        ):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(update, {"id": org_id.hex})
    check = apply_quality_action(org_id, "check_season", actor_id="editor", season_start_year=2026, evidence="archive")
    assert check is not None
    with Session(engine) as session:
        history = session.exec(select(SCD2History).where(SCD2History.entity_name == "organizationseasoncheck")).one()
        assert history.actor_id == "editor"
        assert history.snapshot["id"] == str(check.id)
        assert history.snapshot["organization_id"] == str(org_id)
        assert history.snapshot["evidence"] == "archive"
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM organizationseasoncheck")).scalar_one() == 1
        assert (
            connection.execute(
                text("SELECT count(*) FROM scd2history WHERE entity_name = 'organizationseasoncheck'")
            ).scalar_one()
            == 1
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO organizationseasoncheck "
                    "(id, organization_id, season_start_year, checked_at, checked_by, evidence) "
                    "VALUES (:id, :org_id, 2026, '2026-01-01', 'editor', 'archive')"
                ),
                {"id": uuid4().hex, "org_id": org_id.hex},
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE organizationseasoncheck SET evidence = ' ' WHERE id = :id"), {"id": check.id.hex}
            )
        command.downgrade(user_management.alembic_config(connection), "0006")
        assert "organizationseasoncheck" not in inspect(connection).get_table_names()
        assert "general_verified_at" not in {
            column["name"] for column in inspect(connection).get_columns("organization")
        }
        assert (
            connection.execute(text("SELECT name FROM organization WHERE id = :id"), {"id": org_id.hex}).scalar_one()
            == "House"
        )
        assert connection.execute(text("SELECT organization_id FROM production")).scalar_one() == org_id.hex
        assert connection.execute(text("SELECT count(*) FROM scd2history")).scalar_one() == 2
    engine.dispose()
