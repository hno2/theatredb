import pytest

from backend.locations import split_country


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (" deu ", ("Deutschland", None)),
        ("Federal Republic of Germany", ("Deutschland", None)),
        ("Austria", ("Österreich", None)),
        ("Switzerland", ("Schweiz", None)),
        ("France", ("Frankreich", None)),
        ("FR", ("Frankreich", None)),
        ("Bayern", ("Deutschland", "Bayern")),
        ("Freie Hansestadt Bremen", ("Deutschland", "Bremen")),
        ("Freie und Hansestadt Hamburg", ("Deutschland", "Hamburg")),
        ("Wien", ("Österreich", "Wien")),
        ("Kanton Bern", ("Schweiz", "Bern")),
        ("CH-BE", ("Schweiz", "Bern")),
        ("Kanton Freiburg", ("Schweiz", "Freiburg")),
        ("Kanton St. Gallen", ("Schweiz", "St. Gallen")),
        ("Kanton Tessin", ("Schweiz", "Tessin")),
        ("Kanton Capellen", ("Luxemburg", "Capellen")),
        ("New York", ("Vereinigte Staaten", "New York")),
        ("San José", ("San José", None)),
        (" Unknown place ", ("Unknown place", None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_country(label, expected):
    assert split_country(label) == expected
