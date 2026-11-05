import streamlit as st


def render_person(data: dict) -> None:
    """Show a person's available details."""
    st.title(data["name"])
    st.json(data)
