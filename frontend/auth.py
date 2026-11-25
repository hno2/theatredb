"""Who is using the app: native Streamlit OIDC login, or an explicit local development user."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import streamlit as st

from backend.library import find_or_create_user
from backend.user_management import get_session  # also loads .env

DEV_KEY = "dev:local"


@dataclass(frozen=True)
class Principal:
    """The signed-in user as trusted server-side state."""

    id: uuid.UUID
    name: str
    is_admin: bool
    dev: bool = False


def dev_auth_enabled() -> bool:
    """`THEATERDB_DEV_AUTH=1` signs everyone in as a local admin; refused when `THEATERDB_ENV=production`."""
    value = os.getenv("THEATERDB_DEV_AUTH", "0")
    if value not in {"0", "1"}:
        raise RuntimeError("THEATERDB_DEV_AUTH must be 0 or 1")
    if value == "1" and os.getenv("THEATERDB_ENV", "development") == "production":
        raise RuntimeError("THEATERDB_DEV_AUTH=1 is not allowed with THEATERDB_ENV=production")
    return value == "1"


def login_provider() -> str | bool | None:
    """Provider name for `st.login`, None for a single unnamed provider, False if login is not configured."""
    try:
        auth = st.secrets.get("auth")
    except FileNotFoundError, st.errors.StreamlitSecretNotFoundError:
        return False
    if not auth:
        return False
    providers = [name for name, value in auth.items() if hasattr(value, "items")]
    return providers[0] if providers else None


def current_user() -> Principal | None:
    """Real OIDC identity wins; the dev user only exists when explicitly enabled."""
    if st.user.get("is_logged_in"):
        issuer, subject = st.user.get("iss"), st.user.get("sub")
        if not issuer or not subject:
            st.error("Anmeldung unvollständig: Der Anbieter hat keine Kennung geliefert.")
            return None
        key, name, admin, dev = f"oidc:{issuer}|{subject}", st.user.get("name"), False, False
    elif dev_auth_enabled():
        key, name, admin, dev = DEV_KEY, "Dev", True, True
    else:
        return None
    with get_session() as session:
        user = find_or_create_user(session, str(key), str(name or "") or None, is_admin=admin)
        if not user.is_active:
            return None
        return Principal(user.id, user.display_name or "Ich", user.is_admin, dev)


def login_button(key: str) -> None:
    """Native login, or a hint when the operator has not configured it yet."""
    provider = login_provider()
    if provider is False:
        st.caption("Anmeldung ist hier noch nicht eingerichtet.")
    else:
        st.button("Anmelden", icon=":material/login:", key=key, on_click=st.login, args=(provider,) if provider else ())


def principal() -> Principal | None:
    """The user resolved by `frontend/app.py` for this run; pages rendered alone are anonymous."""
    value = st.session_state.get("_principal")
    return value if isinstance(value, Principal) else None
