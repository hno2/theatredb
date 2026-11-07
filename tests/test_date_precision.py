import datetime as dt
import uuid

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from backend import user_management
from backend.date_precision import DatePrecision, date_interval, format_date, parse_date
from backend.entity_management import update_entity
from backend.ingestion.staging import staging_schema, validate_records
from backend.models import (
    Award,
    AwardNomination,
    AwardResult,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    Source,
    Text,
)
from backend.production_import import ingest_records
from backend.reconciliation import approve_case, merge_people, preview_case, preview_people_merge, propose_record
from frontend.views.organization import filter_productions, premiere_label


@pytest.mark.parametrize(
    ("raw", "precision", "start", "end", "label"),
    [
        ("2026", "year", "2026-01-01T00:00", "2027-01-01T00:00", "2026"),
        ("2026-12", "month", "2026-12-01T00:00", "2027-01-01T00:00", "12.2026"),
        ("2024-02", "month", "2024-02-01T00:00", "2024-03-01T00:00", "02.2024"),
        ("2024-02-29", "day", "2024-02-29T00:00", "2024-03-01T00:00", "29.02.2024"),
        ("2026-01-31T23", "minute", "2026-01-31T23:00", "2026-01-31T23:01", "31.01.2026 23:00"),
        ("2026-12-31T23:59", "minute", "2026-12-31T23:59", "2027-01-01T00:00", "31.12.2026 23:59"),
    ],
)
def test_interval_and_format(raw, precision, start, end, label):
    value, actual = parse_date(raw)
    assert actual == DatePrecision(precision)
    assert date_interval(value, actual) == (dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end))
    assert format_date(value, actual) == label


def test_max_year_interval_is_unbounded():
    for raw in ("9999", "9999-12", "9999-12-31", "9999-12-31T23:59"):
        value, precision = parse_date(raw)
        assert date_interval(value, precision)[1] is None


@pytest.mark.parametrize(
    "raw",
    [
        "0000",
        "2026-00",
        "2026-13",
        "2025-02-29",
        "2026-04-31T09:00",
        "2026-01-01T24:00",
        "2026-01-01T09:60",
        "2026-01-01T09:00:60",
        2026,
    ],
)
def test_invalid_dates_rejected(raw):
    with pytest.raises(ValueError):
        parse_date(raw)


def test_seconds_are_truncated_and_offset_is_explicitly_logged(caplog):
    value, precision = parse_date("2026-01-01T23:59:49+02:00")
    assert (value, precision) == (dt.datetime(2026, 1, 1, 23, 59), DatePrecision.MINUTE)
    assert "timezone offset discarded" in caplog.text


@pytest.mark.parametrize(
    "value", ["2026", "2026-01", "2026-01-03", "2026-01-03T08", "2026-01-03T08:30", "2026-01-03T08:30:45+02:00"]
)
def test_staging_schema_and_runtime_accept_precisions(value):
    record = {
        "title": "Play",
        "url": "https://example.org/play",
        "premiere": value,
        "memberships": [{"name": "A", "born": value}],
    }
    validator = Draft202012Validator(staging_schema(), format_checker=FormatChecker())
    validator.validate([record])
    normalized = validate_records([record])
    validator.validate(normalized)
    assert validate_records(normalized) == normalized


def test_hour_input_defaults_to_zero_minutes():
    record = {
        "title": "Play",
        "url": "https://example.org/play",
        "premiere": "2026-01-03T08",
        "premiere_precision": "minute",
        "memberships": [{"name": "A", "born": "1980-03-05T07", "born_precision": "minute"}],
    }
    Draft202012Validator(staging_schema(), format_checker=FormatChecker()).validate([record])
    normalized = validate_records([record])[0]
    assert normalized["premiere"] == "2026-01-03T08:00:00"
    assert normalized["premiere_precision"] == "minute"
    assert normalized["memberships"][0]["born"] == "1980-03-05T07:00:00"
    assert normalized["memberships"][0]["born_precision"] == "minute"
    assert premiere_label(normalized) == "03.01.2026 08:00"


def test_hour_precision_is_not_valid():
    record = {
        "title": "Play",
        "url": "https://example.org/play",
        "premiere": "2026-01-03T08",
        "premiere_precision": "hour",
    }
    assert list(Draft202012Validator(staging_schema(), format_checker=FormatChecker()).iter_errors([record]))
    with pytest.raises(ValidationError):
        validate_records([record])


@pytest.mark.parametrize("value", ["0000", "2025-02-29", "2026-04-31T08:00", "2026-13", "2026-01-01T08:60"])
def test_staging_schema_and_runtime_reject_invalid_dates(value):
    record = {"title": "Play", "url": "https://example.org/play", "premiere": value}
    validator = Draft202012Validator(staging_schema(), format_checker=FormatChecker())
    assert list(validator.iter_errors([record]))
    with pytest.raises(ValidationError):
        validate_records([record])


def test_precision_cannot_exceed_input_or_exist_without_date():
    validator = Draft202012Validator(staging_schema(), format_checker=FormatChecker())
    for date, precision in (("2026", "day"), (None, "month"), ("2026-05", "minute")):
        record = {"title": "Play", "url": "https://example.org/play", "premiere": date, "premiere_precision": precision}
        assert list(validator.iter_errors([record]))
        with pytest.raises(ValidationError):
            validate_records([record])


@pytest.fixture
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dates.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


def test_all_domain_dates_roundtrip_and_updates(db):
    org = Organization.model_validate({"name": "Theatre", "founded": "1900", "closed": "2026-02"})
    person = Person.model_validate({"name": "Actor", "born": "1980-05", "died": "2020-05-06"})
    text = Text.model_validate({"title": "Work", "published": "1901"})
    award = Award(name="Award")
    with Session(db) as session:
        session.add_all([org, person, text, award])
        session.flush()
        production = Production.model_validate(
            {"name": "Play", "premiere": "2026-10-03T19:30", "organization_id": org.id}
        )
        membership = OrganizationMembership.model_validate(
            {"organization_id": org.id, "person_id": person.id, "start_date": "2021", "end_date": "2024-09"}
        )
        nomination = AwardNomination.model_validate(
            {"award_id": award.id, "date": "2022", "category": "Example", "result": AwardResult.WON}
        )
        session.add_all([production, membership, nomination])
        session.commit()
        ids = org.id, person.id, text.id, production.id, membership.id, nomination.id
    with Session(db) as session:
        for model, ident, fields in (
            (Organization, ids[0], {"founded": "year", "closed": "month"}),
            (Person, ids[1], {"born": "month", "died": "day"}),
            (Text, ids[2], {"published": "year"}),
            (Production, ids[3], {"premiere": "minute"}),
            (OrganizationMembership, ids[4], {"start_date": "year", "end_date": "month"}),
            (AwardNomination, ids[5], {"date": "year"}),
        ):
            record = session.get(model, ident)
            assert record is not None
            for field, precision in fields.items():
                assert isinstance(getattr(record, field), dt.datetime)
                assert getattr(record, f"{field}_precision") == precision
    updated = update_entity(Person, entity_id=str(ids[1]), payload={"born": "1980-05-06"}, actor_id="test")
    assert updated.born == dt.datetime(1980, 5, 6)
    assert updated.born_precision == DatePrecision.DAY
    cleared = update_entity(Person, entity_id=str(ids[1]), payload={"born": None}, actor_id="test")
    assert cleared.born is None and cleared.born_precision is None


def test_review_changes_date_and_precision_atomically(db):
    with Session(db) as session:
        person = Person.model_validate({"name": "Person", "born": "1980"})
        source = Source(name="source")
        session.add_all([person, source])
        session.commit()
        person_id, source_id = person.id, source.id
    case = propose_record(
        EntityType.PERSON, {"name": "Person", "born": "1980-05"}, source_id=source_id, external_id="person"
    )
    preview = preview_case(case.id, target_id=person_id)
    assert "born_precision" not in [row["field"] for row in preview["fields"]]
    approve_case(case.id, revision=preview["revision"], target_id=person_id, fields=["born"], actor_id="test")
    with Session(db) as session:
        saved = session.get(Person, person_id)
        assert saved is not None
        assert saved.born == dt.datetime(1980, 5, 1)
        assert saved.born_precision == DatePrecision.MONTH


def test_hour_input_roundtrips_as_minute_in_database_and_review(db):
    with Session(db) as session:
        person = Person.model_validate({"name": "Person", "born": "1980-05-01T08"})
        source = Source(name="source")
        session.add_all([person, source])
        session.commit()
        person_id, source_id = person.id, source.id
    with Session(db) as session:
        saved = session.get(Person, person_id)
        assert saved is not None
        assert saved.born == dt.datetime(1980, 5, 1, 8, 0)
        assert saved.born_precision == DatePrecision.MINUTE
    case = propose_record(
        EntityType.PERSON,
        {"name": "Person", "born": "1980-05-01T09"},
        source_id=source_id,
        external_id="person",
    )
    preview = preview_case(case.id, target_id=person_id)
    approve_case(case.id, revision=preview["revision"], target_id=person_id, fields=["born"], actor_id="test")
    with Session(db) as session:
        saved = session.get(Person, person_id)
        assert saved is not None
        assert saved.born == dt.datetime(1980, 5, 1, 9, 0)
        assert saved.born_precision == DatePrecision.MINUTE


def test_people_merge_selects_date_and_precision_together(db):
    with Session(db) as session:
        first = Person.model_validate({"name": "Person", "born": "1980"})
        second = Person.model_validate({"name": "Person", "born": "1980-01"})
        session.add_all([first, second])
        session.commit()
        first_id, second_id = first.id, second.id
    preview = preview_people_merge([first_id, second_id], target_id=first_id)
    date_field = next(field for field in preview["fields"] if field["field"] == "born")
    assert date_field["conflict"]
    assert not any(field["field"] == "born_precision" for field in preview["fields"])
    merge_people(
        [first_id, second_id],
        target_id=first_id,
        revision=preview["revision"],
        choices={"born": str(second_id)},
        actor_id="test",
    )
    with Session(db) as session:
        saved = session.get(Person, first_id)
        assert saved is not None
        assert saved.born == dt.datetime(1980, 1, 1)
        assert saved.born_precision == DatePrecision.MONTH


def test_upload_refines_overlap_and_flags_disjoint_months(db):
    with Session(db) as session:
        org = Organization(name="Theatre")
        session.add(org)
        session.commit()
        org_id = org.id

    def upload(date):
        return ingest_records(
            [{"title": "Play", "url": "https://example.org/play", "premiere": date}],
            organization_id=org_id,
            source_name="example.org",
            actor_id="test",
            captured_at=dt.datetime.now(dt.UTC),
        )

    upload("2026")
    upload("2026-04")
    with Session(db) as session:
        production = session.exec(select(Production)).one()
        assert production.premiere == dt.datetime(2026, 4, 1)
        assert production.premiere_precision == DatePrecision.MONTH
    result = upload("2026-05")
    assert result["review"] == ["Play"]


def test_upload_hour_input_is_a_single_minute(db):
    with Session(db) as session:
        org = Organization(name="Theatre")
        session.add(org)
        session.commit()
        org_id = org.id

    def upload(date):
        return ingest_records(
            [{"title": "Play", "url": "https://example.org/play", "premiere": date}],
            organization_id=org_id,
            source_name="example.org",
            actor_id="test",
            captured_at=dt.datetime.now(dt.UTC),
        )

    upload("2026-04-20T19")
    with Session(db) as session:
        production = session.exec(select(Production)).one()
        assert production.premiere == dt.datetime(2026, 4, 20, 19, 0)
        assert production.premiere_precision == DatePrecision.MINUTE
    assert upload("2026-04-20T19:00")["review"] == []
    assert upload("2026-04-20T19:30")["review"] == ["Play"]


def test_filter_overlaps_month_and_keeps_precision_label():
    record = {"id": str(uuid.uuid4()), "name": "Play", "premiere": "2026-02-01T00:00:00", "premiere_precision": "month"}
    assert premiere_label(record) == "02.2026"
    assert filter_productions([record], "", "", [], (dt.date(2026, 2, 20), dt.date(2026, 2, 28))) == [record]
    assert not filter_productions([record], "", "", [], (dt.date(2026, 3, 1), dt.date(2026, 3, 2)))
