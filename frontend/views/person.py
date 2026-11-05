import streamlit as st


def render_person(data: dict) -> None:
    st.title(data["name"])
    st.json(data)
