from unittest.mock import Mock, call

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from backend.models import LineageRelation, Person, Production, Text, TextContribution, TextLineage
from backend.texts import credit_line
from frontend.management import texts


def test_family_scopes_reads_and_keeps_visible_lineage(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    root = Text(title="First root")
    second = Text(title="Second root")
    adaptation = Text(title="Adaptation")
    distant = Text(title="Distant source")
    original = Text(title="Source original")
    source = Text(title="Source translation", languages=["de"])
    child = Text(title="Based on adaptation")
    grandchild = Text(title="Based on child")
    unrelated = Text(title="Unrelated")
    author = Person(name="Source author")
    with Session(engine, expire_on_commit=False) as session:
        session.add_all([root, second, adaptation, distant, original, source, child, grandchild, unrelated, author])
        session.flush()
        links = [
            (root, adaptation, LineageRelation.ADAPTATION),
            (second, adaptation, LineageRelation.ADAPTATION),
            (original, source, LineageRelation.TRANSLATION),
            (distant, source, LineageRelation.BASED_ON),
            (source, second, LineageRelation.BASED_ON),
            (adaptation, child, LineageRelation.BASED_ON),
            (child, grandchild, LineageRelation.BASED_ON),
        ]
        session.add_all(
            TextLineage(source_text_id=start.id, derived_text_id=end.id, relation=kind) for start, end, kind in links
        )
        session.add_all(
            [
                TextContribution(text_id=original.id, person_id=author.id, role="author"),
                Production(name="Root staging", text_id=root.id),
                Production(name="Adaptation staging", text_id=adaptation.id),
                Production(name="Source staging", text_id=source.id),
                Production(name="Unrelated staging", text_id=unrelated.id),
            ]
        )
        session.commit()
        expected_credit = credit_line(session, adaptation.id)
        queries = []

        def capture(_conn, _cursor, statement, parameters, _context, _many):
            queries.append((statement, parameters))

        ui = Mock()
        monkeypatch.setattr(texts, "st", ui)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            assert texts._family(session, adaptation) == ({root.id, second.id, adaptation.id}, False)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    engine.dispose()

    assert expected_credit == "nach Source author"
    assert ui.write.call_args_list == [
        call(expected_credit),
        call("Vorlage: Distant source → Source translation — Werk: Source original; de"),
        call("Darauf basierende Werke: Based on adaptation, Based on child"),
    ]
    assert [entry.args[0] for entry in ui.text.call_args_list] == [
        "First root — Werk: Second root",
        f"  ↳ {texts.LINEAGE_LABELS[LineageRelation.ADAPTATION]}: Adaptation — Werk: First root, Second root",
        "Second root — Werk: First root",
    ]
    assert ui.caption.call_args_list == [call("Produktionen: Root staging"), call("Produktionen: Adaptation staging")]
    assert all("WHERE" in statement and unrelated.id.hex not in parameters for statement, parameters in queries)
    production_queries = [statement for statement, _ in queries if "FROM production" in statement]
    assert len(production_queries) == 1
    assert "SELECT production.text_id, production.name" in production_queries[0]
