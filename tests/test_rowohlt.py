from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from typing import Self

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import text as sql
from sqlmodel import Session, SQLModel, create_engine, select

from backend import user_management
from backend.ingestion import rowohlt
from backend.ingestion.publishers import ingest_text_metadata
from backend.models import (
    LineageRelation,
    ReconciliationCase,
    SourceRecord,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
)
from backend.texts import CreditPeople, add_lineage, resolve_text_case


def play(identifier=1, **fields: object):
    return {
        "data": {
            "play": {
                "id": identifier,
                "seoUrl": f"sample-{identifier}",
                "title": "Sample",
                "authors": [{"givenName": "Original", "surname": "Author"}],
                "translators": [],
                "editors": [],
                **fields,
            },
            "slices": [],
        }
    }


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_metadata_preserves_cast_ranges_keywords_and_raw_observations(session) -> None:
    payload = play(
        titleEnglish="Marketing title",
        subtitle="<b>Subtitle</b>",
        description="<p>First paragraph.</p><p>Second paragraph.</p>",
        genre=["Junges Theater"],
        keywords=["Climate", "Friendship"],
        womenMin=0,
        womenMax=4,
        menMin=3,
        menMax=4,
        totalActorsMin=5,
        totalActorsMax=7,
        castNote="Variable cast",
        ageRating="6+",
        performances="Uraufführung: 01.01.2020 Example",
        onlineFrom="2024-01-01",
        updatedAt="2024-01-01",
    )
    record = rowohlt.normalize_play(payload).model_dump(mode="json", exclude_none=True)
    imported = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(imported, Text)
    session.commit()
    assert (imported.women_min, imported.women_max, imported.men_min, imported.men_max) == (0, 4, 3, 4)
    assert (imported.total_cast_min, imported.total_cast_max, imported.suggested_min_age) == (5, 7, 6)
    assert imported.genre_labels == ["Junges Theater"]
    assert imported.keywords == ["Climate", "Friendship"]
    assert imported.subtitle == "Subtitle"
    assert imported.description == "First paragraph.\n\nSecond paragraph."
    assert imported.published is None
    assert {title.title for title in session.exec(select(TextTitle)).all()} == {"Sample", "Marketing title"}
    assert not session.exec(select(TextLineage)).all()
    observation = session.exec(select(SourceRecord)).one()
    assert observation.payload["source_metadata"] == payload
    payload["data"]["play"]["updatedAt"] = "2024-02-01"
    replay = ingest_text_metadata(session, "Rowohlt", [rowohlt.normalize_play(payload).model_dump(mode="json")])[0]
    assert replay.id == imported.id
    assert not session.exec(select(ReconciliationCase)).all()
    assert observation.payload["source_metadata"] == payload


def test_bilingual_markdown_descriptions_labels_and_rights_are_persisted(session) -> None:
    payload = play(
        description='<p>German <em>summary</em>.</p><p><a href="https://example.org/info">Details</a></p>',
        descriptionEnglish="<p>English <strong>summary</strong>.</p><ul><li>One</li><li>Two</li></ul>",
        subtitle="<em>German subtitle</em>",
        subtitleEnglish="<em>English subtitle</em>",
        castNote="Flexible",
        castNoteEnglish="Flexible casting",
        isFreeForSchoolAmateurPlay=False,
        isNovelOrFilmMaterial=False,
        releasedForUdse=True,
        hasRoleBook=True,
        foreignRights=False,
        freeTextSchoolAmateurPlay="<p><strong>Permission required.</strong></p>",
        foreignRightsText='<p>Contact <a href="https://example.org/agent">agent</a>.</p>',
    )
    record = rowohlt.normalize_play(payload).model_dump(mode="json", exclude_none=True)
    imported = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(imported, Text)
    session.commit()
    assert imported.description == "German *summary*.\n\n[Details](https://example.org/info)"
    assert imported.description_en == "English **summary**.\n\n* One\n* Two"
    assert (imported.subtitle, imported.subtitle_en) == ("German subtitle", "English subtitle")
    assert (imported.cast_note, imported.cast_note_en) == ("Flexible", "Flexible casting")
    assert imported.is_free_for_school_amateur_play is False
    assert imported.is_novel_or_film_material is False
    assert imported.released_for_udse is True
    assert imported.has_role_book is True
    assert imported.foreign_rights is False
    assert imported.school_amateur_note == "**Permission required.**"
    assert imported.foreign_rights_note == "Contact [agent](https://example.org/agent)."
    assert session.exec(select(SourceRecord)).one().payload["source_metadata"] == payload
    assert ingest_text_metadata(session, "Rowohlt", [record])[0].id == imported.id
    assert not session.exec(select(ReconciliationCase)).all()


def test_english_enrichment_keeps_german_description_and_missing_flags_unknown(session) -> None:
    original = rowohlt.normalize_play(play(description="<p>German summary.</p>")).model_dump(mode="json")
    imported = ingest_text_metadata(session, "Rowohlt", [original])[0]
    assert isinstance(imported, Text)
    assert imported.description_en is None and imported.is_free_for_school_amateur_play is None
    enriched = rowohlt.normalize_play(
        play(
            description="<p>German summary.</p>",
            descriptionEnglish="<p>English <em>summary</em>.</p>",
            isFreeForSchoolAmateurPlay=False,
        )
    ).model_dump(mode="json")
    assert ingest_text_metadata(session, "Rowohlt", [enriched])[0].id == imported.id
    assert imported.description == "German summary."
    assert imported.description_en == "English *summary*."
    assert imported.is_free_for_school_amateur_play is False
    assert not session.exec(select(ReconciliationCase)).all()


@pytest.mark.parametrize("changes", [{"description_en": "Changed English"}, {"is_free_for_school_amateur_play": True}])
def test_known_english_description_and_false_rights_flag_require_review_on_change(session, changes) -> None:
    record = rowohlt.normalize_play(
        play(descriptionEnglish="<p>English summary.</p>", isFreeForSchoolAmateurPlay=False)
    ).model_dump(mode="json")
    imported = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(imported, Text)
    case = ingest_text_metadata(session, "Rowohlt", [{**record, **changes}])[0]
    assert isinstance(case, ReconciliationCase)
    assert imported.description_en == "English summary."
    assert imported.is_free_for_school_amateur_play is False
    assert case.context["publisher_observations"][0]["source_metadata"] == record["source_metadata"]


@pytest.mark.parametrize(
    "fields",
    [
        {"womenMin": -1},
        {"womenMin": True},
        {"menMin": "2"},
        {"seoUrl": "sample-99"},
        {"authors": [{}]},
        {"isFreeForSchoolAmateurPlay": "false"},
        {"isNovelOrFilmMaterial": 1},
        {"descriptionEnglish": 123},
    ],
)
def test_invalid_metadata_is_rejected(fields) -> None:
    with pytest.raises((ValueError, ValidationError)):
        rowohlt.normalize_play(play(**fields))


def test_contradictory_upstream_ranges_warn_and_retain_raw_values(caplog) -> None:
    payload = play(menMin=10, menMax=5, womenMin=0, womenMax=3, totalActorsMin=10, totalActorsMax=15)
    record = rowohlt.normalize_play(payload)
    assert record.men_min is None and record.men_max is None
    assert (record.women_min, record.women_max, record.total_cast_min, record.total_cast_max) == (0, 3, 10, 15)
    assert record.source_metadata == payload
    assert "contradictory men range 10..5" in caplog.text


def test_translation_uses_original_title_not_english_marketing_title(session) -> None:
    payload = play(
        title="German title",
        originalTitle="Original title",
        titleEnglish="Foreign rights title",
        freeTextBeforeTranslators="Deutsch von",
        translators=[{"givenName": "German", "surname": "Translator"}],
    )
    result = ingest_text_metadata(session, "Rowohlt", [rowohlt.normalize_play(payload).model_dump(mode="json")])[0]
    assert isinstance(result, Text)
    edge = session.exec(select(TextLineage)).one()
    assert edge.relation == LineageRelation.TRANSLATION
    assert session.get(Text, edge.source_text_id).title == "Original title"
    assert session.get(Text, edge.source_text_id).languages == []
    assert edge.derived_text_id == result.id
    assert result.languages == ["de"]
    assert {
        row.title for row in session.exec(select(TextTitle).where(TextTitle.text_id == edge.source_text_id)).all()
    } == {"Original title"}


def test_original_title_alone_establishes_translation_without_guessing_translator_or_language(session) -> None:
    record = rowohlt.normalize_play(
        play(title="Localized", originalTitle="Source title", originalSubtitle="Source subtitle")
    ).model_dump(mode="json")
    translated = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(translated, Text)
    edge = session.exec(select(TextLineage)).one()
    source = session.get(Text, edge.source_text_id)
    assert edge.relation == LineageRelation.TRANSLATION
    assert source.title == "Source title" and source.languages == [] and source.subtitle == "Source subtitle"
    assert translated.title == "Localized" and translated.languages == ["de"]
    assert not session.exec(select(TextContribution).where(TextContribution.role == "translator")).all()


def test_translation_reuses_existing_linked_german_version(session) -> None:
    source = Text(title="Original title", languages=["en"], genre="play")
    translated = Text(title="German title", languages=["de"], genre="play")
    session.add_all([source, translated])
    session.flush()
    people = CreditPeople(session)
    people.add_credits(source, ["Original Author"], "author")
    people.add_credits(translated, ["Ada Translator"], "translator")
    add_lineage(session, source.id, translated.id, LineageRelation.TRANSLATION)
    session.flush()
    record = rowohlt.normalize_play(
        play(
            title="German title",
            originalTitle="Original title",
            translators=[{"givenName": "Ada", "surname": "Translator"}],
        )
    ).model_dump(mode="json")
    result = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert result.id == translated.id
    assert len(session.exec(select(Text)).all()) == 2
    assert len(session.exec(select(TextLineage)).all()) == 1


def test_original_language_fills_existing_source_language_only_when_unknown(session) -> None:
    source = Text(title="Source title", genre="play")
    session.add(source)
    session.flush()
    CreditPeople(session).add_credits(source, ["Original Author"], "author")
    result = ingest_text_metadata(
        session,
        "Rowohlt",
        [
            rowohlt.normalize_play(
                play(
                    title="German title",
                    originalTitle="Source title",
                    originalLanguage="en",
                    translators=[{"givenName": "Ada", "surname": "Translator"}],
                )
            ).model_dump(mode="json")
        ],
    )[0]
    assert isinstance(result, Text)
    assert source.languages == ["en"]
    assert result.languages == ["de"]


def test_translation_title_collision_waits_for_review_and_keeps_translation_relation(session) -> None:
    record = rowohlt.normalize_play(play(title="Localized", originalTitle="Source title")).model_dump(mode="json")
    other = ingest_text_metadata(
        session,
        "Rowohlt",
        [{"title": "Localized", "authors": ["Original Author"], "genre": "play", "url": "https://example.org/other"}],
    )[0]
    assert isinstance(other, Text)
    case = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(case, ReconciliationCase)
    assert case.incoming["lineage_relation"] == "translation"
    assert len(case.context["credit_inputs"]) == 1
    assert case.context["credit_inputs"][0]["relation"] == "translation"
    assert resolve_text_case(session, case.id, target_id=other.id, actor_id="test").id == other.id
    assert session.exec(select(TextLineage)).one().relation == LineageRelation.TRANSLATION
    assert len(session.exec(select(Text)).all()) == 2


def test_ambiguous_original_title_reviews_source_then_creates_translation(session) -> None:
    sources = [Text(title="Source title", genre="play"), Text(title="Source title", genre="play")]
    session.add_all(sources)
    session.flush()
    people = CreditPeople(session)
    people.add_credits(sources[0], ["Original Author"], "author")
    people.add_credits(sources[1], ["Original Author"], "author")
    record = rowohlt.normalize_play(
        play(
            title="Localized",
            originalTitle="Source title",
            originalSubtitle="Source subtitle",
            translators=[{"givenName": "Ada", "surname": "Translator"}],
        )
    ).model_dump(mode="json")
    case = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(case, ReconciliationCase)
    assert case.context["source_case_id"]
    assert case.context["pending_derived"]["title"] == "Localized"
    result = resolve_text_case(session, case.id, target_id=sources[1].id, actor_id="test")
    edge = session.exec(select(TextLineage)).one()
    assert result.title == "Localized"
    assert (edge.source_text_id, edge.derived_text_id, edge.relation) == (
        sources[1].id,
        result.id,
        LineageRelation.TRANSLATION,
    )
    assert sources[1].subtitle == "Source subtitle"
    assert len(session.exec(select(Text)).all()) == 3


def test_translations_field_is_stored_as_rights_note_not_false_lineage(session) -> None:
    record = rowohlt.normalize_play(
        play(
            translations="<p><strong>English (UK):</strong> Translator Name</p>",
            translationsEnglish="<p><strong>Chinese:</strong> rights available</p>",
        )
    ).model_dump(mode="json")
    text = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(text, Text)
    assert text.translations_note == "**English (UK):** Translator Name"
    assert text.translations_note_en == "**Chinese:** rights available"
    assert not session.exec(select(TextLineage)).all()


def test_source_material_and_same_author_adaptations_stay_separate(session) -> None:
    material = play(isNovelOrFilmMaterial=True, titleBookTemplate="Sample", authorBookTemplate="Original Author")
    first = play(
        2,
        titleBookTemplate="Sample",
        authorBookTemplate="Original Author",
        freeTextBeforeEditors="Fassung von",
        editors=[{"givenName": "Original", "surname": "Author"}],
    )
    second = play(
        3,
        titleBookTemplate="Sample",
        authorBookTemplate="Original Author",
        freeTextBeforeEditors="Bearbeitet von",
        editors=[{"givenName": "Other", "surname": "Adapter"}],
    )
    results = ingest_text_metadata(
        session,
        "Rowohlt",
        [rowohlt.normalize_play(payload).model_dump(mode="json") for payload in (material, first, second)],
    )
    assert all(isinstance(result, Text) for result in results)
    assert len({result.id for result in results}) == 3
    edges = session.exec(select(TextLineage)).all()
    assert {(edge.source_text_id, edge.derived_text_id, edge.relation) for edge in edges} == {
        (results[0].id, result.id, LineageRelation.BASED_ON) for result in results[1:]
    }
    assert len(session.exec(select(Text)).all()) == 3
    assert len(session.exec(select(TextContribution).where(TextContribution.role == "adapter")).all()) == 2
    material_text, adaptation = results[:2]
    assert isinstance(material_text, Text) and isinstance(adaptation, Text)
    assert material_text.genre == "other" and adaptation.genre == "play"


def test_source_book_reuses_existing_novel(session) -> None:
    book = ingest_text_metadata(
        session,
        "Rowohlt",
        [{"title": "Sample", "authors": ["Original Author"], "genre": "novel", "url": "https://example.org/book"}],
    )[0]
    payload = play(
        titleBookTemplate="Sample",
        authorBookTemplate="Original Author",
        freeTextBeforeEditors="Bearbeitet von",
        editors=[{"givenName": "Stage", "surname": "Adapter"}],
    )
    result = ingest_text_metadata(session, "Rowohlt", [rowohlt.normalize_play(payload).model_dump(mode="json")])[0]
    assert isinstance(result, Text) and result.id != book.id
    assert session.exec(select(TextLineage)).one().source_text_id == book.id
    assert len(session.exec(select(Text)).all()) == 2


def test_approving_adaptation_review_attaches_confirmed_source_and_metadata(session) -> None:
    existing = ingest_text_metadata(
        session,
        "Rowohlt",
        [{"title": "Sample", "authors": ["Stage Adapter"], "genre": "play", "url": "https://example.org/play"}],
    )[0]
    payload = play(
        titleBookTemplate="Sample",
        authorBookTemplate="Original Author",
        keywords=["Theme"],
        freeTextBeforeEditors="Bearbeitet von",
        editors=[{"givenName": "Stage", "surname": "Adapter"}],
    )
    record = rowohlt.normalize_play(payload).model_dump(mode="json")
    case = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(case, ReconciliationCase)
    approved = resolve_text_case(session, case.id, target_id=existing.id, actor_id="test")
    assert approved.id == existing.id
    assert approved.keywords == ["Theme"]
    assert session.exec(select(TextLineage)).one().derived_text_id == existing.id
    assert len(session.exec(select(TextContribution).where(TextContribution.role == "adapter")).all()) == 1
    assert ingest_text_metadata(session, "Rowohlt", [record])[0].id == existing.id
    assert len(session.exec(select(Text)).all()) == 2


def test_changed_cast_requires_review_without_overwriting(session) -> None:
    record = rowohlt.normalize_play(play(womenMin=0, womenMax=2)).model_dump(mode="json")
    imported = ingest_text_metadata(session, "Rowohlt", [record])[0]
    assert isinstance(imported, Text)
    case = ingest_text_metadata(session, "Rowohlt", [{**record, "women_max": 3}])[0]
    assert isinstance(case, ReconciliationCase)
    assert imported.women_max == 2
    assert session.exec(select(SourceRecord)).one().payload["women_max"] == 2
    assert case.context["publisher_observations"][0]["women_max"] == 3


@pytest.fixture
def http(monkeypatch):
    responses, requests = {}, []

    class Response:
        status = 200

        def __init__(self, payload) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            pass

        async def json(self):
            return copy.deepcopy(self.payload)

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        @asynccontextmanager
        async def get(self, url, **kwargs: object):
            assert kwargs == {"allow_redirects": False}
            requests.append(url)
            yield Response(responses[url])

    monkeypatch.setattr(rowohlt.aiohttp, "ClientSession", lambda **kwargs: Client())
    return responses, requests


def catalog(ids, page=1, total=2, next_url=None):
    return {
        "data": [{"id": identifier} for identifier in ids],
        "page": page,
        "totalItems": total,
        "totalPages": 2,
        "nextPageUrl": next_url,
    }


@pytest.fixture
def progress_bars(monkeypatch):
    bars = []

    class Progress:
        def __init__(self, *, total, desc, unit) -> None:
            self.total = total
            self.desc = desc
            self.unit = unit
            self.n = 0
            self.updates = []
            self.closed = False
            bars.append(self)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.closed = True

        def refresh(self) -> None:
            pass

        def update(self, amount=1) -> None:
            self.n += amount
            self.updates.append(self.n)

    monkeypatch.setattr(rowohlt, "tqdm", Progress)
    return bars


def test_download_follows_pagination_and_fetches_details(http, progress_bars) -> None:
    responses, requests = http
    second_url = f"{rowohlt.API}?page=2&limit=100"
    responses[f"{rowohlt.API}?page=1&limit=100"] = catalog([1], next_url="/rowohlt-theater/plays?page=2&limit=100")
    responses[second_url] = catalog([2], page=2)
    responses[f"{rowohlt.API}/1"], responses[f"{rowohlt.API}/2"] = play(1), play(2)
    results = asyncio.run(rowohlt.download())
    assert [result["external_id"] for result in results] == ["1", "2"]
    assert requests == [f"{rowohlt.API}?page=1&limit=100", f"{rowohlt.API}/1", second_url, f"{rowohlt.API}/2"]
    progress = progress_bars[0]
    assert progress.total == 2 and progress.updates == [1, 2] and progress.closed


def test_download_limit_and_single_id_avoid_extra_requests(http, progress_bars) -> None:
    responses, requests = http
    responses[f"{rowohlt.API}?page=1&limit=100"] = catalog([1, 2])
    responses[f"{rowohlt.API}/1"] = play(1)
    assert len(asyncio.run(rowohlt.download(limit=1))) == 1
    assert len(requests) == 2
    requests.clear()
    assert len(asyncio.run(rowohlt.download(play_id=1))) == 1
    assert requests == [f"{rowohlt.API}/1"]
    assert [(bar.total, bar.n, bar.closed) for bar in progress_bars] == [(1, 1, True), (1, 1, True)]


@pytest.mark.parametrize(
    ("next_url", "message"),
    [(None, "ended before"), ("https://example.org/plays", "Unsafe"), (f"{rowohlt.API}?page=1&limit=100", "repeated")],
)
def test_download_rejects_incomplete_unsafe_or_looping_catalog(http, next_url, message, progress_bars) -> None:
    responses, requests = http
    responses[f"{rowohlt.API}?page=1&limit=100"] = catalog([1], next_url=next_url)
    responses[f"{rowohlt.API}/1"] = play(1)
    with pytest.raises(ValueError, match=message):
        asyncio.run(rowohlt.download())
    assert len(requests) == 2
    assert (progress_bars[0].total, progress_bars[0].n, progress_bars[0].closed) == (2, 1, True)


def test_failed_download_preserves_existing_snapshot(http, tmp_path, progress_bars) -> None:
    responses, _requests = http
    output = tmp_path / "plays.json"
    output.write_text("previous snapshot")
    responses[f"{rowohlt.API}/1"] = play(2)
    with pytest.raises(ValueError, match="does not match request"):
        rowohlt.main(["download", "--play-id", "1", "--output", str(output)])
    assert output.read_text() == "previous snapshot"
    assert list(tmp_path.iterdir()) == [output]
    assert progress_bars[0].n == 0 and progress_bars[0].closed


def test_cli_import_commits_snapshot_and_validates_before_initializing_db(
    session, monkeypatch, tmp_path, progress_bars
) -> None:
    initialized = []
    monkeypatch.setattr(user_management, "init_db", lambda: initialized.append(True))
    monkeypatch.setattr(user_management, "_get_engine", session.get_bind)
    snapshot = tmp_path / "plays.json"
    snapshot.write_text(json.dumps([rowohlt.normalize_play(play()).model_dump(mode="json")]))
    rowohlt.main(["import", "--json", str(snapshot)])
    assert initialized == [True]
    assert session.exec(select(SourceRecord)).one().external_id == "1"
    from backend.models import IngestionRun  # ruff: ignore[import-outside-top-level]

    history = session.exec(select(IngestionRun)).one()
    assert history.source_name == "Rowohlt"
    assert history.stats == {"observations": 1, "review": 0}
    assert [(bar.total, bar.n, bar.closed) for bar in progress_bars] == [(1, 1, True)]
    initialized.clear()
    snapshot.write_text('[{"title":"invalid"}]')
    with pytest.raises(ValidationError):
        rowohlt.main(["import", "--json", str(snapshot)])
    assert not initialized
    assert len(progress_bars) == 1


def test_cli_import_progress_counts_observations_queued_for_review(
    session, monkeypatch, tmp_path, progress_bars, capsys
) -> None:
    session.add_all([Text(title="Sample", genre="play"), Text(title="Sample", genre="play")])
    session.commit()
    monkeypatch.setattr(user_management, "init_db", lambda: None)
    monkeypatch.setattr(user_management, "_get_engine", session.get_bind)
    snapshot = tmp_path / "plays.json"
    snapshot.write_text(json.dumps([rowohlt.normalize_play(play()).model_dump(mode="json")]))
    rowohlt.main(["import", "--json", str(snapshot)])
    assert session.exec(select(ReconciliationCase)).one().status == "pending"
    assert [(bar.total, bar.updates, bar.closed) for bar in progress_bars] == [(1, [1], True)]
    assert "1 awaiting review" in capsys.readouterr().out


def test_migration_preserves_existing_text_and_unknown_cast(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        config = user_management.alembic_config(connection)
        command.upgrade(config, "0014")
        connection.execute(sql("INSERT INTO text (id, title, languages) VALUES ('old', 'Existing', '[]')"))
        command.upgrade(config, "0015")
        row = connection.execute(sql("SELECT title, keywords, genre_labels, women_min, total_cast_max FROM text")).one()
        assert tuple(row) == ("Existing", "[]", "[]", None, None)
        command.downgrade(config, "0014")
        assert connection.execute(sql("SELECT title FROM text")).scalar_one() == "Existing"
    engine.dispose()


def test_bilingual_rights_migration_keeps_original_description_and_unknown_flags(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'bilingual.db'}")
    with engine.begin() as connection:
        config = user_management.alembic_config(connection)
        command.upgrade(config, "0015")
        connection.execute(
            sql(
                "INSERT INTO text (id, title, languages, keywords, genre_labels, description) "
                "VALUES ('old', 'Existing', '[]', '[]', '[]', 'Original description')"
            )
        )
        command.upgrade(config, "0017")
        row = connection.execute(
            sql(
                "SELECT description, description_en, is_free_for_school_amateur_play, "
                "is_novel_or_film_material FROM text"
            )
        ).one()
        assert tuple(row) == ("Original description", None, None, None)
        command.downgrade(config, "0016")
        assert connection.execute(sql("SELECT description FROM text")).scalar_one() == "Original description"
        command.downgrade(config, "0015")
    engine.dispose()
