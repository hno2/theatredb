import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend.models import Production, ProductionTag, TagOrigin, TagStatus, Text
from backend.taxonomy import (
    decide_suggestion,
    facet_filters,
    prepare_production_merge,
    set_production_tags,
    sync_vocabulary,
)
from backend.texts import resolve_staged_text


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'taxonomy.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        sync_vocabulary(session)
        session.commit()
        yield session
    engine.dispose()


def _assignments(session: Session, production: Production) -> dict[str, tuple[TagStatus, TagOrigin]]:
    rows = session.exec(select(ProductionTag).where(ProductionTag.production_id == production.id))
    return {row.tag_code: (row.status, row.origin) for row in rows}


def test_legacy_tags_follow_source_but_manual_decisions_win(session):
    production = Production(name="Stück", tags=["Tanz", "UA", "Unbekannt"])
    session.add(production)
    session.commit()
    published = (TagStatus.PUBLISHED, TagOrigin.SOURCE)
    assert _assignments(session, production) == {"form.tanz": published, "premiere.ua": published}

    set_production_tags(session, production.id, {"form.tanz", "theme.migration"}, "editor")
    session.commit()
    assert _assignments(session, production)["premiere.ua"] == (TagStatus.REJECTED, TagOrigin.MANUAL)

    production.tags = ["UA", "Schauspiel"]
    session.add(production)
    session.commit()
    assert _assignments(session, production) == {
        "form.tanz": (TagStatus.PUBLISHED, TagOrigin.MANUAL),
        "theme.migration": (TagStatus.PUBLISHED, TagOrigin.MANUAL),
        "premiere.ua": (TagStatus.REJECTED, TagOrigin.MANUAL),
        "form.schauspiel": published,
    }


def test_facets_or_within_and_across_published_only(session):
    dance = Production(name="Tanz UA", tags=["Tanz", "UA"])
    opera = Production(name="Oper UA", tags=["Oper/Operette", "UA"])
    play = Production(name="Schauspiel", tags=["Schauspiel"])
    guess = Production(name="Vorschlag", tags=["Tanz"])
    session.add_all([dance, opera, play, guess])
    session.flush()
    session.add(
        ProductionTag(
            production_id=guess.id, tag_code="premiere.ua", status=TagStatus.SUGGESTED, origin=TagOrigin.MODEL
        )
    )
    session.commit()

    def names(selected: dict[str, list[str]]) -> set[str]:
        return set(session.exec(select(Production.name).where(*facet_filters(selected))))

    assert names({"form": ["form.tanz", "form.oper"]}) == {"Tanz UA", "Oper UA", "Vorschlag"}
    assert names({"form": ["form.tanz", "form.oper"], "premiere": ["premiere.ua"]}) == {"Tanz UA", "Oper UA"}

    decide_suggestion(session, guess.id, "premiere.ua", accept=True, actor_id="editor")
    session.commit()
    assert "Vorschlag" in names({"premiere": ["premiere.ua"]})


def test_merge_preparation_keeps_manual_decision(session):
    target = Production(name="A", tags=["Tanz"])
    duplicate = Production(name="B")
    session.add_all([target, duplicate])
    session.commit()
    set_production_tags(session, duplicate.id, {"form.tanz"}, "editor")
    session.commit()

    prepare_production_merge(session, target.id, duplicate.id)
    rows = session.exec(select(ProductionTag).where(ProductionTag.tag_code == "form.tanz")).all()
    assert [(row.production_id, row.origin) for row in rows] == [(duplicate.id, TagOrigin.MANUAL)]


def test_staged_text_description_fills_only_missing_value(session):
    text = resolve_staged_text(session, {"title": "Neu", "description": "Eine Familie zerbricht."})
    assert isinstance(text, Text)
    assert text.description == "Eine Familie zerbricht."
    again = resolve_staged_text(session, {"title": "Neu", "description": "Andere Beschreibung"})
    assert again is text and text.description == "Eine Familie zerbricht."
