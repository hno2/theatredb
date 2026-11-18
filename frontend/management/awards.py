from backend.models import Award
from frontend.management.uifactory import render_sqlmodel_crud


def main() -> None:
    """Render theater award management page."""
    render_sqlmodel_crud(
        model_cls=Award,
    )


if __name__ == "__main__":
    main()
