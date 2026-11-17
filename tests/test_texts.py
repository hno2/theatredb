import uuid

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy import text as sql
from sqlmodel import Session, SQLModel, create_engine, select

from backend import user_management
from backend.models import (
    LineageRelation,
    Production,
    ReconciliationCase,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
)
from backend.reconciliation import merge_entities, preview_entity_merge
from backend.texts import (
    add_lineage,
    add_title,
    credit_line,
    family_roots,
    resolve_staged_text,
    resolve_text,
    resolve_text_case,
    work_family,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_title_sync_normalization_and_languages(session):
    root = Text.model_validate({"title": "Twelfth Night", "languages": ["EN", "de", "en"]})
    session.add(root)
    session.commit()
    assert root.languages == ["en", "de"]
    title = session.exec(select(TextTitle)).one()
    assert title.normalized == "twelfth night" and title.language == "en"
    assert add_title(session, root, "TWELFTH NIGHT!").id == title.id
    root.title = "What You Will"
    session.commit()
    assert len(session.exec(select(TextTitle)).all()) == 2
    root.languages = ["english"]
    with pytest.raises(ValueError, match="two-letter"):
        session.flush()


def test_unknown_and_multilingual_languages(session):
    row = Text.model_validate({"title": "Many", "languages": ["mul"]})
    session.add(row)
    session.commit()
    assert session.exec(select(TextTitle)).one().language is None


def test_family_excludes_based_on_and_rejects_cycles(session):
    novel, musical, english, german = [Text(title=title) for title in ("Novel", "Musical", "English", "German")]
    session.add_all([novel, musical, english, german])
    session.flush()
    add_lineage(session, novel.id, musical.id, "based_on")
    add_lineage(session, musical.id, english.id, "adaptation")
    edge = add_lineage(session, english.id, german.id, "translation")
    assert add_lineage(session, english.id, german.id, "translation").id == edge.id
    assert {row.id for row in work_family(session, german.id)} == {musical.id, english.id, german.id}
    assert [row.id for row in family_roots(session, german.id)] == [musical.id]
    with pytest.raises(ValueError, match="cycle"):
        add_lineage(session, german.id, novel.id, "based_on")


def test_orm_lineage_relationships_are_validated_before_flush(session):
    root, child = Text(title="Root"), Text(title="Child")
    edge = TextLineage(source_text=root, derived_text=child, relation="translation")
    session.add(edge)
    session.flush()
    assert edge.source_text_id == root.id and edge.derived_text_id == child.id
    edge.source_text = child
    with pytest.raises(ValueError, match="itself"):
        session.flush()


def test_translation_resolver_matches_alias_and_translator_idempotently(session):
    root = resolve_text(session, "Twelfth Night", ["William Shakespeare"], language="en")
    assert isinstance(root, Text)
    add_title(session, root, "Was ihr wollt", "de")
    german = resolve_text(session, "Was ihr wollt", ["William Shakespeare"], ["Frank Günther"])
    assert isinstance(german, Text) and german.id != root.id and german.languages == ["de"]
    again = resolve_text(session, "Twelfth Night", ["William Shakespeare"], ["Frank Günther"])
    assert isinstance(again, Text) and again.id == german.id
    assert len(session.exec(select(Text)).all()) == 2
    assert session.exec(select(TextLineage)).one().relation == LineageRelation.TRANSLATION
    assert len(session.exec(select(TextContribution)).all()) == 2


def test_authors_disambiguate_families_or_open_one_review_case(session):
    first = resolve_text(session, "Same", ["First"])
    second = resolve_text(session, "Same", ["Second"])
    assert isinstance(first, Text) and isinstance(second, Text) and first.id != second.id
    assert resolve_text(session, "Same", ["First"]).id == first.id
    case = resolve_text(session, "Same", [])
    assert isinstance(case, ReconciliationCase) and case.operation == "link_text"
    assert resolve_text(session, "Same", []).id == case.id
    production = Production(name="Show")
    session.add(production)
    session.flush()
    case.context = {**case.context, "production_id": str(production.id)}
    resolve_text_case(session, case.id, target_id=second.id, actor_id="test")
    session.commit()
    assert production.text_id == second.id and case.status == "approved"
    assert resolve_text(session, "Same", []).id == second.id


def test_original_metadata_and_based_on_credit_line(session):
    result = resolve_staged_text(
        session,
        {
            "title": "Die Elenden",
            "original_title": "Les Misérables",
            "original_language": "fr",
            "language": "de",
            "authors": ["Victor Hugo"],
            "translators": ["Translator"],
            "genre": "novel",
            "published": "1862",
        },
    )
    assert isinstance(result, Text)
    assert result.languages == ["de"] and result.published_precision == "year"
    roots = family_roots(session, result.id)
    assert len(roots) == 1 and roots[0].languages == ["fr"]
    musical = resolve_staged_text(
        session,
        {
            "title": "Musical",
            "genre": "musical",
            "authors": ["Boublil"],
            "composers": ["Schönberg"],
            "based_on": roots[0],
        },
    )
    assert isinstance(musical, Text)
    assert credit_line(session, musical.id) == "Musik Schönberg · Buch Boublil · nach Victor Hugo"
    english = resolve_text(session, "Musical", ["Boublil"], ["Translator"], language="en")
    assert isinstance(english, Text)
    assert (
        credit_line(session, english.id) == "Musik Schönberg · Buch Boublil · Übersetzung Translator · nach Victor Hugo"
    )


def test_text_merge_keeps_aliases_and_coalesces_edge_notes(session, monkeypatch):
    engine = session.get_bind()
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    first, second, child = [Text(title=title) for title in ("Root", "Alias", "Child")]
    session.add_all([first, second, child])
    session.flush()
    add_lineage(session, first.id, child.id, "adaptation", "First")
    add_lineage(session, second.id, child.id, "adaptation", "Second")
    session.commit()
    ids = [first.id, second.id]
    preview = preview_entity_merge("text", ids, target_id=first.id)
    merge_entities(
        "text", ids, target_id=first.id, revision=preview["revision"], actor_id="test", choices={"title": str(first.id)}
    )
    session.expire_all()
    assert {title.title for title in session.exec(select(TextTitle).where(TextTitle.text_id == first.id))} == {
        "Root",
        "Alias",
    }
    assert session.exec(select(TextLineage)).one().notes == "First\nSecond"


def test_lineage_case_requires_pending_valid_graph(session):
    first, second = Text(title="First"), Text(title="Second")
    session.add_all([first, second])
    session.flush()
    case = ReconciliationCase(
        key="lineage:1",
        entity_type="text",
        operation="text_lineage",
        target_id=second.id,
        incoming={"source_candidate_id": str(first.id), "relation": "based_on"},
        reason="Review",
    )
    session.add(case)
    session.flush()
    resolve_text_case(session, case.id, actor_id="test")
    assert session.exec(select(TextLineage)).one().source_text_id == first.id
    with pytest.raises(ValueError, match="Pending"):
        resolve_text_case(session, case.id)


def test_ambiguous_source_review_creates_derived_text_not_source_link(session):
    sources = [Text(title="Source"), Text(title="Source")]
    session.add_all(sources)
    session.flush()
    case = resolve_text(session, "Musical", ["Book author"], based_on="Source")
    assert isinstance(case, ReconciliationCase) and case.context["pending_derived"]["title"] == "Musical"
    production = Production(name="Stage")
    session.add(production)
    session.flush()
    case.context = {**case.context, "production_id": str(production.id)}
    resolved = resolve_text_case(session, case.id, target_id=sources[1].id, actor_id="test")
    assert resolved.title == "Musical"
    assert production.text_id == resolved.id
    edge = session.exec(select(TextLineage)).one()
    assert (edge.source_text_id, edge.derived_text_id) == (sources[1].id, resolved.id)


def test_ambiguous_translator_review_selects_family_then_creates_translation(session):
    roots = [Text(title="Shared"), Text(title="Shared")]
    session.add_all(roots)
    session.flush()
    case = resolve_text(session, "Shared", ["Author"], ["Translator"])
    assert isinstance(case, ReconciliationCase)
    resolved = resolve_text_case(session, case.id, target_id=roots[1].id, actor_id="test")
    assert resolved.id not in {root.id for root in roots}
    assert family_roots(session, resolved.id)[0].id == roots[1].id
    assert credit_line(session, resolved.id) == "Text Author · Übersetzung Translator"


def test_migration_backfills_titles_languages_relations_and_source_authors(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    first, second, person = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    with engine.begin() as connection:
        config = user_management.alembic_config(connection)
        command.upgrade(config, "0003")
        connection.execute(
            sql("INSERT INTO text (id,title,language) VALUES (:id,'Original','en'),(:other,'Derived',NULL)"),
            {"id": first, "other": second},
        )
        connection.execute(sql("INSERT INTO person (id,name) VALUES (:id,'Author')"), {"id": person})
        connection.execute(
            sql(
                "INSERT INTO textlineage (id,source_text_id,derived_text_id,relation,notes) VALUES (:id,:source,:derived,'unmapped','Keep')"
            ),
            {"id": uuid.uuid4().hex, "source": first, "derived": second},
        )
        connection.execute(
            sql("INSERT INTO textcontribution (id,text_id,person_id,role) VALUES (:id,:text,:person,'source author')"),
            {"id": uuid.uuid4().hex, "text": first, "person": person},
        )
        command.upgrade(config, "head")
        assert "language" not in {column["name"] for column in inspect(connection).get_columns("text")}
    with Session(engine) as migrated:
        assert len(migrated.exec(select(Text)).all()) == 3
        assert len(migrated.exec(select(TextTitle)).all()) == 3
        original = migrated.get(Text, uuid.UUID(first))
        assert original is not None and original.languages == ["en"]
        rows = migrated.exec(select(TextLineage)).all()
        assert all(row.relation == "based_on" for row in rows)
        assert any(row.notes == "Keep\nOriginal relation: unmapped" for row in rows)
        assert migrated.exec(select(TextContribution)).one().role == "author"
    engine.dispose()
