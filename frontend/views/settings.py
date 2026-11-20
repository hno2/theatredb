import streamlit as st

VERIFIED_ONLY = "verified_only"


def verified_only() -> bool:
    """Keep the theatre filter across pages, including direct links that start a new session."""
    return st.session_state.setdefault(VERIFIED_ONLY, st.query_params.get(VERIFIED_ONLY) != "0")


def theatre_filter_link_params() -> dict[str, str]:
    """Carry the non-default theatre filter on links that reload the app."""
    return {} if verified_only() else {VERIFIED_ONLY: "0"}


def _update_verified_only(key: str) -> None:
    st.session_state[VERIFIED_ONLY] = st.session_state[key]


def render_verified_toggle(key: str) -> None:
    """Render a control synchronized with the shared theatre filter."""
    st.session_state[key] = verified_only()
    st.toggle(
        "Nur geprüfte Theater",
        key=key,
        on_change=_update_verified_only,
        args=(key,),
    )
