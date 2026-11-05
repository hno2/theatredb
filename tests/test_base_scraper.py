import pytest

from backend.config import ROLE_ALIASES, ROLE_CONFIG, canonical_role
from backend.ingestion.base_scraper import infer_person_type, parse_date_dmy, parse_duration_minutes


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


def test_duration() -> None:
    assert parse_duration_minutes("1h 30m") == 90
    assert parse_duration_minutes("45m") == 45
    assert parse_duration_minutes("2h") == 120
    assert parse_duration_minutes("invalid") is None
    assert parse_duration_minutes("") is None
    assert parse_duration_minutes("'Dauer:  1h 50, keine Pause'") == 110


def test_parse_date_dmy() -> None:
    assert parse_date_dmy("Premiere: 15.09.2023") == "2023-09-15"
    assert parse_date_dmy("Premiere: 1.1.2020") == "2020-01-01"
    assert parse_date_dmy("Premiere:1.1.2026") == "2026-01-01"
    assert parse_date_dmy("Invalid") is None
    assert parse_date_dmy("") is None
    assert parse_date_dmy("31.12.2024") == "2024-12-31"
