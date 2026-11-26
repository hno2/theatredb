"""Read-only HTTP access to the public archive."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from sqlmodel import SQLModel

from backend.entity_management import (
    CatalogModel,
    EntityNotFoundError,
    catalog_page,
    get_entity_graph,
    get_organization_profile,
    get_people_productions,
)
from backend.models import Organization, Person, Production, Text
from backend.orif import ORIF_MEDIA_TYPE, orif_document, orif_documents

EntityId = Annotated[UUID, Path(description="Current or merged entity UUID.")]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Entity not found"}}


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

    @router.get("/{entity_id}", responses=NOT_FOUND)
    def read_entity(entity_id: EntityId) -> dict[str, Any]:
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


class ORIFResponse(JSONResponse):
    """JSON-LD response for Open Repertoire documents."""

    media_type = ORIF_MEDIA_TYPE


orif = APIRouter(tags=["open-repertoire"], default_response_class=ORIFResponse, responses=NOT_FOUND)


def _graph(model: CatalogModel, entity_id: UUID, max_depth: int = 3) -> dict[str, Any]:
    try:
        return get_entity_graph(model, str(entity_id), max_depth=max_depth)
    except EntityNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@orif.get("/organizations/{entity_id}/orif")
def read_organization_orif(entity_id: EntityId) -> dict[str, Any]:
    """Export a theatre and all its productions as one Open Repertoire (ORIF v2) document."""
    try:
        profile = get_organization_profile(str(entity_id))
    except EntityNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return orif_document(profile, profile["productions"])


@orif.get("/productions/{entity_id}/orif")
def read_production_orif(entity_id: EntityId) -> dict[str, Any]:
    """Export one production, wrapped in its theatre's Open Repertoire (ORIF v2) document."""
    documents = orif_documents([_graph(Production, entity_id)])
    if not documents:
        raise HTTPException(status_code=404, detail="Production has no theatre; ORIF requires an organization")
    return documents[0]


@orif.get("/people/{entity_id}/orif")
def read_person_orif(entity_id: EntityId) -> list[dict[str, Any]]:
    """Export a person's productions as Open Repertoire (ORIF v2) documents, one per theatre."""
    person = _graph(Person, entity_id, max_depth=0)
    return orif_documents(get_people_productions([str(person["id"])]))


app.include_router(orif, prefix="/api")
