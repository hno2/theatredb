from backend.models import Source
from backend.user_management import init_db
from frontend.management.uifactory import render_sqlmodel_crud


def main() -> None:
    """Render theater award management page."""
    init_db()
    render_sqlmodel_crud(
        model_cls=Source,
    )


if __name__ == "__main__":
    main()
