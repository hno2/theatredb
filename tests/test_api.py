import datetime as dt
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from backend.api import app
from backend.models import (
    EntityRedirect,
    EntitySource,
    EntityType,
    Organization,
    Person,
    Production,
    ProductionMembership,
    Source,
    Text,
    TextTitle,
)


@pytest.fixture
def api_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("backend.user_management._get_engine", lambda: engine)
    theatre = Organization(
        name="Test Theatre",
        address_locality="Berlin",
        general_verified_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        general_verified_by="internal-reviewer",
        general_verification_evidence="https://example.org",
    )
    person = Person(name="Test Director")
    text = Text(title="Test Play")
    production = Production(name="Test Production", organization_id=theatre.id, text_id=text.id, premiere="2020")
    source = Source(name="Public source")
    old_id = uuid4()
    ids = {
        "organizations": theatre.id,
        "people": person.id,
        "texts": text.id,
        "productions": production.id,
        "old_person": old_id,
    }
    with Session(engine) as session:
        session.add_all(
            [
                theatre,
                Organization(name="Other Theatre"),
                person,
                Person(name="Other Person"),
                text,
                Text(title="Other Text"),
                TextTitle(text_id=text.id, title="Alternative"),
                production,
                Production(name="Other Production"),
                ProductionMembership(person_id=person.id, production_id=production.id, role_name="director"),
                source,
                EntitySource(source_id=source.id, entity_type=EntityType.PRODUCTION, entity_id=production.id),
                EntityRedirect(entity_type=EntityType.PERSON, old_id=old_id, target_id=person.id),
            ]
        )
        session.commit()
    yield ids
    engine.dispose()


@pytest.fixture
def client(api_db):
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    ("resource", "query"),
    [("organizations", "Berlin"), ("productions", "Berlin"), ("people", "Director"), ("texts", "Alternative")],
)
def test_list_search_and_get_reuse_public_catalog(client, api_db, resource, query):
    first = client.get(f"/api/{resource}", params={"limit": 1})
    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert len(first.json()["items"]) == 1
    assert first.json()["items"][0]["id"] == str(api_db[resource])
    second = client.get(f"/api/{resource}", params={"limit": 1, "offset": 1}).json()
    assert second["total"] == 2
    assert second["items"][0]["id"] != str(api_db[resource])

    search = client.get(f"/api/{resource}", params={"q": query})
    assert search.status_code == 200
    assert search.json()["total"] == 1
    assert search.json()["items"][0]["id"] == str(api_db[resource])
    assert client.get(f"/api/{resource}", params={"q": "no match"}).json() == {"items": [], "total": 0}
    assert client.get(f"/api/{resource}", params={"offset": 2}).json() == {"items": [], "total": 2}

    detail = client.get(f"/api/{resource}/{api_db[resource]}")
    assert detail.status_code == 200
    assert detail.json()["id"] == str(api_db[resource])
    assert client.get(f"/api/{resource}/{uuid4()}").status_code == 404
    assert client.get(f"/api/{resource}/not-a-uuid").status_code == 422
    assert client.post(f"/api/{resource}", json={}).status_code == 405
    assert client.delete(f"/api/{resource}/{api_db[resource]}").status_code == 405


def test_details_preserve_relationships_redaction_and_merged_ids(client, api_db):
    production = client.get(f"/api/productions/{api_db['productions']}").json()
    assert production["organization"]["name"] == "Test Theatre"
    assert "general_verified_by" not in production["organization"]
    assert production["memberships"][0]["person"]["name"] == "Test Director"
    assert production["text"]["title"] == "Test Play"
    assert len(production["sources"]) == 1
    assert "general_verified_by" not in client.get("/api/organizations").json()["items"][0]
    assert "general_verified_by" not in client.get("/api/productions").json()["items"][0]["organization"]
    assert client.get(f"/api/people/{api_db['old_person']}").json()["id"] == str(api_db["people"])


def test_open_repertoire_exports_use_orif_documents(client, api_db):
    organization = client.get(f"/api/organizations/{api_db['organizations']}/orif")
    assert organization.headers["content-type"] == "application/ld+json"
    document = organization.json()
    assert (document["version"], document["organization"]["name"]) == ("v2", "Test Theatre")
    [production] = document["productions"]
    assert (production["identifier"], production["name"]) == (str(api_db["productions"]), "Test Production")
    assert production["creator"][0]["creator"][0]["name"] == "Test Director"
    assert production["isBasedOn"] == [{"@type": "CreativeWork", "name": "Test Play"}]
    assert client.get(f"/api/productions/{api_db['productions']}/orif").json() == document
    assert client.get(f"/api/people/{api_db['old_person']}/orif").json() == [document]
    other = client.get("/api/productions", params={"q": "Other"}).json()["items"][0]["id"]
    assert client.get(f"/api/productions/{other}/orif").status_code == 404
    for resource in ("organizations", "productions", "people"):
        assert client.get(f"/api/{resource}/{uuid4()}/orif").status_code == 404


@pytest.mark.parametrize(
    ("params", "status"),
    [({"limit": 100}, 200), ({"limit": 0}, 422), ({"limit": 101}, 422), ({"limit": "bad"}, 422), ({"offset": -1}, 422)],
)
def test_pagination_validates_bounds(client, params, status):
    assert client.get("/api/texts", params=params).status_code == status


def test_unexpected_errors_are_not_reported_as_missing_entities(client, api_db, monkeypatch):
    def broken_graph(*args: object, **kwargs: object) -> dict:
        raise ValueError("Entity redirect cycle")

    monkeypatch.setattr("backend.api.get_entity_graph", broken_graph)
    with pytest.raises(ValueError, match="Entity redirect cycle"):
        client.get(f"/api/people/{api_db['people']}")


def test_openapi_exposes_only_get_routes_and_parameter_bounds():
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    assert len(schema["paths"]) == 11
    assert all(set(operations) == {"get"} for operations in schema["paths"].values())
    parameters = {parameter["name"]: parameter for parameter in schema["paths"]["/api/texts"]["get"]["parameters"]}
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert parameters["offset"]["schema"]["minimum"] == 0
