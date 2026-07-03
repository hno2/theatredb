import streamlit as st


def entity_link(label: str, kind: str, entity_id: str) -> None:
    st.markdown(
        f'<a href="?kind={kind}&id={entity_id}" target="_self">{label}</a>',
        unsafe_allow_html=True,
    )
