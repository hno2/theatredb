from typing import NoReturn
from urllib.error import URLError

import pytest
import streamlit as st

from backend import gnd, wikidata


def _offline(*_args: object, **_kwargs: object) -> NoReturn:
    raise URLError("network disabled in tests")


@pytest.fixture(autouse=True)
def _no_authority_network(monkeypatch):
    """Management pages look up GND/Wikidata on selection; tests must never reach them."""
    monkeypatch.setattr(gnd, "urlopen", _offline)
    monkeypatch.setattr(wikidata, "urlopen", _offline)
    st.cache_data.clear()
