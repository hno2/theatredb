from backend.data_quality import QUALITY_FIELDS

from backend.models import Organization, OrganizationMembership, Person
from frontend.management.quality import render_quality_editor
from frontend.management.uifactory import render_sqlmodel_crud


def main() -> None:
    """Render organization management page."""
    render_sqlmodel_crud(
        model_cls=Organization,
        field_overrides={
            **{field: {"exclude": True} for field in QUALITY_FIELDS},
            "lat": {"exclude": True},  # TODO: Should be added by geocoding, but currently is not
            "lon": {"exclude": True},
        },
        selected_callback=render_quality_editor,
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
            }
        ],
    )


if __name__ == "__main__":
    main()
