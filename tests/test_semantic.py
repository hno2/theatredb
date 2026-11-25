from uuid import uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import semantic
from backend.models import EMBEDDING_DIMENSIONS, DescriptionChunk, Production, ProductionTag, TagOrigin, TagStatus
from backend.taxonomy import sync_vocabulary

KEYWORDS = ("flucht", "liebe")


def fake_encoder(texts, _instruction):
    """One axis per keyword plus a shared baseline, so similarity is predictable."""
    vectors = []
    for text in texts:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        vector[-1] = 1.0
        for axis, word in enumerate(KEYWORDS):
            vector[axis] = 3.0 if word in text.lower() else 0.0
        norm = sum(value * value for value in vector) ** 0.5
        vectors.append([value / norm for value in vector])
    return vectors


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'semantic.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        sync_vocabulary(session)
        session.commit()
        yield session
    engine.dispose()


def test_chunks_merge_paragraphs_and_split_long_ones():
    assert semantic.chunk_description("Eins.\n\nZwei.", size=20) == ["Eins.\n\nZwei."]
    assert semantic.chunk_description("Erster Satz. Zweiter Satz.", size=15) == ["Erster Satz.", "Zweiter Satz."]


def test_rrf_rewards_agreement_between_rankings():
    a, b, c = uuid4(), uuid4(), uuid4()
    assert semantic.reciprocal_rank_fusion([a, b], [c, b])[0] == b


def test_index_skips_unchanged_and_refreshes_edits(session):
    first = Production(name="Exil", description="Eine Flucht über das Meer.")
    second = Production(name="Ohne", description="Etwas anderes.")
    session.add_all([first, second, Production(name="Leer")])
    session.commit()

    assert semantic.index_descriptions(session, limit=1, encoder=fake_encoder) == 1
    assert semantic.index_descriptions(session, encoder=fake_encoder) == 1
    assert semantic.index_descriptions(session, encoder=fake_encoder) == 0

    first.description = "Eine Liebe in Zeiten der Flucht."
    second.description = None
    session.add_all([first, second])
    session.commit()
    assert semantic.index_descriptions(session, encoder=fake_encoder) == 1
    chunks = session.exec(select(DescriptionChunk)).all()
    assert [(chunk.production_id, chunk.content) for chunk in chunks] == [(first.id, first.description)]


def test_suggestions_never_override_editor_decisions(session):
    exile = Production(name="Exil", description="Eine Flucht über das Meer.")
    session.add(exile)
    session.commit()
    semantic.index_descriptions(session, encoder=fake_encoder)

    assert semantic.suggest_tags(session, encoder=fake_encoder) == 1
    row = session.exec(select(ProductionTag).where(ProductionTag.production_id == exile.id)).one()
    assert (row.tag_code, row.status, row.origin) == ("theme.migration", TagStatus.SUGGESTED, TagOrigin.MODEL)
    assert row.evidence == exile.description
    assert semantic.suggest_tags(session, encoder=fake_encoder) == 0

    row.status, row.origin = TagStatus.REJECTED, TagOrigin.MANUAL
    session.add(row)
    session.commit()
    exile.description = "Eine Flucht über die Berge."
    session.add(exile)
    session.commit()
    semantic.index_descriptions(session, encoder=fake_encoder)
    assert semantic.suggest_tags(session, encoder=fake_encoder) == 0
    session.refresh(row)
    assert (row.status, row.origin) == (TagStatus.REJECTED, TagOrigin.MANUAL)
