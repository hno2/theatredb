import datetime as dt
import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend import ai, wikidata
from backend.date_precision import DatePrecision
from backend.models import (
    EntitySource,
    Person,
    ReconciliationCase,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
)
from backend.texts import add_lineage, add_title, resolve_text_case


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args: object, **kwargs: object):
        pytest.fail("Unexpected external request")

    monkeypatch.setattr(wikidata, "urlopen", forbidden)
    monkeypatch.setattr(ai, "urlopen", forbidden)
    for key in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
        monkeypatch.delenv(key, raising=False)


def claim(value):
    return {"mainsnak": {"snaktype": "value", "datavalue": {"value": value}}}


def item(qid, title, *, claims=None):
    return {"id": qid, "labels": {"en": {"value": title}}, "claims": claims or {}}


def configure_ai(monkeypatch):
    monkeypatch.setenv("AI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AI_API_KEY", "test-token")
    monkeypatch.setenv("AI_MODEL", "test-model")


def completion(value):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]}).encode())


def test_search_returns_unapproved_dictionaries_and_checks_qids(monkeypatch, session):
    monkeypatch.setattr(
        wikidata,
        "_request_json",
        lambda params: {
            "search": [
                {"id": "Q41567", "label": "Hamlet", "description": "play", "aliases": ["The Tragedy of Hamlet"]},
            ]
        },
    )
    assert wikidata.search_texts("Hamlet")[0]["id"] == "Q41567"
    assert session.exec(select(Text)).all() == []
    with pytest.raises(ValueError, match="QID"):
        wikidata.fetch_entity("https://example.invalid/Q1")


def test_wikidata_timeout_and_http_error_are_explicit(monkeypatch):
    requests = []

    def fail(request, *, timeout):
        requests.append((request, timeout))
        raise HTTPError(request.full_url, 503, "unavailable", Message(), None)

    monkeypatch.setattr(wikidata, "urlopen", fail)
    with pytest.raises(wikidata.WikidataError, match="503"):
        wikidata.search_texts("Hamlet")
    assert requests[0][1] == wikidata.TIMEOUT


def test_selected_stub_uses_qid_not_same_title_and_reuses_identity(session):
    unrelated = Text(title="Hamlet")
    session.add(unrelated)
    session.flush()
    selected = wikidata.create_text_stub(session, "Q41567", entity=item("Q41567", "Hamlet"))
    assert selected.id != unrelated.id
    assert wikidata.create_text_stub(session, "Q41567").id == selected.id
    link = session.exec(select(EntitySource)).one()
    assert link.entity_id == selected.id
    assert link.external_id == "Q41567"
    with pytest.raises(ValueError, match="match selected"):
        wikidata.create_text_stub(session, "Q2", entity=item("Q3", "Other"))


def test_enrichment_preserves_titles_languages_dates_and_root_credits(session, monkeypatch):
    entity = item(
        "Q1",
        "Hamlet",
        claims={
            "P50": [claim({"id": "Q2"})],
            "P144": [claim({"id": "Q3"})],
            "P407": [claim({"id": "Q1860"}), claim({"id": "Q99999"})],
            "P577": [claim({"time": "+1603-00-00T00:00:00Z", "precision": 9})],
        },
    )
    entity["labels"]["de"] = {"value": "Hamlet, Prinz von Dänemark"}
    entity["labels"]["ja"] = {"value": "ハムレット"}
    entity["aliases"] = {"fr": [{"value": "La Tragédie d'Hamlet"}]}
    entities = {
        "Q2": item("Q2", "William Shakespeare"),
        "Q3": item("Q3", "Amleth"),
        "Q99999": item("Q99999", "Unmapped language"),
    }
    monkeypatch.setattr(wikidata, "fetch_entity", entities.__getitem__)
    root = wikidata.create_text_stub(session, "Q1", entity=entity)
    translation = Text(title="Hamlet", languages=["de"])
    translator = Person(name="Translator")
    session.add_all([translation, translator])
    session.flush()
    session.add(TextContribution(text_id=translation.id, person_id=translator.id, role="translator"))
    add_lineage(session, root.id, translation.id, "translation")
    report = wikidata.enrich_text(session, root, entity=entity)
    assert report["unknown_language_qids"] == ["Q99999"]
    assert report["warnings"]
    assert root.languages == ["en"]
    assert root.published == dt.datetime(1603, 1, 1)
    assert root.published_precision == DatePrecision.YEAR
    assert {row.title for row in session.exec(select(TextTitle).where(TextTitle.text_id == root.id))} >= {
        "Hamlet",
        "Hamlet, Prinz von Dänemark",
        "ハムレット",
        "La Tragédie d'Hamlet",
    }
    second = wikidata.enrich_text(session, root, entity=entity)
    assert second["authors_added"] == second["titles_added"] == 0
    assert len(session.exec(select(TextLineage)).all()) == 2
    derived_credits = session.exec(select(TextContribution).where(TextContribution.text_id == translation.id)).all()
    assert [(row.person_id, row.role) for row in derived_credits] == [(translator.id, "translator")]
    assert session.exec(select(EntitySource).where(EntitySource.external_id == "Q1")).one().entity_id == root.id
    with pytest.raises(ValueError, match="family root"):
        wikidata.enrich_text(session, translation, "Q1", entity=entity)


def test_enrichment_rejects_translation_before_any_write(session):
    root, translation = Text(title="Hamlet"), Text(title="Hamlet", languages=["de"])
    session.add_all([root, translation])
    session.flush()
    add_lineage(session, root.id, translation.id, "translation")
    entity = item("Q1", "Hamlet", claims={"P50": [claim({"id": "Q2"})]})
    with pytest.raises(ValueError, match="family root"):
        wikidata.enrich_text(session, translation, "Q1", entity=entity)
    assert session.exec(select(EntitySource)).all() == []
    assert session.exec(select(TextContribution)).all() == []
    assert [row.title for row in session.exec(select(TextTitle).where(TextTitle.text_id == translation.id))] == [
        "Hamlet"
    ]


def test_enrichment_keeps_variant_language_titles_unlabelled(session):
    entity = item("Q1", "Hamlet")
    entity["labels"] |= {
        "en-gb": {"value": "Hamlet, Prince of Denmark"},
        "zh-hans": {"value": "哈姆雷特"},
        "simple": {"value": "Hamlet play"},
        "de": {"value": "Hamlet, Prinz von Dänemark"},
    }
    entity["aliases"] = {"pt-br": [{"value": "A Tragédia de Hamlet"}]}
    text = wikidata.create_text_stub(session, "Q1", entity=entity)
    wikidata.enrich_text(session, text, entity=entity)
    titles = {row.title: row.language for row in session.exec(select(TextTitle).where(TextTitle.text_id == text.id))}
    assert titles == {
        "Hamlet": "en",
        "Hamlet, Prince of Denmark": None,
        "哈姆雷特": None,
        "Hamlet play": None,
        "Hamlet, Prinz von Dänemark": "de",
        "A Tragédia de Hamlet": None,
    }


@pytest.mark.parametrize(
    ("time", "precision", "expected", "resolution"),
    [
        ("+1901-00-00T00:00:00Z", 9, dt.datetime(1901, 1, 1), DatePrecision.YEAR),
        ("+1901-03-00T00:00:00Z", 10, dt.datetime(1901, 3, 1), DatePrecision.MONTH),
        ("+1901-03-02T00:00:00Z", 11, dt.datetime(1901, 3, 2), DatePrecision.DAY),
    ],
)
def test_partial_wikidata_dates(time, precision, expected, resolution):
    assert wikidata.parse_publication({"time": time, "precision": precision}) == (expected, resolution)


def test_unsupported_date_is_reported_without_inventing_date(session):
    entity = item(
        "Q1",
        "Ancient work",
        claims={
            "P577": [claim({"time": "-0500-00-00T00:00:00Z", "precision": 9})],
        },
    )
    text = wikidata.create_text_stub(session, "Q1", entity=entity)
    report = wikidata.enrich_text(session, text, entity=entity)
    assert text.published is None
    assert report["warnings"]


def test_ai_disabled_never_calls_network():
    with pytest.raises(ai.AIDisabledError):
        ai.complete_json("ignored", {"type": "object"})


def test_ai_transport_schema_validation_and_bounded_timeout(monkeypatch):
    configure_ai(monkeypatch)
    calls = []

    def respond(request, *, timeout):
        calls.append((request, timeout))
        return completion({"ids": ["Q1"]})

    monkeypatch.setattr(ai, "urlopen", respond)
    schema = {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["ids"],
        "additionalProperties": False,
    }
    assert ai.complete_json("choose", schema) == {"ids": ["Q1"]}
    assert calls[0][0].full_url == "https://example.invalid/v1/chat/completions"
    assert calls[0][1] == ai.TIMEOUT
    monkeypatch.setattr(ai, "urlopen", lambda *args, **kwargs: completion({"ids": [42]}))
    with pytest.raises(ai.AIError, match="schema mismatch"):
        ai.complete_json("choose", schema)
    with pytest.raises(ai.AIError, match="Unsupported JSON schema"):
        ai.complete_json("choose", {"$ref": "#/something"})


def test_ai_http_errors_are_explicit(monkeypatch):
    configure_ai(monkeypatch)

    def fail(request, **kwargs: object):
        raise HTTPError(request.full_url, 401, "unauthorized", Message(), None)

    monkeypatch.setattr(ai, "urlopen", fail)
    with pytest.raises(ai.AIError, match="AI HTTP error 401"):
        ai.complete_json("choose", {"type": "object"})


def credits(**values: object):
    empty = {"title": None, **{field: [] for field in ai._CREDIT_LISTS}}
    return {**empty, **values}


@pytest.mark.parametrize(
    ("credit", "expected"),
    [
        (
            "von William Shakespeare / Deutsch von A. Schlegel / Fassung von Ada Example",
            credits(authors=["William Shakespeare"], translators=["A. Schlegel"], adapters=["Ada Example"]),
        ),
        (
            "Fassung von Ada Example nach William Shakespeare",
            credits(adapters=["Ada Example"], source_authors=["William Shakespeare"]),
        ),
        (
            "Les Misérables nach dem gleichnamigen Roman von Victor Hugo, in einer Fassung von R. Example",
            credits(
                title="Les Misérables",
                adapters=["R. Example"],
                source_authors=["Victor Hugo"],
                source_titles=["Les Misérables"],
                source_notes=["dem gleichnamigen Roman"],
            ),
        ),
        (
            "Hamlet von William Shakespeare / Deutsch von August Wilhelm Schlegel und Ludwig Tieck",
            credits(
                title="Hamlet",
                authors=["William Shakespeare"],
                translators=["August Wilhelm Schlegel", "Ludwig Tieck"],
            ),
        ),
        (
            "Der Prozess nach „Der Process“ von Franz Kafka",
            credits(
                title="Der Prozess",
                source_authors=["Franz Kafka"],
                source_titles=["Der Process"],
            ),
        ),
    ],
)
def test_credit_parser_separates_authors_sources_and_translators(credit, expected):
    assert ai.parse_credits(credit) == expected


def test_credit_ai_fallback_is_opt_in_gets_full_context_and_drops_invented_names(monkeypatch):
    calls = []
    credit = "Hamlet. Ein Stück frei erzählt durch Ada Example"

    def suggest(prompt, schema):
        calls.append(prompt)
        return credits(title="Hamlet", authors=["Ada Example", "Invented Person"], source_titles=["Hamlet"])

    monkeypatch.setattr(ai, "complete_json", suggest)
    assert ai.parse_credits(credit)["unparsed"] == [credit.rstrip(".")]
    assert not calls
    parsed = ai.parse_credits(credit, use_ai=True)
    assert parsed == credits(title="Hamlet", authors=["Ada Example"], source_titles=["Hamlet"])
    assert len(calls) == 1
    assert credit in calls[0]


def test_lineage_proposal_restricts_candidates_and_only_writes_review_case(session, monkeypatch):
    root, derived, other = Text(title="Root"), Text(title="Translation"), Text(title="Unrelated")
    session.add_all([root, derived, other])
    session.flush()
    proposal = {"source_text_id": str(other.id), "relation": "translation", "reason": "guess"}
    monkeypatch.setattr(ai, "complete_json", lambda prompt, schema: proposal)
    with pytest.raises(ai.AIError, match="unsupplied"):
        ai.propose_text_lineage(session, derived.id, [{"id": str(root.id)}], use_ai=True)
    proposal["source_text_id"] = str(root.id)
    case = ai.propose_text_lineage(session, derived.id, [{"id": str(root.id)}], use_ai=True)
    assert case is not None
    assert case.operation == "text_lineage"
    assert case.target_id == derived.id
    assert case.incoming["source_candidate_id"] == str(root.id)
    repeated = ai.propose_text_lineage(session, derived.id, [{"id": str(root.id)}], use_ai=True)
    assert repeated is not None and repeated.id == case.id
    assert len(session.exec(select(ReconciliationCase)).all()) == 1
    assert session.exec(select(TextLineage)).all() == []
    assert resolve_text_case(session, case.id).id == derived.id
    edge = session.exec(select(TextLineage)).one()
    assert (edge.source_text_id, edge.derived_text_id, edge.relation) == (root.id, derived.id, "translation")


def test_title_suggestions_are_verified_or_flagged_and_never_added(session, monkeypatch):
    text = wikidata.create_text_stub(session, "Q1", entity=item("Q1", "Hamlet"))
    add_title(session, text, "Hamlet", "en")
    before = len(session.exec(select(TextTitle)).all())
    entity = item("Q1", "Hamlet")
    entity["labels"]["de"] = {"value": "Der Prinz"}
    monkeypatch.setattr(wikidata, "fetch_entity", lambda qid: entity)
    monkeypatch.setattr(
        ai,
        "complete_json",
        lambda prompt, schema: {
            "titles": [
                {"title": "Der Prinz", "language": "de"},
                {"title": "An invented title", "language": "en"},
            ]
        },
    )
    suggestions = ai.suggest_titles(session, text.id, use_ai=True)
    by_title = {row["title"]: row for row in suggestions}
    assert by_title["Der Prinz"]["verified"] is True
    assert by_title["Der Prinz"]["source"] == "wikidata"
    assert by_title["An invented title"]["verified"] is False
    assert len(session.exec(select(TextTitle)).all()) == before
