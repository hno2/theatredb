from typing import NoReturn
from urllib.error import URLError

import pytest

from backend import gnd, wikidata
from frontend.management import authority


def _offline(*_args: object, **_kwargs: object) -> NoReturn:
    raise URLError("network disabled in tests")


@pytest.fixture(autouse=True)
def _no_authority_network(monkeypatch):
    """Management pages look up GND/Wikidata on selection; tests must never reach them."""
    monkeypatch.setattr(gnd, "urlopen", _offline)
    monkeypatch.setattr(wikidata, "urlopen", _offline)
    for cached in (authority._search, authority._fetch, authority._predecessors):
        cached.clear()
