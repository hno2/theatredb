from backend.models import Organization, Person, Production, ProductionMembership, Text
from backend.user_management import init_db
from frontend.management.uifactory import render_sqlmodel_crud


def main() -> None:
    """Render performance management page."""
    init_db()
    render_sqlmodel_crud(
        model_cls=Production,
        field_overrides={
            "venue_id": {
                "kind": "fk_select",
                "fk_model": Organization,
                "fk_display": "name",
                "label": "Venue",
            },
            "text_id": {
                "kind": "fk_select",
                "fk_model": Text,
                "fk_display": "title",
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
                    "role": {"label": "Role (e.g. director, actor)"},
                },
            }
        ],
    )


if __name__ == "__main__":
    main()
