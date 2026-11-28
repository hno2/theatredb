import pytest

from backend.config import ROLE_ALIASES, ROLE_CONFIG, canonical_role
from backend.ingestion.scraper_utils import infer_person_type, parse_date_dmy, parse_duration_minutes


@pytest.mark.parametrize(("alias", "expected"), list(ROLE_ALIASES.items()))
def test_shared_role_aliases(alias: str, expected: str) -> None:
    assert canonical_role(f" {alias.upper()} ") == expected
    if expected != "other":
        assert infer_person_type(alias.upper()) == expected


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("Vermittlung", "theatre_educator"),
        ("Theaterpädagogische Vermittlung", "theatre_educator"),
        ("Theaterpädagogin", "theatre_educator"),
        ("Konzept und Vermittlung", "theatre_educator"),
        ("Musikalische Leitung", "composer"),
        ("Regie und Bühne", "director"),
        ("Kostüm und Musik", "costume_designer"),
        ("Hamlet", "actor"),
        ("", "actor"),
    ],
)
def test_infer_person_type(role: str, expected: str) -> None:
    assert infer_person_type(role) == expected


def test_role_config_uses_canonical_keys() -> None:
    assert all(role == role.casefold() for role in ROLE_CONFIG)
    assert all(alias == alias.casefold() for alias in ROLE_ALIASES)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Gesang", "singer"),
        ("Gesang (männliche Solopartien)", "singer"),
        ("Gesang (weibliche Solopartien)", "singer"),
        ("Gesang (diverse Solopartien)", "singer"),
        ("Sopran", "singer"),
        ("Musiktheaterregie", "director"),
        ("Schauspiel (diverse Rollen)", "actor"),
        ("sonstige Schauspiel", "actor"),
        ("Autor*in", "author"),
        ("Dirigat / Musikalische Leitung", "conductor"),
        ("Tanz (weibliche Rollen)", "dancer"),
        ("Tanz (männliche Rollen)", "dancer"),
        ("Choreografie / Direktion", "choreographer"),
        ("Assistenz Schauspielregie", "assistant_director"),
        ("Assistenz Musiktheaterregie", "assistant_director"),
        ("Leitung Schauspielregie", "directing_leadership"),
        ("Chorleitung", "choir_director"),
        ("Puppenspiel / Figurentheater", "puppeteer"),
        ("Sounddesign", "sound_designer"),
    ],
)
def test_requested_role_classifications(title: str, expected: str) -> None:
    assert canonical_role(title) == expected
    assert infer_person_type(title) == expected


def test_unapproved_roles_are_not_mapped() -> None:
    assert canonical_role("Performance") == "performance"
    assert canonical_role("sonstige Musiktheater") == "sonstige musiktheater"
    assert canonical_role("Assistenz Dramaturgie") == "assistenz dramaturgie"
    assert infer_person_type("Performance") == "performance"
    assert infer_person_type("sonstige Musiktheater") == "sonstige musiktheater"
    assert infer_person_type("Assistenz Dramaturgie") == "assistenz dramaturgie"
    assert infer_person_type("Leitung Musiktheaterregie") == "leitung musiktheaterregie"


def test_duration() -> None:
    assert parse_duration_minutes("1h 30m") == 90
    assert parse_duration_minutes("45m") == 45
    assert parse_duration_minutes("2h") == 120
    assert parse_duration_minutes("invalid") is None
    assert parse_duration_minutes("") is None
    assert parse_duration_minutes("'Dauer:  1h 50, keine Pause'") == 110
    assert parse_duration_minutes("1 Stunden 05 Minuten") == 65
    assert parse_duration_minutes("1 Stunde 30") == 90
    assert parse_duration_minutes("2 Stunden") == 120
    assert parse_duration_minutes("45 Minuten") == 45
    assert parse_duration_minutes("1 Minute") == 1


def test_parse_date_dmy() -> None:
    assert parse_date_dmy("Premiere: 15.09.2023") == "2023-09-15"
    assert parse_date_dmy("Premiere: 1.1.2020") == "2020-01-01"
    assert parse_date_dmy("Premiere:1.1.2026") == "2026-01-01"
    assert parse_date_dmy("Invalid") is None
    assert parse_date_dmy("") is None
    assert parse_date_dmy("31.12.2024") == "2024-12-31"
