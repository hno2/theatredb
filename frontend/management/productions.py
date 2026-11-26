from uuid import UUID

import streamlit as st

from backend.models import Organization, Person, Production, ProductionMembership, TagStatus, Text
from backend.taxonomy import (
    FACETS,
    LAYER_LABELS,
    ORIGIN_LABELS,
    decide_suggestion,
    facet_options,
    production_tags,
    set_production_tags,
)
from backend.user_management import get_session
from frontend.management.texts import text_labels
from frontend.management.uifactory import _actor_id, render_sqlmodel_crud


def _tag_editor(row: dict) -> None:
    production_id = UUID(str(row["id"]))
    with get_session() as session:
        options = facet_options(session)
        assigned = production_tags(session, production_id)
    published = {item["code"] for item in assigned if item["status"] == TagStatus.PUBLISHED}
    with st.expander("Profil & Inhalt", expanded=True, icon=":material/sell:"):
        st.caption("Gespeicherte Auswahl ist redaktionell und hat Vorrang vor Quell- und Modellzuordnungen.")
        with st.form(f"production_tags_{production_id}"):
            selected: set[str] = set()
            columns = st.columns(3)
            for index, (facet, values) in enumerate(options.items()):
                labels = dict(values)
                with columns[index % 3]:
                    selected.update(
                        st.multiselect(
                            f"{LAYER_LABELS[FACETS[facet][0]]}: {FACETS[facet][1]}",
                            list(labels),
                            default=[code for code in labels if code in published],
                            format_func=lambda code, labels=labels: labels[code],
                            key=f"production_tags_{production_id}_{facet}",
                        )
                    )
            if st.form_submit_button("Merkmale speichern", icon=":material/save:"):
                with get_session() as session:
                    set_production_tags(session, production_id, selected, _actor_id())
                    session.commit()
                st.rerun()
        for item in (item for item in assigned if item["status"] == TagStatus.SUGGESTED):
            with st.container(horizontal=True, vertical_alignment="center"):
                score = f" · Ähnlichkeit {item['score']:.2f}" if item["score"] is not None else ""
                st.markdown(
                    f"Vorschlag **{item['label']}** ({ORIGIN_LABELS[item['origin']]}{score})",
                    help=item["evidence"],
                )
                for accept, label in ((True, "Übernehmen"), (False, "Ablehnen")):
                    if st.button(label, key=f"suggestion_{production_id}_{item['code']}_{accept}"):
                        with get_session() as session:
                            decide_suggestion(session, production_id, item["code"], accept=accept, actor_id=_actor_id())
                            session.commit()
                        st.rerun()


def main() -> None:
    """Render performance management page."""
    render_sqlmodel_crud(
        model_cls=Production,
        selected_callback=_tag_editor,
        field_overrides={
            "organization_id": {
                "kind": "fk_select",
                "fk_model": Organization,
                "fk_display": "name",
                "label": "Venue",
            },
            "text_id": {
                "kind": "fk_select",
                "fk_model": Text,
                "fk_display": "title",
                "fk_labels": text_labels,
                "label": "Text",
                "new_payload_field": "title",
                "related_people_role": "author",
                "related_people_label": "Authors (new or existing)",
            },
            "memberships": {"exclude": True},
        },
        inline_relations=[
            {
                "model": ProductionMembership,
                "fk_field": "production_id",
                "title": "Cast & Crew",
                "order_by": "role_name",
                "editable_table": True,
                "fk_autocreate": {
                    "person_id": {
                        "payload_field": "name",
                        "infer_type_from": "role_name",
                        "rename_existing": True,
                    }
                },
                "field_overrides": {
                    "person_id": {
                        "kind": "fk_select",
                        "fk_model": Person,
                        "fk_display": "name",
                        "label": "Person",
                    },
                    "role_name": {"label": "Role (e.g. director, actor)"},
                },
            }
        ],
    )


if __name__ == "__main__":
    main()
