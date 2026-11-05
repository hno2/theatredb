from backend.ingestion.base_scraper import parse_date_dmy, parse_duration_minutes


def test_duration():
    assert parse_duration_minutes("1h 30m") == 90
    assert parse_duration_minutes("45m") == 45
    assert parse_duration_minutes("2h") == 120
    assert parse_duration_minutes("invalid") is None
    assert parse_duration_minutes("") is None
    assert parse_duration_minutes("'Dauer:  1h 50, keine Pause'") == 110


def test_parse_date_dmy():
    assert parse_date_dmy("Premiere: 15.09.2023") == "2023-09-15"
    assert parse_date_dmy("Premiere: 1.1.2020") == "2020-01-01"
    assert parse_date_dmy("Premiere:1.1.2026") == "2026-01-01"
    assert parse_date_dmy("Invalid") is None
    assert parse_date_dmy("") is None
    assert parse_date_dmy("31.12.2024") == "2024-12-31"
