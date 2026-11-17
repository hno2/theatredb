import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from backend.ingestion.publishers import (
    PUBLISHERS,
    extract_metadata,
    ingest_text_metadata,
    normalize_metadata,
    resolve_publisher_observations,
    save_publisher_observation,
)
from backend.ingestion.staging import staging_schema, validate_records
from backend.models import EntitySource, ReconciliationCase, Source, SourceRecord, Text
from backend.texts import add_title, resolve_text_case


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_staging_accepts_recursive_text_metadata_and_rejects_bad_nested_values() -> None:
    record = {
        "title": "Production",
        "url": "https://theatre.example/show",
        "text": {
            "title": "Play",
            "language": "de",
            "published": "2020",
            "based_on": {"title": "Novel", "authors": ["Author"], "based_on": "Older story"},
        },
    }
    validator = Draft202012Validator(staging_schema(), format_checker=FormatChecker())
    validator.validate([record])
    validated = validate_records([record])
    assert validated[0]["text"]["published_precision"] == "year"
    assert validated[0]["text"]["based_on"]["based_on"] == "Older story"
    validator.validate(validated)
    record["text"]["based_on"]["authors"] = [""]
    with pytest.raises(ValidationError):
        validate_records([record])
    assert list(validator.iter_errors([record]))


@pytest.mark.parametrize("publisher", PUBLISHERS)
def test_publisher_import_is_idempotent_and_preserves_url_and_ua(session, publisher) -> None:
    metadata = {
        "Titel": "Translated play",
        "Originaltitel": "Original play",
        "Originalsprache": "en",
        "Sprache": "de",
        "Autor": "Original Author",
        "Übersetzung": ["Translator"],
        "UA": "20.10.2020, Example Theatre",
        "url": PUBLISHERS[publisher] + "play",
        "external_id": "play-1",
    }
    first = ingest_text_metadata(session, publisher, [metadata])[0]
    session.flush()
    second = ingest_text_metadata(session, publisher, [metadata])[0]
    assert isinstance(first, Text)
    assert first.id == second.id
    links = session.exec(select(EntitySource)).all()
    assert len(links) == 1
    assert links[0].external_url == metadata["url"]
    assert links[0].external_id == "play-1"
    observation = session.exec(select(SourceRecord)).one()
    assert observation.payload["world_premiere"] == metadata["UA"]
    assert first.published is None


def test_publisher_validates_entire_batch_before_writing(session) -> None:
    with pytest.raises(ValidationError):
        ingest_text_metadata(
            session,
            "VdA",
            [
                {"title": "Valid", "url": "https://example.org/play"},
                {"title": "Invalid", "url": "javascript:alert(1)"},
            ],
        )
    assert not session.exec(select(Source)).all()
    assert not session.exec(select(Text)).all()
    with pytest.raises(ValueError, match="Unknown publisher"):
        ingest_text_metadata(session, "Unknown", [])


def test_publisher_ambiguity_retains_observation_without_attaching_source(session) -> None:
    for _ in range(2):
        add_title(session, Text(title="Shared title"), "Shared title")
    metadata = {"title": "Shared title", "url": "https://www.theatertexte.de/shared"}
    case = ingest_text_metadata(session, "theatertexte.de", [metadata])[0]
    repeated = ingest_text_metadata(session, "theatertexte.de", [metadata])[0]
    assert isinstance(case, ReconciliationCase)
    assert repeated.id == case.id
    assert case.source_id is not None
    assert len(case.context["publisher_observations"]) == 1
    assert case.context["publisher_observations"][0]["url"] == metadata["url"]
    assert not session.exec(select(EntitySource)).all()


def test_approved_publisher_observation_saves_sources_and_replays_identity(session) -> None:
    texts = [Text(title="Shared title"), Text(title="Shared title")]
    for text in texts:
        add_title(session, text, text.title)
    metadata = {"title": "Shared title", "url": "https://www.theatertexte.de/shared", "external_id": "shared"}
    case = ingest_text_metadata(session, "theatertexte.de", [metadata])[0]
    assert isinstance(case, ReconciliationCase)
    resolve_text_case(session, case.id, target_id=texts[1].id, actor_id="test")
    assert session.exec(select(EntitySource)).one().entity_id == texts[1].id
    assert session.exec(select(SourceRecord)).one().entity_id == texts[1].id
    replay = ingest_text_metadata(session, "theatertexte.de", [metadata])[0]
    assert replay.id == texts[1].id
    assert len(session.exec(select(Text)).all()) == 2


@pytest.mark.parametrize("changes", [{"title": "New title"}, {"authors": ["New author"]}])
def test_publisher_identity_changed_metadata_queues_review_without_duplicate(session, changes) -> None:
    metadata = {
        "title": "Original title",
        "authors": ["Original author"],
        "url": "https://www.theatertexte.de/original",
        "external_id": "stable-id",
    }
    original = ingest_text_metadata(session, "theatertexte.de", [metadata])[0]
    assert isinstance(original, Text)
    incoming = {**metadata, **changes, "url": "https://www.theatertexte.de/new"}
    case = ingest_text_metadata(session, "theatertexte.de", [incoming])[0]
    assert isinstance(case, ReconciliationCase)
    assert case.status == "pending"
    assert case.context["candidate_ids"] == [str(original.id)]
    assert len(session.exec(select(Text)).all()) == 1
    assert original.title == "Original title"
    assert session.exec(select(SourceRecord)).one().payload["title"] == "Original title"
    resolve_publisher_observations(session, original, case.context["publisher_observations"])
    case.status, case.target_id = "approved", original.id
    session.add(case)
    session.flush()
    assert ingest_text_metadata(session, "theatertexte.de", [incoming])[0].id == original.id
    assert len(session.exec(select(Text)).all()) == 1
    assert original.title == "Original title"


def test_publisher_replay_url_identity_can_fill_blanks_without_overwriting(session) -> None:
    metadata = {"title": "Play", "url": "https://www.theatertexte.de/play"}
    text = ingest_text_metadata(session, "theatertexte.de", [metadata])[0]
    assert isinstance(text, Text)
    replay = ingest_text_metadata(
        session, "theatertexte.de", [{**metadata, "external_id": "new-id", "genre": "play", "language": "de"}]
    )[0]
    assert replay.id == text.id
    assert text.genre == "play"
    assert text.languages == ["de"]
    assert len(session.exec(select(Text)).all()) == 1
    source_record = session.exec(select(SourceRecord)).one()
    other = Text(title="Other")
    add_title(session, other, other.title)
    with pytest.raises(ValueError, match="already links another text"):
        save_publisher_observation(session, other, {"source_id": str(source_record.source_id), **metadata})


def test_offline_publisher_extraction_reuses_configurable_field_rules() -> None:
    metadata = extract_metadata(
        "<h1>Play</h1><p class='author'>Author</p><p class='original'>Original</p>",
        url="https://www.theatertexte.de/play",
        rules={
            "Titel": {"selector": "h1", "required": True},
            "Autoren": {"selector": ".author", "many": True},
            "Originaltitel": {"selector": ".original"},
        },
    )
    assert metadata.title == "Play"
    assert metadata.original_title == "Original"
    assert metadata.authors == ["Author"]
    with pytest.raises(ValueError, match="Conflicting metadata"):
        normalize_metadata({"title": "One", "Titel": "Two", "url": metadata.url})
