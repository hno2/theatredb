"""Safe inline description previews, with no server interaction on hover."""

from __future__ import annotations

import re

import streamlit as st
from streamlit.components.v2 import component

from backend import mentions
from frontend.views.helpers import entity_url
from frontend.views.settings import verified_only

CSS = """
:host { color: var(--st-text-color); font-family: var(--st-font); }
#description { overflow-wrap: anywhere; line-height: 1.6; }
p { margin: 0 0 1rem; }
p:last-child { margin-bottom: 0; }
a { color: var(--st-link-color); text-decoration: underline; }
a:focus-visible { outline: 2px solid var(--st-primary-color); outline-offset: 3px; }
table { border-collapse: collapse; }
th, td { padding: .4rem; border: 1px solid var(--st-border-color); }
pre { overflow-x: auto; }
img { max-width: 100%; }
#preview {
    box-sizing: border-box;
    position: fixed; z-index: 1000; width: min(320px, calc(100vw - 24px));
    padding: 12px; border: 1px solid var(--st-border-color); border-radius: 8px;
    background: var(--st-background-color); color: var(--st-text-color);
    box-shadow: 0 4px 16px rgba(0,0,0,.2); line-height: 1.5;
    max-height: calc(100vh - 24px); overflow-y: auto;
}
#preview[hidden] { display: none; }
#preview strong, #preview small { display: block; }
#preview small { margin-bottom: 6px; }
#preview p { margin: 6px 0 0; }
"""

JS = """
export default function({parentElement, data}) {
    const description = parentElement.querySelector("#description");
    const preview = parentElement.querySelector("#preview");
    description.innerHTML = data.html;
    let active = null, timer = null;
    const hide = () => {
        clearTimeout(timer);
        preview.hidden = true;
        if (active) active.removeAttribute("aria-describedby");
        active = null;
    };
    const later = () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
            if (!active?.matches(":focus") && !preview.matches(":hover")) hide();
        }, 150);
    };
    const position = () => {
        if (!active || preview.hidden) return;
        const rect = active.getBoundingClientRect();
        if (rect.bottom < 0 || rect.top > window.innerHeight) { hide(); return; }
        const width = preview.offsetWidth, height = preview.offsetHeight;
        preview.style.left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)) + "px";
        const below = rect.bottom + 6;
        preview.style.top = Math.max(12, Math.min(
            below + height <= window.innerHeight - 12 ? below : rect.top - height - 6,
            window.innerHeight - height - 12
        )) + "px";
    };
    const names = {person: "Person", text: "Text", organization: "Theater"};
    description.querySelectorAll("[data-mention]").forEach(span => {
        const item = data.previews[Number(span.dataset.mention)];
        if (!item.url) return;
        const link = document.createElement("a");
        link.textContent = span.textContent;
        link.href = item.url;
        link.target = "_self";
        span.replaceWith(link);
        const show = () => {
            hide();
            active = link;
            preview.replaceChildren();
            const type = document.createElement("small");
            type.textContent = names[item.kind];
            const name = document.createElement("strong");
            name.textContent = item.name;
            const facts = document.createElement("small");
            facts.textContent = item.facts;
            const excerpt = document.createElement("p");
            excerpt.textContent = item.excerpt;
            preview.append(type, name, facts, excerpt);
            link.setAttribute("aria-describedby", preview.id);
            preview.hidden = false;
            position();
        };
        link.addEventListener("mouseenter", show);
        link.addEventListener("mouseleave", later);
        link.addEventListener("focus", show);
        link.addEventListener("blur", later);
    });
    preview.onmouseenter = () => clearTimeout(timer);
    preview.onmouseleave = later;
    const escape = event => { if (event.key === "Escape") hide(); };
    document.addEventListener("keydown", escape);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => {
        hide();
        document.removeEventListener("keydown", escape);
        window.removeEventListener("resize", position);
        window.removeEventListener("scroll", position, true);
    };
}
"""

_COMPONENT = component(
    "theaterdb.description_mentions",
    html='<div id="description"></div><div id="preview" role="tooltip" hidden></div>',
    css=CSS,
    js=JS,
)


@st.cache_data(ttl=30)
def _catalog() -> mentions.Catalog:
    """Cache lightweight labels for 30 seconds; graph and decisions are read fresh."""
    return mentions.load_catalog()


def render_description(description: str, kind: str, entity_id: object, *, key: str = "") -> None:
    """Keep ordinary Markdown native; mount rich previews only for resolved links."""
    labels = _catalog()
    if not entity_id or not labels.candidates:
        st.markdown(description)
        return
    # shortcut: CCv2 renders source Markdown, use native rendering for Streamlit directives until needed.
    if re.search(r":[a-z-]+\[|:material/|:[\w+-]+:|\$[^$\n]+\$|```mermaid", description):
        st.markdown(description)
        return
    html, previews = mentions.render_description(kind, str(entity_id), description, labels)
    for item in previews:
        visible = item["kind"] != "organization" or not verified_only() or item["verified"] == "True"
        item["url"] = entity_url(item["kind"], item["id"], path="detail") if visible else ""
    if not any(item["url"] for item in previews):
        st.markdown(description)
        return
    _COMPONENT(key=f"description_{kind}_{entity_id}_{key}", data={"html": html, "previews": previews})
