from backend.models import Person
from backend.user_management import init_db
from frontend.management.uifactory import render_sqlmodel_crud
from backend.entity_management import get_unique_field_values
from backend.models import ProductionMembership, Production, TextContribution, Text


def main() -> None:
    """Render people management page."""
    init_db()
    # from db get all roles
    render_sqlmodel_crud(
        model_cls=Person,
        field_overrides={
            "type": {"kind": "multiselect", "label": "Types", "options": get_unique_field_values(Person, "type")},
            "urls": {"kind": "list_text", "label": "URLs"},
        },
        inline_relations=[
            {
                "model": ProductionMembership,
                "fk_field": "person_id",
                "title": "Productions",
                "order_by": "role",
                "editable_table": True,
                "fk_autocreate": {
                    "production_id": {
                        "payload_field": "name",
                        "infer_type_from": "role",
                    }
                },
                "field_overrides": {
                    "production_id": {
                        "kind": "fk_select",
                        "fk_model": Production,
                        "fk_display": "name",
                        "label": "Production",
                    },
                    "role": {"label": "Role (e.g. director, actor)"},
                },
            },
            {
                "model": TextContribution,
                "fk_field": "person_id",
                "title": "Text Contributions",
                "order_by": "role",
                "editable_table": False,
                "field_overrides": {
                    "text_id": {
                        "kind": "fk_select",
                        "fk_model": Text,
                        "fk_display": "title",
                        "label": "Text",
                    },
                    "role": {"label": "Role (e.g. author, editor)"},
                },
            },
        ],
    )


if __name__ == "__main__":
    main()
