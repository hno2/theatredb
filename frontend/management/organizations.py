from backend.models import Organization, OrganizationMembership, Person
from backend.user_management import init_db
from frontend.management.uifactory import render_sqlmodel_crud


def main() -> None:
    """Render organization management page."""
    init_db()
    render_sqlmodel_crud(
        model_cls=Organization,
        field_overrides={
            "lat": {"exclude": True}, #TODO: Should be added by geocoding, but currently is not
            "lon": {"exclude": True},},
        inline_relations=[
            {
                "model": OrganizationMembership,
                "fk_field": "organization_id",
                "title": "Leitung & Mitglieder",
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
                    "role": {"label": "Rolle"},
                },
            }]
    )


if __name__ == "__main__":
    main()
