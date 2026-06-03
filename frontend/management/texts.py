from backend.models import Person, Text, TextContribution, TextLineage
from backend.user_management import init_db
from frontend.management.uifactory import render_sqlmodel_crud


def main() -> None:
    """Render text management page."""
    init_db()
    render_sqlmodel_crud(
        model_cls=Text,
        field_overrides={
            "contributions": {"exclude": True},
            "productions": {"exclude": True},
            "lineages_as_source": {"exclude": True},
            "lineages_as_derived": {"exclude": True},
        },
        inline_relations=[
            {
                "model": TextContribution,
                "fk_field": "text_id",
                "title": "Authors & Contributions",
                "order_by": "role",
                "editable_table": True,
                "fk_autocreate": {
                    "person_id": {
                        "payload_field": "name",
                        "infer_type_from": "role",
                    }
                },
                "field_overrides": {
                    "person_id": {
                        "kind": "fk_select",
                        "fk_model": Person,
                        "fk_display": "name",
                        "label": "Person",
                    },
                    "role": {
                        "label": "Role (author, translator, adapter, source author)",
                    },
                },
            },
            {
                "model": TextLineage,
                "fk_field": "source_text_id",
                "title": "Derived Texts",
                "order_by": "relation",
                "editable_table": True,
                "field_overrides": {
                    "derived_text_id": {
                        "kind": "fk_select",
                        "fk_model": Text,
                        "fk_display": "title",
                        "label": "Derived Text",
                        "new_payload_field": "title",
                    },
                    "relation": {"label": "Relation (adaptation, translation, etc.)"},
                    "notes": {"label": "Notes"},
                },
            },
            {
                "model": TextLineage,
                "fk_field": "derived_text_id",
                "title": "Source Texts",
                "order_by": "relation",
                "editable_table": True,
                "field_overrides": {
                    "source_text_id": {
                        "kind": "fk_select",
                        "fk_model": Text,
                        "fk_display": "title",
                        "label": "Source Text",
                        "new_payload_field": "title",
                    },
                    "relation": {"label": "Relation (adaptation, translation, etc.)"},
                    "notes": {"label": "Notes"},
                },
            },
        ],
    )


if __name__ == "__main__":
    main()
