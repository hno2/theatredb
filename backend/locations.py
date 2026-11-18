"""Resolve country and subdivision labels using ISO data and German display names."""

import gettext
from functools import cache

import pycountry


@cache
def _countries() -> dict[str, str]:
    german = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["de"]).gettext
    result = {}
    for country in pycountry.countries:
        for field in ("alpha_2", "alpha_3", "name", "official_name", "common_name"):
            if name := getattr(country, field, None):
                result[name.casefold()] = german(country.name)
                result[german(name).casefold()] = german(country.name)
    return result


@cache
def _subdivisions() -> dict[str, set[tuple[str, str]]]:
    german = gettext.translation("iso3166-2", pycountry.LOCALES_DIR, languages=["de"]).gettext
    # ISO data and its German catalog omit these common local spellings.
    names = {
        "DE-HB": ("Freie Hansestadt Bremen", "Bremen"),
        "DE-HH": ("Freie und Hansestadt Hamburg", "Hamburg"),
        "CH-BE": ("Bern", "Bern"),
        "CH-FR": ("Freiburg", "Freiburg"),
        "CH-SG": ("St. Gallen", "St. Gallen"),
    }
    result: dict[str, set[tuple[str, str]]] = {}
    countries = _countries()
    for item in pycountry.subdivisions:
        alias, region = names.get(item.code, (german(item.name), german(item.name)))
        location = (countries[item.country_code.casefold()], region)
        for name in (item.code, item.name, german(item.name), alias, region):
            result.setdefault(name.casefold(), set()).add(location)
            if item.type == "Canton" and name != item.code:
                result.setdefault(f"kanton {name.casefold()}", set()).add(location)
    return result


def split_country(value: str | None) -> tuple[str | None, str | None]:
    """Return German country and region names; preserve unknown or ambiguous labels."""
    label = (value or "").strip()
    key = label.casefold()
    if country := _countries().get(key):
        return country, None
    matches = _subdivisions().get(key, set())
    return next(iter(matches)) if len(matches) == 1 else (label or None, None)
