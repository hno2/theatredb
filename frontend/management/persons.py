from backend.models import PERSON_GENDERS, EntityType, Person, Production, ProductionMembership, Text, TextContribution
from frontend.management.authority import render_authority_panel
from frontend.management.filters import field_value_options
from frontend.management.uifactory import render_sqlmodel_crud

GENDER_LABELS = {"m": "männlich", "f": "weiblich", "none": "kein Geschlecht", "div": "divers"}


def main() -> None:
    """Render people management page."""
    render_sqlmodel_crud(
        model_cls=Person,
        merge_kind="person",
        tabbed_relations=True,
        field_overrides={
            "type": {"kind": "multiselect", "label": "Types", "options": field_value_options(Person, "type") or []},
            "urls": {"kind": "list_text", "label": "URLs"},
            "gender": {
                "kind": "choice",
                "label": "Geschlecht (leer = unbekannt)",
                "options": list(PERSON_GENDERS),
                "option_labels": GENDER_LABELS,
            },
        },
        selected_callback=lambda row: render_authority_panel(EntityType.PERSON, row),
        inline_relations=[
            {
                "model": ProductionMembership,
                "fk_field": "person_id",
                "title": "Productions",
                "order_by": "role_name",
                "editable_table": True,
                "sortable": True,
                "production_context": True,
                "fk_autocreate": {
                    "production_id": {
                        "payload_field": "name",
                        "infer_type_from": "role_name",
                    }
                },
                "field_overrides": {
                    "production_id": {
                        "kind": "fk_select",
                        "fk_model": Production,
                        "fk_display": "name",
                        "label": "Production",
                    },
                    "role_name": {"label": "Role (e.g. director, actor)"},
                },
            },
            {
                "model": TextContribution,
                "fk_field": "person_id",
                "title": "Text Contributions",
                "order_by": "role",
                "editable_table": True,
                "sortable": True,
                "merge": {"kind": "text", "field": "text_id"},
                "sort_by": "text_id",
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
