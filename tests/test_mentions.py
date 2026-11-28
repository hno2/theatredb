import datetime as dt
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import mentions, reconciliation
from backend.models import (
    EntityRedirect,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Text,
    TextContribution,
    TextTitle,
)
from frontend.views import mentions as view


@pytest.fixture
def graph(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mentions.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("backend.user_management._get_engine", lambda: engine)
    with Session(engine, expire_on_commit=False) as session:
        person = Person(name="Ada Beispiel")
        first, second = Text(title="Hamlet"), Text(title="Hamlet")
        theatre = Organization(name="Theater Beispiel", address_locality="Wien")
        session.add_all([person, first, second, theatre])
        session.commit()
        production = Production(
            name="Hamlet", text_id=first.id, organization_id=theatre.id, premiere=dt.datetime(2020, 1, 1)
        )
        session.add(production)
        session.commit()
        credit = ProductionMembership(production_id=production.id, person_id=person.id, role_name="director")
        session.add(credit)
        session.commit()
    yield engine, person, first, second, theatre, production, credit
    engine.dispose()


def match_description(graph, text):
    engine, person, *_ = graph
    with Session(engine) as session:
        return mentions.find_mentions(
            session, (mentions.EntityType.PERSON, person.id), text, mentions.catalog(session)
        )[1]


def test_same_title_uses_person_production_graph_and_staging_year(graph):
    matches = match_description(graph, "Sie inszenierte Hamlet (2020).")
    assert len(matches) == 1
    assert matches[0].confidence == "high"
    assert matches[0].target is not None
    assert matches[0].target.id == graph[2].id
    assert matches[0].evidence[str(graph[2].id)][0].role == "director"


def test_medium_and_multiple_connected_versions_stay_unlinked(graph):
    engine, person, _first, second, *_ = graph
    with Session(engine) as session:
        session.add(TextContribution(person_id=person.id, text_id=second.id, role="author"))
        session.add(Text(title="Ein unbekanntes Stück"))
        session.commit()
    matches = match_description(graph, "Das Stück Hamlet und Ein unbekanntes Stück.")
    assert [(match.confidence, match.target) for match in matches] == [("ambiguous", None), ("medium", None)]


@pytest.mark.parametrize("description", ["Sie inszenierte Hamlet (1990).", "Sie übersetzte Hamlet (2020)."])
def test_conflicting_year_or_role_does_not_force_graph_match(graph, description):
    assert match_description(graph, description)[0].target is None


@pytest.mark.parametrize("description", ["Sie schrieb Hamlet (2020).", "Sie inszenierte Hamlet (2010)."])
def test_writing_year_and_staging_year_are_not_interchangeable(graph, description):
    engine, person, first, *_ = graph
    with Session(engine) as session:
        text = session.get(Text, first.id)
        assert text is not None
        text.created = dt.datetime(2010, 1, 1)
        session.add(text)
        session.add(TextContribution(person_id=person.id, text_id=first.id, role="author"))
        session.commit()
    assert match_description(graph, description)[0].target is None


def test_authorship_and_known_alias_are_graph_evidence(graph):
    engine, person, first, *_ = graph
    with Session(engine) as session:
        session.add(TextContribution(person_id=person.id, text_id=first.id, role="author"))
        session.add(TextTitle(text_id=first.id, title="Prinz von Dänemark"))
        session.commit()
    match = match_description(graph, "Sie schrieb **Prinz von Dänemark**.")[0]
    assert match.target is not None
    assert match.target.id == first.id
    assert any(path.path == "TextContribution" for path in match.evidence[str(first.id)])


def test_theatre_membership_and_staging_are_distinct_evidence(graph):
    engine, person, _, _, theatre, *_ = graph
    match = match_description(graph, "Sie arbeitete am Theater Beispiel.")[0]
    assert match.target is not None
    assert match.target.id == theatre.id
    assert all(path.path != "OrganizationMembership" for path in match.evidence[str(theatre.id)])
    with Session(engine) as session:
        session.add(OrganizationMembership(person_id=person.id, organization_id=theatre.id, role="ensemble member"))
        session.commit()
    match = match_description(graph, "Sie war Ensemblemitglied am Theater Beispiel.")[0]
    assert any(path.path == "OrganizationMembership" for path in match.evidence[str(theatre.id)])


def test_membership_interval_does_not_support_a_conflicting_year(graph):
    engine, person, *_ = graph
    with Session(engine) as session:
        theatre = Organization(name="Andere Bühne")
        session.add(theatre)
        session.flush()
        session.add(
            OrganizationMembership(
                person_id=person.id,
                organization_id=theatre.id,
                role="ensemble member",
                start_date=dt.datetime(2000, 1, 1),
                end_date=dt.datetime(2010, 1, 1),
            )
        )
        session.commit()
    assert match_description(graph, "Ensemblemitglied am Andere Bühne (2005).")[0].target is not None
    assert match_description(graph, "Ensemblemitglied am Andere Bühne (2020).")[0].target is None


def test_common_words_self_links_and_weak_cocast_do_not_auto_link(graph):
    engine, person, _, _, _, production, _ = graph
    with Session(engine) as session:
        other = Person(name="Andere Person")
        common = Text(title="Liebe")
        session.add_all([other, common])
        session.flush()
        session.add_all(
            [
                TextContribution(person_id=person.id, text_id=common.id),
                ProductionMembership(person_id=other.id, production_id=production.id),
            ]
        )
        session.commit()
    matches = match_description(graph, "Ada Beispiel spricht über Liebe und Andere Person.")
    assert [match.quote for match in matches] == ["Liebe", "Andere Person"]
    assert all(match.confidence == "medium" and match.target is None for match in matches)


def test_markdown_skips_links_code_html_and_preserves_unicode_offsets(graph):
    engine, _person, first, *_ = graph
    with Session(engine) as session:
        session.add(TextTitle(text_id=first.id, title="Straße Bühne"))
        session.commit()
    text = "[Hamlet](https://example.org) `Hamlet` <b>Hamlet</b>\n\n**STRASSE Bu\u0308hne** (2020)"
    matches = match_description(graph, text)
    assert len(matches) == 1
    assert matches[0].quote == "STRASSE Bu\u0308hne"
    assert matches[0].target is not None
    assert matches[0].target.id == first.id


def test_longest_overlapping_alias_wins_even_when_it_starts_later(graph):
    engine, _, first, *_ = graph
    with Session(engine) as session:
        session.add_all(
            [
                TextTitle(text_id=first.id, title="Langer Titel"),
                TextTitle(text_id=first.id, title="Titel mit vielen Worten"),
            ]
        )
        session.commit()
    matches = match_description(graph, "Langer Titel mit vielen Worten")
    assert [match.quote for match in matches] == ["Titel mit vielen Worten"]


def set_description(graph, description):
    engine, person, *_ = graph
    with Session(engine) as session:
        row = session.get(Person, person.id)
        assert row is not None
        row.description = description
        session.add(row)
        session.commit()


def queue_medium(graph):
    set_description(graph, "Sie las ein Stück: Hamlet (1990).")
    result = mentions.scan_descriptions("person", offset=0)
    assert result == {"scanned": 1, "queued": 1, "next_offset": 1}
    return mentions.mention_cases()[0]


def test_review_is_idempotent_and_approval_changes_no_domain_data(graph):
    engine, person, first, *_ = graph
    case = queue_medium(graph)
    assert mentions.scan_descriptions("person", offset=0)["queued"] == 0
    with Session(engine) as session:
        owner = session.get(Person, person.id)
        assert owner is not None
        before = owner.model_dump()
    preview = mentions.preview_mention(case["id"], target_kind="text", target_id=str(first.id))
    mentions.decide_mention(
        case["id"],
        revision=preview["revision"],
        actor_id="editor",
        note="Quelle geprüft",
        target_kind="text",
        target_id=str(first.id),
    )
    with Session(engine) as session:
        owner = session.get(Person, person.id)
        assert owner is not None
        assert owner.model_dump() == before
        assert len(session.exec(select(Text)).all()) == 2
        decided = session.get(ReconciliationCase, UUID(case["id"]))
        assert decided is not None
        assert decided.status == "approved" and decided.actor_id == "editor"
        labels = mentions.catalog(session)
    html, previews = mentions.render_description("person", str(person.id), before["description"], labels)
    assert 'data-mention="0"' in html and previews[0]["id"] == str(first.id)


def test_approved_mentions_survive_renamed_merged_targets_and_skip_deletion(graph):
    engine, person, first, second, _, production, _ = graph
    case = queue_medium(graph)
    preview = mentions.preview_mention(case["id"], target_kind="text", target_id=str(first.id))
    mentions.decide_mention(
        case["id"],
        revision=preview["revision"],
        actor_id="editor",
        note="Quelle geprüft",
        target_kind="text",
        target_id=str(first.id),
    )
    with Session(engine) as session:
        survivor = session.get(Text, second.id)
        assert survivor is not None
        survivor.title = "Ein umbenanntes Werk"
        for title in session.exec(select(TextTitle).where(TextTitle.title == "Hamlet")):
            session.delete(title)
        staging = session.get(Production, production.id)
        assert staging is not None
        staging.text_id = second.id
        session.add_all(
            [
                survivor,
                staging,
                EntityRedirect(
                    entity_type=EntityType.TEXT,
                    old_id=first.id,
                    target_id=second.id,
                ),
            ]
        )
        session.delete(session.get(Text, first.id))
        session.commit()
    description = "Sie las ein Stück: Hamlet (1990)."
    html, previews = mentions.render_description("person", str(person.id), description, mentions.load_catalog())
    assert 'data-mention="0"' in html
    assert previews[0]["id"] == str(second.id) and previews[0]["name"] == "Ein umbenanntes Werk"
    with Session(engine) as session:
        staging = session.get(Production, production.id)
        assert staging is not None
        staging.text_id = None
        session.add(staging)
        for title in session.exec(select(TextTitle).where(TextTitle.text_id == second.id)):
            session.delete(title)
        session.delete(session.get(Text, second.id))
        session.commit()
    html, previews = mentions.render_description("person", str(person.id), description, mentions.load_catalog())
    assert not previews and "data-mention" not in html


def test_rejected_case_is_not_requeued_and_suppresses_future_graph_link(graph):
    case = queue_medium(graph)
    preview = mentions.preview_mention(case["id"])
    mentions.decide_mention(case["id"], revision=preview["revision"], actor_id="editor", note="Falsches Werk")
    assert mentions.scan_descriptions("person", offset=0)["queued"] == 0
    assert not mentions.mention_cases()
    engine, person, _, _, _, production, _ = graph
    with Session(engine) as session:
        staging = session.get(Production, production.id)
        assert staging is not None
        staging.premiere = dt.datetime(1990, 1, 1)
        session.add(staging)
        session.commit()
    description = "Sie las ein Stück: Hamlet (1990)."
    assert match_description(graph, description)[0].target is not None
    html, previews = mentions.render_description("person", str(person.id), description, mentions.load_catalog())
    assert not previews and "data-mention" not in html


def test_changed_description_and_graph_reject_stale_decisions(graph):
    engine, _, first, _, _, _, credit = graph
    case = queue_medium(graph)
    preview = mentions.preview_mention(case["id"], target_kind="text", target_id=str(first.id))
    with Session(engine) as session:
        row = session.get(ProductionMembership, credit.id)
        assert row is not None
        row.role_name = "actor"
        session.add(row)
        session.commit()
    with pytest.raises(ValueError, match="evidence changed"):
        mentions.decide_mention(
            case["id"],
            revision=preview["revision"],
            actor_id="editor",
            note="Prüfung",
            target_kind="text",
            target_id=str(first.id),
        )
    set_description(graph, "Ein anderer Text.")
    with pytest.raises(ValueError, match="Description changed"):
        mentions.preview_mention(case["id"])


def test_generic_review_cannot_import_or_merge_a_mention_case(graph):
    case = queue_medium(graph)
    with pytest.raises(ValueError, match="description mention inbox"):
        reconciliation.preview_case(case["id"])
    with pytest.raises(ValueError, match="description mention inbox"):
        reconciliation.approve_case(case["id"], revision="wrong", actor_id="editor", allow_create=True)


def test_public_render_is_read_only_and_html_is_safe(graph):
    engine, person, *_ = graph
    writes = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    html, previews = mentions.render_description(
        "person",
        str(person.id),
        '# Profil\n\n**Hamlet** (2020)\n\n- Eintrag\n\n<script>alert("x")</script>\n\n[unsafe](javascript:alert(1))',
        mentions.load_catalog(),
    )
    assert "<h1>Profil</h1>" in html and "<strong>" in html and "<ul>" in html
    assert "<script>" not in html and 'href="javascript:' not in html
    assert len(previews) == 1 and not writes


def test_repeated_mentions_share_preview_and_markdown_table_is_preserved(graph):
    _, person, *_ = graph
    html, previews = mentions.render_description(
        "person",
        str(person.id),
        "Hamlet (2020).\n\n| Stück | Jahr |\n| --- | --- |\n| Das Stück Hamlet | 2020 |",
        mentions.load_catalog(),
    )
    assert "<table>" in html and "<td>" in html
    assert html.count('data-mention="0"') == 2 and len(previews) == 1


def test_scan_pagination_is_bounded_and_final_page_reachable(graph):
    engine, *_ = graph
    with Session(engine) as session:
        session.add_all(Person(name=f"Scan {index}", description="Das Stück Hamlet (1990).") for index in range(52))
        session.commit()
    first = mentions.scan_descriptions("person", offset=0)
    last = mentions.scan_descriptions("person", offset=first["next_offset"])
    assert first == {"scanned": 50, "queued": 50, "next_offset": 50}
    assert last == {"scanned": 2, "queued": 2, "next_offset": 52}
    assert len(mentions.mention_cases()) == 50 and len(mentions.mention_cases(offset=50)) == 2


def test_mention_management_refuses_anonymous_access():
    app = AppTest.from_string("from frontend.management.mentions import render\nrender()").run()
    assert not app.exception
    assert app.error[0].value == "Nur Administratoren können Erwähnungen prüfen."
    assert not app.button


def test_description_view_uses_shared_links_and_repeated_component_mounts(graph, monkeypatch):
    _, person, *_ = graph
    monkeypatch.setattr(view, "_catalog", mentions.load_catalog)
    app = AppTest.from_string(
        "from frontend.views.mentions import render_description\n"
        f"render_description('Hamlet (2020)', 'person', '{person.id}', key='first')\n"
        f"render_description('Hamlet (2020)', 'person', '{person.id}', key='second')\n"
    ).run()
    assert not app.exception
    assert len(app.get("bidi_component")) == 2


def test_verified_filter_hides_unreachable_theatre_links(graph, monkeypatch):
    _, person, *_ = graph
    mount = Mock()
    monkeypatch.setattr(view, "_catalog", mentions.load_catalog)
    monkeypatch.setattr(view, "_COMPONENT", mount)
    monkeypatch.setattr(view, "verified_only", lambda: True)
    app = AppTest.from_string(
        "from frontend.views.mentions import render_description\n"
        f"render_description('Sie arbeitete am Theater Beispiel.', 'person', '{person.id}')\n"
    ).run()
    assert not app.exception and not mount.called
    assert app.markdown[0].value == "Sie arbeitete am Theater Beispiel."
