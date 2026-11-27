import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import authority_match, gnd, user_management, wikidata
from backend.date_precision import DatePrecision
from backend.models import EntityType, Organization, Person, Tag, Text, TextContribution, TextLineage
from backend.taxonomy import GND_CONCEPTS, VOCABULARY, sync_vocabulary
from backend.texts import add_title


def ref(gnd_id, label):
    return {"id": gnd.GND_URI + gnd_id, "label": label}


GOETHE = ref("118540238", "Goethe, Johann Wolfgang von")
RECORDS = {
    "4099197-0": {
        "gndIdentifier": "4099197-0",
        "type": ["Work"],
        "preferredName": "Faust, 1",
        "variantName": ["Faust - der Tragödie erster Teil", "Фауст"],
        "dateOfPublication": ["1808"],
        "firstAuthor": [GOETHE],
        "formOfWorkAndExpression": [ref("4012899-4", "Drama")],
        "precedingWork": [ref("4682136-3", "Faust. Ein Fragment"), ref("4127437-4", "Urfaust")],
    },
    "4682136-3": {
        "gndIdentifier": "4682136-3",
        "type": ["Work"],
        "preferredName": "Faust. Ein Fragment",
        "dateOfProduction": ["1786-1789"],
        "dateOfPublication": ["1790"],
        "firstAuthor": [GOETHE],
        "precedingWork": [ref("4127437-4", "Urfaust")],
    },
    "4127437-4": {
        "gndIdentifier": "4127437-4",
        "type": ["Work"],
        "preferredName": "Urfaust",
        "dateOfProduction": ["1774"],
        "firstAuthor": [GOETHE],
    },
    "118540238": {
        "gndIdentifier": "118540238",
        "type": ["Person", "DifferentiatedPerson"],
        "preferredName": "Goethe, Johann Wolfgang von",
        "variantName": ["Goethe, J. W.", "Gete, Iogann", "歌德", "ゲーテ, ヨハン"]
        + [f"Goethe, V{i}" for i in range(20)],
        "dateOfBirth": ["1749-08-28"],
        "dateOfDeath": ["1832-03-22"],
        "gender": [{"id": "https://d-nb.info/standards/vocab/gnd/gender#male"}],
    },
    "63046-9": {
        "gndIdentifier": "63046-9",
        "type": ["CorporateBody"],
        "preferredName": "Thalia Theater (Hamburg)",
        "dateOfEstablishment": ["1843"],
        "precedingCorporateBody": [ref("1-2", "Altes Thalia")],
    },
}


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(gnd, "urlopen", lambda *a, **k: pytest.fail("Unexpected external request"))
    monkeypatch.setattr(gnd, "fetch_record", lambda gnd_id: RECORDS[gnd_id])
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.mark.parametrize(
    "value",
    ["4099197-0", "https://d-nb.info/gnd/4099197-0", "https://explore.gnd.network/gnd/4099197-0?term=Faust%20I&pos=1"],
)
def test_parse_gnd_id_accepts_ids_and_record_urls(value):
    assert gnd.parse_gnd_id(value) == "4099197-0"


@pytest.mark.parametrize("value", ["", "Faust", "https://evil.example/gnd/4099197-0", "4099197-0; drop"])
def test_parse_gnd_id_rejects_other_input(value):
    with pytest.raises(ValueError, match="Invalid GND ID"):
        gnd.parse_gnd_id(value)


def test_text_enrichment_imports_selected_facts_and_one_lineage_hop(session):
    text = Text(title="Faust I")
    session.add(text)
    session.flush()

    report = gnd.enrich_text(session, text, "4099197-0")

    assert (text.genre, text.published, text.published_precision) == (
        "play",
        dt.datetime(1808, 1, 1),
        DatePrecision.YEAR,
    )
    assert report["predecessors"] == ["Faust. Ein Fragment", "Urfaust"]
    edges = {
        (session.get(Text, row.source_text_id).title, session.get(Text, row.derived_text_id).title)
        for row in session.exec(select(TextLineage))
    }
    # Fragment -> Urfaust is a second hop and must not be imported.
    assert edges == {("Faust. Ein Fragment", "Faust I"), ("Urfaust", "Faust I")}
    fragment = session.exec(select(Text).where(Text.title == "Faust. Ein Fragment")).one()
    assert (fragment.created.year, fragment.published.year) == (1786, 1790)
    goethe = session.exec(select(Person)).one()
    assert (goethe.name, goethe.gender, goethe.born.year, goethe.born_precision) == (
        "Johann Wolfgang von Goethe",
        "m",
        1749,
        DatePrecision.DAY,
    )
    assert "J. W. Goethe" in goethe.alt_names
    assert len(goethe.alt_names) == gnd.MAX_VARIANTS
    assert not {"Iogann Gete", "歌德"} & set(goethe.alt_names)
    assert len(session.exec(select(TextContribution)).all()) == 3

    again = gnd.enrich_text(session, text)
    assert again["filled"] == again["texts_created"] == again["people_created"] == []
    assert len(session.exec(select(Text)).all()) == 3


def test_text_enrichment_respects_selection_and_existing_values(session):
    text = Text(title="Faust I", genre="other")
    session.add(text)
    session.flush()

    report = gnd.enrich_text(session, text, "4099197-0", predecessor_ids={"4127437-4"})

    assert report["conflicts"] == [{"field": "genre", "current": "other", "authority": "play"}]
    assert text.genre == "other"
    assert {session.get(Text, row.source_text_id).title for row in session.exec(select(TextLineage))} == {"Urfaust"}


def test_same_name_records_are_reported_not_merged(session):
    namesake = Person(name="Johann Wolfgang von Goethe")
    urfaust = Text(title="Urfaust")
    text = Text(title="Faust I")
    session.add_all([namesake, urfaust, text])
    session.flush()
    add_title(session, urfaust, "Urfaust")

    report = gnd.enrich_text(session, text, "4099197-0")

    assert report["unlinked_people"][0]["candidate_id"] == str(namesake.id)
    assert report["unlinked_texts"][0]["candidate_id"] == str(urfaust.id)
    assert session.exec(select(TextContribution)).all() == []
    assert len(session.exec(select(Person)).all()) == 1


def test_organization_fills_dates_and_only_reports_predecessors(session):
    organization = Organization(name="Thalia Theater")
    session.add(organization)
    session.flush()

    report = gnd.enrich_organization(session, organization, "63046-9")

    assert (organization.founded, organization.founded_precision) == (dt.datetime(1843, 1, 1), DatePrecision.YEAR)
    assert report["predecessors"] == [{"id": "1-2", "label": "Altes Thalia"}]
    assert len(session.exec(select(Organization)).all()) == 1


def test_record_type_must_match_entity(session):
    person = Person(name="Thalia")
    session.add(person)
    session.flush()
    with pytest.raises(ValueError, match="not a Person"):
        gnd.enrich_person(session, person, "63046-9")


def test_person_gender_is_constrained(session):
    with pytest.raises(ValueError, match="Gender"):
        Person.model_validate({"name": "X", "gender": "x"})
    assert Person.model_validate({"name": "X", "gender": ""}).gender is None
    session.add(Person(name="X", gender="x"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_vocabulary_carries_gnd_concepts(session):
    assert set(GND_CONCEPTS) <= {code for code, *_ in VOCABULARY}
    sync_vocabulary(session)
    assert session.get(Tag, "form.tanz").uri == "https://d-nb.info/gnd/4059028-8"
    assert session.get(Tag, "premiere.wa").uri is None


def _hit(gnd_id):
    record = RECORDS[gnd_id]
    return {"id": gnd_id, "label": record["preferredName"], "description": "", "url": "", "record": record}


def _claim(value):
    return {"mainsnak": {"snaktype": "value", "datavalue": {"value": value}}}


GOETHE_WIKIDATA = {
    "id": "Q5879",
    "labels": {"de": {"value": "Johann Wolfgang von Goethe"}},
    "descriptions": {"de": {"value": "deutscher Dichter"}},
    "claims": {
        "P569": [_claim({"time": "+1749-00-00T00:00:00Z", "precision": 9})],
        "P227": [_claim("118540238")],
    },
}


def test_panel_looks_up_both_and_fills_from_both_after_one_click(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'gnd-ui.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    monkeypatch.setattr(gnd, "search", lambda query, kind: [_hit("118540238")])
    wikidata_hit = {"id": "Q5879", "label": "Goethe", "description": "", "url": "", "record": GOETHE_WIKIDATA}
    monkeypatch.setattr(wikidata, "search", lambda query, kind: [wikidata_hit])
    monkeypatch.setattr(gnd, "fetch_record", RECORDS.__getitem__)
    monkeypatch.setattr(wikidata, "fetch_entity", lambda qid: GOETHE_WIKIDATA)
    with Session(engine) as db:
        person = Person(
            name="Johann Wolfgang von Goethe", born=dt.datetime(1749, 1, 1), born_precision=DatePrecision.YEAR
        )
        db.add(person)
        db.commit()
        person_id = str(person.id)
    app = AppTest.from_string(
        "from backend.models import EntityType\n"
        "from frontend.management.authority import render_authority_panel\n"
        f"render_authority_panel(EntityType.PERSON, {{'id': '{person_id}'}})\n"
    ).run()
    assert not app.exception
    assert any("Hohe Sicherheit" in s.value for s in app.success)
    assert any("verweisen aufeinander" in s.value for s in app.success)
    with Session(engine) as db:
        unchanged = db.get(Person, person.id)
        assert unchanged is not None
        assert unchanged.died is None  # lookup alone never writes
    [button] = [b for b in app.button if b.label == "Bestätigen & übernehmen"]
    button.click().run()
    assert not app.warning, [w.value for w in app.warning]
    assert any(s.value.startswith("Übernommen: born, description, died, gender") for s in app.success)
    with Session(engine) as db:
        stored = db.get(Person, person.id)
        assert stored is not None
        assert (stored.gender, stored.died, stored.description) == ("m", dt.datetime(1832, 3, 22), "deutscher Dichter")
        assert (stored.born, stored.born_precision) == (dt.datetime(1749, 8, 28), DatePrecision.DAY)
        assert (
            gnd.identity(db, EntityType.PERSON, person.id),
            wikidata.identity(db, EntityType.PERSON, person.id),
        ) == (
            "118540238",
            "Q5879",
        )
    engine.dispose()


def test_contradicting_cross_link_is_not_a_match():
    gnd_record = {**RECORDS["118540238"], "sameAs": [{"id": "http://www.wikidata.org/entity/Q1"}]}
    assert authority_match.agreement(gnd_record, GOETHE_WIKIDATA) is True  # Wikidata P227 names the GND record
    assert authority_match.agreement(gnd_record, {"id": "Q2"}) is False
    assert authority_match.agreement(RECORDS["118540238"], {"id": "Q2"}) is None


def test_name_alone_is_not_a_high_confidence_match(session):
    person = Person(name="Johann Wolfgang von Goethe")
    session.add(person)
    session.flush()
    assert (
        authority_match.propose(session, authority_match.GND, EntityType.PERSON, person, hits=[_hit("118540238")])
        is None
    )
    person.died = dt.datetime(1830, 1, 1)  # contradicting year
    person.born = dt.datetime(1749, 1, 1)
    assert (
        authority_match.propose(session, authority_match.GND, EntityType.PERSON, person, hits=[_hit("118540238")])
        is None
    )


def test_theatre_needs_matching_place(session):
    theatre = Organization(name="Thalia Theater", address_locality="Rostock")
    session.add(theatre)
    session.flush()
    hamburg = {**RECORDS["63046-9"], "placeOfBusiness": [{"id": gnd.GND_URI + "4023118-5", "label": "Hamburg"}]}
    hit = {"id": "63046-9", "record": hamburg}
    assert authority_match.propose(session, authority_match.GND, EntityType.ORGANIZATION, theatre, hits=[hit]) is None
    theatre.address_locality = "Hamburg"
    proposal = authority_match.propose(session, authority_match.GND, EntityType.ORGANIZATION, theatre, hits=[hit])
    assert proposal is not None
    assert proposal.evidence == "Name + Ort"


def test_bulk_links_author_first_then_text_by_linked_author(session, monkeypatch):
    goethe = Person(name="Johann Wolfgang von Goethe", born=dt.datetime(1749, 8, 28))
    faust = Text(title="Faust, 1")
    session.add_all([goethe, faust])
    session.flush()
    add_title(session, faust, "Faust, 1")
    session.add(TextContribution(text_id=faust.id, person_id=goethe.id, role="author"))
    session.commit()
    hits = {EntityType.PERSON: [_hit("118540238")], EntityType.TEXT: [_hit("4099197-0")]}
    monkeypatch.setattr(gnd, "search", lambda query, kind: hits[kind])

    assert authority_match.bulk_proposals(session, authority_match.GND, EntityType.TEXT)[0] == []
    people, _, done = authority_match.bulk_proposals(session, authority_match.GND, EntityType.PERSON)
    assert done
    assert [p.evidence for p in people] == ["Name + Geburtsjahr"]
    authority_match.apply(session, people)
    texts, _, _ = authority_match.bulk_proposals(session, authority_match.GND, EntityType.TEXT)
    assert [(p.external_id, p.evidence) for p in texts] == [("4099197-0", "Titel + verknüpfte:r Autor:in")]
    [(_, report)] = authority_match.apply(session, texts)
    assert isinstance(report, dict)
    assert report["predecessors"] == []  # bulk never imports lineage
    assert gnd.identity(session, EntityType.TEXT, faust.id) == "4099197-0"


def test_cross_link_from_wikidata_proposes_gnd_without_search(session, monkeypatch):
    person = Person(name="Goethe")
    session.add(person)
    session.flush()
    entity = {
        "id": "Q5879",
        "claims": {"P227": [{"mainsnak": {"snaktype": "value", "datavalue": {"value": "118540238"}}}]},
    }
    wikidata.enrich_person(session, person, "Q5879", entity=entity)
    monkeypatch.setattr(wikidata, "fetch_entity", lambda qid: entity)
    monkeypatch.setattr(gnd, "search", lambda *a: pytest.fail("cross-link must not search"))
    proposal = authority_match.propose(session, authority_match.GND, EntityType.PERSON, person)
    assert proposal is not None
    assert (proposal.external_id, proposal.evidence) == ("118540238", "Wikidata Q5879 verweist darauf")


def test_wikidata_person_enrichment_fills_blanks(session):
    person = Person(name="Johann Wolfgang von Goethe")
    session.add(person)
    session.flush()

    def claim(value):
        return {"mainsnak": {"snaktype": "value", "datavalue": {"value": value}}}

    entity = {
        "id": "Q5879",
        "labels": {"de": {"value": "Johann Wolfgang von Goethe"}},
        "aliases": {"de": [{"value": "J. W. Goethe"}, {"value": "Gete"}], "ru": [{"value": "Гёте"}]},
        "claims": {
            "P569": [claim({"time": "+1749-08-28T00:00:00Z", "precision": 11})],
            "P21": [claim({"id": "Q6581097"})],
        },
    }
    report = wikidata.enrich_person(session, person, "Q5879", entity=entity)
    assert (person.born, person.gender) == (dt.datetime(1749, 8, 28), "m")
    assert person.alt_names == ["J. W. Goethe"]
    assert set(report["filled"]) == {"born", "gender"}


def test_finer_agreeing_date_refines_instead_of_conflicting(session):
    person = Person(name="Goethe", born=dt.datetime(1749, 1, 1), born_precision=DatePrecision.YEAR)
    session.add(person)
    session.flush()
    report = gnd.enrich_person(session, person, "118540238")
    assert (person.born, person.born_precision) == (dt.datetime(1749, 8, 28), DatePrecision.DAY)
    assert report["conflicts"] == []
    report = wikidata.enrich_person(session, person, "Q5879", entity=GOETHE_WIKIDATA)  # coarser agreeing year
    assert (person.born, person.born_precision) == (dt.datetime(1749, 8, 28), DatePrecision.DAY)
    assert "born" not in report["filled"] and report["conflicts"] == []
