import datetime as dt
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import config, user_management
from backend.data_quality import apply_quality_action
from backend.models import Organization, OrganizationSeasonCheck, ReconciliationCase
from backend.reconciliation import queue_duplicate
from backend.scd2 import SCD2History
from frontend.auth import Principal
from frontend.management import quality
from frontend.views.organization import load_organization_profile

EDITOR = UUID(int=7)
EDITOR_SCRIPT = """
import streamlit as st
from frontend.management.quality import render_quality_editor
render_quality_editor({"id": st.session_state.organization_id})
"""


@pytest.fixture
def editor(tmp_path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'quality-ui.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    with Session(engine) as session:
        organization = Organization(
            name="Testtheater",
            url="https://theater.example",
            url_plays="https://theater.example/spielplan",
        )
        session.add(organization)
        session.commit()
        organization_id = organization.id
    app = AppTest.from_string(EDITOR_SCRIPT)
    app.session_state.organization_id = str(organization_id)
    app.session_state["_principal"] = Principal(EDITOR, "Editor", is_admin=True)
    yield app, engine, organization_id
    load_organization_profile.clear()
    engine.dispose()


def _submit(app: AppTest, label: str) -> None:
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception


def test_current_season_is_one_deliberate_confirmation_with_reused_source_and_audit(editor) -> None:
    app, engine, organization_id = editor
    app.run()
    assert not app.exception
    assert app.number_input[0].value == config.CURRENT_SEASON_START_YEAR
    assert app.text_input[0].value == "https://theater.example/spielplan"
    _submit(app, f"Spielzeit {quality.season_label(config.CURRENT_SEASON_START_YEAR)} als geprüft bestätigen")
    with Session(engine) as session:
        check = session.exec(select(OrganizationSeasonCheck)).one()
        assert check.organization_id == organization_id
        assert check.checked_by == str(EDITOR)
        assert check.evidence == "https://theater.example/spielplan"
        assert session.exec(
            select(SCD2History).where(SCD2History.entity_name == "organizationseasoncheck")
        ).one().actor_id == str(EDITOR)
    assert any("Aktuelle Spielzeit geprüft" in text.value for text in app.text)


def test_validation_error_preserves_input_and_undo_requires_confirmation(editor) -> None:
    app, engine, organization_id = editor
    app.run()
    app.segmented_control[0].set_value("general").run()
    app.text_input[0].set_value(" ")
    _submit(app, "Allgemeine Angaben als geprüft bestätigen")
    assert app.error
    assert app.text_input[0].value == " "
    with Session(engine) as session:
        organization = session.get(Organization, organization_id)
        assert organization is not None
        assert organization.general_verified_at is None
    app.text_input[0].set_value("Printed directory, p. 12")
    _submit(app, "Allgemeine Angaben als geprüft bestätigen")
    undo = next(button for button in app.button if button.label == "Prüfung zurücknehmen")
    assert undo.disabled
    app.checkbox[0].check().run()
    _submit(app, "Prüfung zurücknehmen")
    with Session(engine) as session:
        organization = session.get(Organization, organization_id)
        assert organization is not None
        assert organization.general_verified_at is None


def test_editor_without_actor_is_read_only(editor) -> None:
    app, engine, _ = editor
    del app.session_state["_principal"]
    app.run()
    assert not app.exception
    assert not app.button
    assert not app.text_input
    assert any("anmelden" in item.value for item in app.info)
    with Session(engine) as session:
        assert not session.exec(select(OrganizationSeasonCheck)).all()


def test_public_summary_shows_scope_and_season_without_reviewer_or_unsafe_links() -> None:
    app = AppTest.from_string("""
import streamlit as st
from frontend.views.organization import render_quality_summary
render_quality_summary(st.session_state.profile)
""")
    app.session_state.profile = {
        "general_verified_at": dt.datetime.now(dt.UTC).isoformat(),
        "general_verified_by": "private-reviewer",
        "general_verification_evidence": "https://theater.example",
        "history_from_season": 1990,
        "history_through_season": 2025,
        "history_verified_at": dt.datetime.now(dt.UTC).isoformat(),
        "history_verified_by": "private-reviewer",
        "history_verification_evidence": "javascript:alert(1)",
        "season_checks": [],
    }
    app.run()
    assert not app.exception
    text = "\n".join(item.value for item in app.text)
    assert "1990/91" in text and "2025/26" in text
    assert "private-reviewer" not in text
    assert "javascript:alert(1)" in text
    assert len(app.get("link_button")) == 1


def test_merge_requires_grouped_choices_and_previews_result_before_saving(editor) -> None:
    _, engine, organization_id = editor
    with Session(engine) as session:
        duplicate = Organization(name="Testtheater")
        session.add(duplicate)
        session.commit()
        duplicate_id = duplicate.id
    for identity, evidence in ((organization_id, "First archive"), (duplicate_id, "Second archive")):
        apply_quality_action(identity, "verify_general", actor_id="editor", evidence=evidence)
        apply_quality_action(
            identity,
            "check_season",
            actor_id="editor",
            evidence=evidence,
            season_start_year=config.CURRENT_SEASON_START_YEAR,
        )
    case = queue_duplicate(
        "organization",
        organization_id,
        duplicate_id,
        actor_id="editor",
        reason="Same theatre",
    )
    app = AppTest.from_string("""
import streamlit as st
from uuid import UUID
from backend.models import ReconciliationCase
from backend.reconciliation import preview_case
from backend.user_management import get_session
from frontend.management.reconciliation import _assessment_choices, _decision_buttons
with get_session() as session:
    case = session.get(ReconciliationCase, UUID(st.session_state.case_id))
    pending = case.status == "pending"
if pending:
    preview = preview_case(st.session_state.case_id)
    choices = _assessment_choices(preview)
    _decision_buttons(st.session_state.case_id, preview, preview["target_id"], [], choices)
else:
    st.success("Merged")
""")
    app.session_state.case_id = str(case.id)
    app.session_state["_principal"] = Principal(EDITOR, "Editor", is_admin=True)
    app.run()
    assert not app.exception
    app.checkbox[0].check().run()
    assert next(button for button in app.button if button.label == "Genehmigen und speichern").disabled
    app.selectbox[0].set_value(str(duplicate_id)).run()
    app.selectbox[1].set_value(str(organization_id)).run()
    assert not app.exception
    assert any("Allgemeine Angaben geprüft" in item.value for item in app.text)
    app.checkbox[0].check().run()
    _submit(app, "Genehmigen und speichern")
    with Session(engine) as session:
        survivor = session.get(Organization, organization_id)
        assert survivor is not None
        assert survivor.general_verification_evidence == "Second archive"
        assert session.get(Organization, duplicate_id) is None
        check = session.exec(select(OrganizationSeasonCheck)).one()
        assert check.evidence == "First archive"
        saved = session.get(ReconciliationCase, case.id)
        assert saved is not None and saved.status == "approved"
