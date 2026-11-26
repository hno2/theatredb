import pytest
from pydantic import ValidationError

from backend.ingestion.staging import validate_records
from backend.orif import as_staged, orif_document, orif_documents

THEATRE = {
    "id": "theatre",
    "name": "Bühne",
    "street_address": "Weg 1",
    "postal_code": "10115",
    "address_locality": "Berlin",
    "address_country": "Deutschland",
    "latitude": 52.5,
    "longitude": 13.4,
}


def _person(person_id: str, name: str) -> dict:
    return {"id": person_id, "name": name}


TOSCA = {
    "id": "tosca",
    "name": "Tosca",
    "premiere": "2026-03-01T19:30:00",
    "premiere_precision": "minute",
    "duration": 150,
    "url": "https://theatre.example/tosca",
    "tags": ["Oper", "Neu", "UA"],
    "suggested_min_age": 0,
    "memberships": [
        {"role_name": "Regie", "person": _person("anna", "Anna")},
        {"role_name": "actor", "character_name": "Tosca", "person": _person("bea", "Bea")},
        {"role_name": "Musik", "person": None},
    ],
    "text": {
        "title": "Tosca",
        "genre": "opera",
        "languages": ["it"],
        "contributions": [
            {"role": "composer", "person": _person("puccini", "Puccini")},
            {"role": "author", "person": _person("illica", "Illica")},
        ],
    },
}


def test_export_maps_renamed_fields_credits_and_premiere_event() -> None:
    document = orif_document(THEATRE, [TOSCA])
    assert document["@context"] == "https://schema.org" and document["version"] == "v2"
    assert document["organization"]["address"] == {
        "@type": "PostalAddress",
        "streetAddress": "Weg 1",
        "postalCode": "10115",
        "addressLocality": "Berlin",
        "addressCountry": "Deutschland",
    }
    production = document["productions"][0]
    assert production["audience"] == {"@type": "PeopleAudience", "suggestedMinAge": 0}
    assert production["genre"] == ["oper"] and production["inLanguage"] == ["it"]
    assert production["productionType"] == "WorldPremiere"
    assert production["isBasedOn"][0]["@type"] == "MusicComposition"
    assert [agent["name"] for agent in production["isBasedOn"][0]["lyricist"]] == ["Illica"]
    assert production["creator"] == [
        {"@type": "Role", "creator": [{"@type": "Person", "identifier": "anna", "name": "Anna"}], "roleName": "Regie"}
    ]
    [event] = production["events"]
    assert event["startDate"] == "2026-03-01T19:30:00+01:00"
    assert event["location"][0]["latitude"] == pytest.approx(52.5)
    assert event["performer"] == [
        {
            "@type": "PerformanceRole",
            "performer": [{"@type": "Person", "identifier": "bea", "name": "Bea"}],
            "characterName": "Tosca",
        }
    ]


def test_imprecise_premiere_has_no_event_and_keeps_cast_as_creators() -> None:
    production = orif_document({"id": "t", "name": "T"}, [{**TOSCA, "premiere_precision": "month"}])["productions"][0]
    assert production["events"] == []
    assert [role["roleName"] for role in production["creator"]] == ["Regie", "Schauspiel"]


def test_export_preserves_specialized_performer_and_director_titles() -> None:
    production = {
        **TOSCA,
        "memberships": [
            {"role_name": "sopran", "person": _person("singer", "Singer")},
            {"role_name": "musiktheaterregie", "person": _person("director", "Director")},
        ],
    }
    exported = orif_document(THEATRE, [production])["productions"][0]
    assert exported["events"][0]["performer"][0]["roleName"] == "Sopran"
    assert exported["creator"][0]["roleName"] == "Musiktheaterregie"


def test_people_export_groups_by_theatre_and_skips_productions_without_one() -> None:
    other = {**TOSCA, "id": "other", "organization": {"id": "other", "name": "Andere"}}
    documents = orif_documents([{**TOSCA, "organization": THEATRE}, other, {**TOSCA, "id": "loose"}])
    assert [(doc["organization"]["name"], len(doc["productions"])) for doc in documents] == [
        ("Bühne", 1),
        ("Andere", 1),
    ]


def test_orif_round_trip_is_accepted_by_staging() -> None:
    [record] = validate_records(orif_document(THEATRE, [TOSCA]))
    assert (record["title"], record["url"], record["premiere"], record["premiere_precision"]) == (
        "Tosca",
        "https://theatre.example/tosca",
        "2026-03-01T19:30:00",
        "minute",
    )
    assert (record["duration"], record["suggested_min_age"], record["tags"]) == (150, 0, ["oper", "UA"])
    assert (record["text"]["composers"], record["text"]["lyricists"]) == (["Puccini"], ["Illica"])
    assert [(m["name"], m["role_name"], m["character_name"]) for m in record["memberships"]] == [
        ("Anna", "Regie", None),
        ("Bea", "actor", "Tosca"),
    ]


def test_orif_import_converts_offsets_and_rejects_unusable_documents() -> None:
    document = orif_document(THEATRE, [TOSCA])
    document["productions"][0]["events"][0]["startDate"] = "2026-07-01T18:00:00Z"
    staged = as_staged(document)
    assert isinstance(staged, list) and staged[0]["premiere"] == "2026-07-01T20:00"
    with pytest.raises(ValueError, match="version"):
        as_staged({**document, "version": "v1"})
    del document["productions"][0]["events"][0]["url"]
    with pytest.raises(ValueError, match="event url"):
        as_staged(document)
    assert as_staged([{"title": "Staged"}]) == [{"title": "Staged"}]


def test_staging_accepts_legacy_field_names_and_rejects_conflicts() -> None:
    record = {"title": "Alt", "url": "https://theatre.example/alt", "age_rating": 12}
    member = {"name": "Alex", "role": "Schauspiel", "character": "Hamlet"}
    [staged] = validate_records([{**record, "memberships": [member]}])
    assert staged["suggested_min_age"] == 12
    assert (staged["memberships"][0]["role_name"], staged["memberships"][0]["character_name"]) == (
        "Schauspiel",
        "Hamlet",
    )
    with pytest.raises(ValidationError):
        validate_records([{**record, "suggested_min_age": 6}])
