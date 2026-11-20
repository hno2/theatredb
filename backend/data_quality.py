"""Organization-level editorial assessments and shared public status derivation."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Literal, TypedDict, cast
from uuid import UUID

from sqlmodel import Session, col, select

from backend import config
from backend.models import Organization, OrganizationSeasonCheck
from backend.scd2 import add_scd2_event
from backend.user_management import get_session

QUALITY_FIELDS = frozenset(
    {
        "general_verified_at",
        "general_verified_by",
        "general_verification_evidence",
        "history_from_season",
        "history_through_season",
        "history_verified_at",
        "history_verified_by",
        "history_verification_evidence",
        "update_scope",
        "update_reason",
        "update_flagged_at",
        "update_flagged_by",
    }
)

_GENERAL_FIELDS = (
    "general_verified_at",
    "general_verified_by",
    "general_verification_evidence",
)
_HISTORY_FIELDS = (
    "history_from_season",
    "history_through_season",
    "history_verified_at",
    "history_verified_by",
    "history_verification_evidence",
)
_UPDATE_FIELDS = ("update_scope", "update_reason", "update_flagged_at", "update_flagged_by")
_ACTIONS = {
    "verify_general",
    "withdraw_general",
    "verify_history",
    "withdraw_history",
    "check_season",
    "remove_season",
    "flag_update",
    "resolve_update",
}
_COLORS = {
    "update": [217, 119, 6, 200],
    "history": [0, 122, 94, 200],
    "general": [37, 99, 235, 200],
    "unverified": [107, 114, 128, 200],
}
_QUALITY_LABELS = {
    "update": "Aktualisierung nötig",
    "history": "Allgemeine Angaben und Historie geprüft",
    "general": "Allgemeine Angaben geprüft",
    "unverified": "Allgemeine Angaben noch ungeprüft",
}
QUALITY_LABELS: dict[str, str] = _QUALITY_LABELS
type QualityRecord = Organization | OrganizationSeasonCheck | Mapping[str, object]


class PublicQualitySummary(TypedDict):
    """Publicly displayable assessment state without reviewer identities."""

    quality: Literal["unverified", "general", "history", "update"]
    quality_label: str
    color: list[int]
    general_label: str
    history_label: str
    season_label: str
    season_current: bool
    current_season: int
    current_season_label: str
    season_checked_at: str | None
    season_checked_season_start_year: int | None
    update_reason: str
    evidence: list[str]


def season_label(year: int) -> str:
    """Format a season start year, including century rollover."""
    if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9998:
        raise ValueError("Season must be a four-digit start year")
    return f"{year}/{(year + 1) % 100:02d}"


def _value(record: QualityRecord, field: str, default: object = None) -> object:
    if isinstance(record, Mapping):
        return record.get(field, default)
    return getattr(record, field, default)


def _timestamp(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)
    return None


def _display_date(value: object) -> str | None:
    timestamp = _timestamp(value)
    return timestamp.strftime("%d.%m.%Y") if timestamp else None


def _is_present(record: QualityRecord, fields: Iterable[str]) -> bool:
    return all(_value(record, field) is not None for field in fields)


def public_summary(
    organization_or_mapping: Organization | Mapping[str, object],
    checks: Iterable[OrganizationSeasonCheck | Mapping[str, object]],
    current_season: int | None = None,
) -> PublicQualitySummary:
    """Derive public labels and marker color without exposing reviewer identities."""
    target_season = config.CURRENT_SEASON_START_YEAR if current_season is None else current_season
    season_label(target_season)
    checks = list(checks)
    checked_years = [
        year
        for check in checks
        if isinstance((year := _value(check, "season_start_year")), int)
        and not isinstance(year, bool)
        and 1000 <= year <= target_season
    ]
    current_check = next((check for check in checks if _value(check, "season_start_year") == target_season), None)
    latest_check = max(
        (check for check in checks if _value(check, "season_start_year") in checked_years),
        key=lambda check: cast(int, _value(check, "season_start_year")),
        default=None,
    )
    has_general = _is_present(organization_or_mapping, ("general_verified_at", "general_verification_evidence"))
    has_history = _is_present(
        organization_or_mapping,
        ("history_from_season", "history_through_season", "history_verified_at", "history_verification_evidence"),
    )
    update_reason = _value(organization_or_mapping, "update_reason")
    update_active = _value(organization_or_mapping, "update_scope") is not None
    quality = (
        "update"
        if update_active
        else "history"
        if has_general and has_history
        else "general"
        if has_general
        else "unverified"
    )

    general_date = _display_date(_value(organization_or_mapping, "general_verified_at"))
    general_label = (
        f"Allgemeine Angaben geprüft am {general_date}"
        if has_general and general_date
        else "Allgemeine Angaben geprüft"
        if has_general
        else "Allgemeine Angaben noch ungeprüft"
    )
    if has_history:
        history_label = (
            "Produktionshistorie vollständig: "
            f"{season_label(cast(int, _value(organization_or_mapping, 'history_from_season')))}\u2013"
            f"{season_label(cast(int, _value(organization_or_mapping, 'history_through_season')))}"
        )
    else:
        history_label = "Produktionshistorie noch nicht bestätigt"

    if current_check is not None:
        season_status = "Aktuelle Spielzeit geprüft"
        season_date = _display_date(_value(current_check, "checked_at"))
        season_date = f" am {season_date}" if season_date else ""
    elif checked_years:
        season_status = "Nur frühere Spielzeiten geprüft"
        season_date = ""
    else:
        season_status = "Spielzeit noch nicht geprüft"
        season_date = ""
    latest_year = cast(int, _value(latest_check, "season_start_year")) if latest_check is not None else None
    latest_date = _display_date(_value(latest_check, "checked_at")) if latest_check is not None else None
    latest_suffix = (
        f"; zuletzt {season_label(latest_year)} am {latest_date}"
        if latest_year is not None and latest_year != target_season and latest_date
        else f"; zuletzt {season_label(latest_year)}"
        if latest_year is not None and latest_year != target_season
        else ""
    )
    freshness_label = f"{season_status} ({season_label(target_season)}{season_date}{latest_suffix})"
    evidence = [
        str(value).strip()
        for value in (
            _value(organization_or_mapping, "general_verification_evidence"),
            _value(organization_or_mapping, "history_verification_evidence"),
            *(_value(check, "evidence") for check in checks),
        )
        if isinstance(value, str) and value.strip()
    ]
    return {
        "quality": quality,
        "quality_label": _QUALITY_LABELS[quality],
        "color": _COLORS[quality].copy(),
        "general_label": general_label,
        "history_label": history_label,
        "season_label": freshness_label,
        "season_current": current_check is not None,
        "current_season": target_season,
        "current_season_label": season_label(target_season),
        "season_checked_at": _display_date(_value(current_check, "checked_at"))
        if current_check is not None
        else latest_date,
        "season_checked_season_start_year": target_season if current_check is not None else latest_year,
        "update_reason": cast(str, update_reason) if update_active and isinstance(update_reason, str) else "",
        "evidence": evidence,
    }


def season_checks_many(organization_ids: Iterable[UUID]) -> dict[UUID, list[OrganizationSeasonCheck]]:
    """Load checks for a set of organizations in one query."""
    ids = {UUID(str(identity)) for identity in organization_ids}
    if not ids:
        return {}
    with get_session() as session:
        rows = session.exec(
            select(OrganizationSeasonCheck)
            .where(col(OrganizationSeasonCheck.organization_id).in_(ids))
            .order_by(col(OrganizationSeasonCheck.organization_id), col(OrganizationSeasonCheck.season_start_year))
        ).all()
    result: dict[UUID, list[OrganizationSeasonCheck]] = {identity: [] for identity in ids}
    for row in rows:
        result[row.organization_id].append(row)
    return result


def _required_text(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value.strip()


def _season_start(value: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 9998:
        raise ValueError("Season must be a four-digit start year")
    if value > config.CURRENT_SEASON_START_YEAR:
        raise ValueError("Future seasons cannot be checked")
    return value


def _audit(session: Session, entity: Organization | OrganizationSeasonCheck, operation: str, actor_id: str) -> None:
    add_scd2_event(
        session,
        entity_name=type(entity).__name__.lower(),
        entity_id=str(entity.id),
        operation=operation,
        snapshot=entity.model_dump(mode="json"),
        actor_id=actor_id,
    )


def _validate_action(
    action: str,
    actor_id: str | None,
    evidence: str,
    season_start_year: int | None,
    history_from_season: int | None,
    history_through_season: int | None,
    update_scope: str | None,
    update_reason: str | None,
) -> tuple[str, str, int | None, int | None, int | None, str | None, str | None]:
    if action not in _ACTIONS:
        raise ValueError(f"Unknown quality action: {action}")
    actor = _required_text(actor_id, "Actor")
    if action in {"verify_general", "verify_history", "check_season"}:
        evidence = _required_text(evidence, "Evidence")
    if action == "verify_history":
        history_from_season = _season_start(history_from_season)
        history_through_season = _season_start(history_through_season)
        if history_from_season > history_through_season:
            raise ValueError("History start season must not follow its end season")
    if action in {"check_season", "remove_season"}:
        season_start_year = _season_start(season_start_year)
    if action == "flag_update":
        if update_scope not in {"general", "productions", "both"}:
            raise ValueError("Choose general, productions, or both for the update scope")
        update_reason = _required_text(update_reason, "Update reason")
    return actor, evidence, season_start_year, history_from_season, history_through_season, update_scope, update_reason


def _save_organization_action(
    session: Session,
    organization: Organization,
    fields: tuple[str, ...],
    values: tuple[object, ...],
    actor: str,
) -> Organization:
    for field, value in zip(fields, values, strict=True):
        setattr(organization, field, value)
    _audit(session, organization, "update", actor)
    session.commit()
    session.refresh(organization)
    return organization


def _save_season_action(
    session: Session,
    organization: Organization,
    action: str,
    actor: str,
    evidence: str,
    season_start_year: int,
    now: dt.datetime,
) -> OrganizationSeasonCheck | None:
    check = session.exec(
        select(OrganizationSeasonCheck).where(
            OrganizationSeasonCheck.organization_id == organization.id,
            OrganizationSeasonCheck.season_start_year == season_start_year,
        )
    ).one_or_none()
    if action == "remove_season":
        if check is None:
            raise ValueError("Season check not found")
        _audit(session, check, "delete", actor)
        session.delete(check)
        session.commit()
        return None
    if check is None:
        check = OrganizationSeasonCheck(
            organization_id=organization.id,
            season_start_year=season_start_year,
            checked_at=now,
            checked_by=actor,
            evidence=evidence,
        )
        session.add(check)
        operation = "insert"
    else:
        check.checked_at = now
        check.checked_by = actor
        check.evidence = evidence
        session.add(check)
        operation = "update"
    _audit(session, check, operation, actor)
    session.commit()
    session.refresh(check)
    return check


def apply_quality_action(
    org_id: UUID | str,
    action: str,
    actor_id: str | None = None,
    evidence: str = "",
    season_start_year: int | None = None,
    history_from_season: int | None = None,
    history_through_season: int | None = None,
    update_scope: str | None = None,
    update_reason: str | None = None,
) -> Organization | OrganizationSeasonCheck | None:
    """Apply one validated editorial action atomically with its SCD2 snapshot."""
    actor, evidence, season_start_year, history_from_season, history_through_season, update_scope, update_reason = (
        _validate_action(
            action,
            actor_id,
            evidence,
            season_start_year,
            history_from_season,
            history_through_season,
            update_scope,
            update_reason,
        )
    )
    identity, now = UUID(str(org_id)), dt.datetime.now(dt.UTC)

    with get_session() as session:
        organization = session.get(Organization, identity, with_for_update=True)
        if organization is None:
            raise ValueError("Organization not found")

        if action in {"verify_general", "withdraw_general"}:
            values = (now, actor, evidence.strip()) if action == "verify_general" else (None, None, None)
            return _save_organization_action(session, organization, _GENERAL_FIELDS, values, actor)

        if action in {"verify_history", "withdraw_history"}:
            values = (
                (
                    history_from_season,
                    history_through_season,
                    now,
                    actor,
                    evidence.strip(),
                )
                if action == "verify_history"
                else (None, None, None, None, None)
            )
            return _save_organization_action(session, organization, _HISTORY_FIELDS, values, actor)

        if action in {"check_season", "remove_season"}:
            return _save_season_action(
                session,
                organization,
                action,
                actor,
                evidence.strip(),
                cast(int, season_start_year),
                now,
            )

        values = (update_scope, update_reason, now, actor) if action == "flag_update" else (None, None, None, None)
        return _save_organization_action(session, organization, _UPDATE_FIELDS, values, actor)
