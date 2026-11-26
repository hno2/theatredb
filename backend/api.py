"""Read-only HTTP access to the public archive."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Path, Query
from sqlmodel import SQLModel

from backend.entity_management import CatalogModel, EntityNotFoundError, catalog_page, get_entity_graph
from backend.models import Organization, Person, Production, Text


class CatalogPage(SQLModel):
    """A public catalog page and its total matching count."""

    items: list[dict[str, Any]]
    total: int


def _catalog_router(model: CatalogModel) -> APIRouter:
    router = APIRouter()

    @router.get("", response_model=CatalogPage)
    def read_catalog(
        q: Annotated[str, Query(description="Search the public catalog; an empty query lists all records.")] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 12,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> CatalogPage:
        """List or search records using the archive's existing ranking."""
        items, total = catalog_page(model, q, [], limit=limit, offset=offset)
        return CatalogPage(items=items, total=total)

    @router.get("/{entity_id}", responses={404: {"description": "Entity not found"}})
    def read_entity(entity_id: Annotated[UUID, Path(description="Current or merged entity UUID.")]) -> dict[str, Any]:
        """Get public entity data, relationships, and source links."""
        try:
            return get_entity_graph(model, str(entity_id))
        except EntityNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return router


app = FastAPI(title="TheatreDB", version="0.1.0")

for resource, model in (
    ("organizations", Organization),
    ("productions", Production),
    ("people", Person),
    ("texts", Text),
):
    app.include_router(_catalog_router(model), prefix=f"/api/{resource}", tags=[resource])
