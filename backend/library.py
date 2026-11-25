"""Private reading/watch diary, wishes, stars, reviews and playlists.

Every function takes the authenticated user's ID and scopes all reads/writes to it. Callers commit.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, func, select

from backend.date_precision import date_interval, format_date
from backend.models import (
    LibraryEntry,
    LibraryVisit,
    Organization,
    Playlist,
    PlaylistItem,
    Production,
    Text,
    User,
)
from backend.texts import work_family

TARGETS: dict[str, type[Text | Production]] = {"text": Text, "production": Production}
RECENT_DAYS = 180
REVIEW_LIMIT = 10_000


def find_or_create_user(session: Session, key: str, display_name: str | None, *, is_admin: bool = False) -> User:
    """Map a stable identity key (OIDC issuer+subject or the dev key) to one user; never match by email."""
    statement = select(User).where(User.username == key)
    if user := session.exec(statement).first():
        return user
    user = User(username=key, display_name=display_name, is_admin=is_admin)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:  # concurrent first login of the same identity
        session.rollback()
        return session.exec(statement).one()
    return user


def _column(kind: str):  # ruff: ignore[missing-return-type-private-function]
    if kind not in TARGETS:
        raise ValueError(f"Unknown target kind: {kind}")
    return col(LibraryEntry.text_id) if kind == "text" else col(LibraryEntry.production_id)


def _entry(
    session: Session, user_id: uuid.UUID, kind: str, target_id: uuid.UUID, *, create: bool
) -> LibraryEntry | None:
    entry = session.exec(
        select(LibraryEntry).where(LibraryEntry.user_id == user_id, _column(kind) == target_id).with_for_update()
    ).first()
    if entry is None and create:
        if session.get(TARGETS[kind], target_id) is None:
            raise ValueError("Eintrag nicht gefunden.")
        entry = (
            LibraryEntry(user_id=user_id, text_id=target_id)
            if kind == "text"
            else LibraryEntry(user_id=user_id, production_id=target_id)
        )
        session.add(entry)
        session.flush()
    return entry


def _ensure(session: Session, user_id: uuid.UUID, kind: str, target_id: uuid.UUID) -> LibraryEntry:
    entry = _entry(session, user_id, kind, target_id, create=True)
    if entry is None:
        raise ValueError("Eintrag nicht gefunden.")
    return entry


def _touch(session: Session, entry: LibraryEntry) -> None:
    entry.updated_at = dt.datetime.now(dt.UTC)
    session.add(entry)


def set_want(session: Session, user_id: uuid.UUID, kind: str, target_id: uuid.UUID, want: bool) -> None:
    """Set (not toggle) the wish, so retries are idempotent."""
    entry = _entry(session, user_id, kind, target_id, create=want)
    if entry is not None:
        entry.want_to_see = want
        _touch(session, entry)


def set_opinion(
    session: Session, user_id: uuid.UUID, kind: str, target_id: uuid.UUID, rating: int | None, review: str | None
) -> None:
    """Replace the current stars (1-5, None clears) and review (blank clears)."""
    if rating is not None and not 1 <= rating <= 5:
        raise ValueError("Bewertung muss zwischen 1 und 5 Sternen liegen.")
    review = (review or "").strip() or None
    if review and len(review) > REVIEW_LIMIT:
        raise ValueError(f"Rezension ist länger als {REVIEW_LIMIT} Zeichen.")
    entry = _entry(session, user_id, kind, target_id, create=rating is not None or review is not None)
    if entry is not None:
        entry.rating, entry.review = rating, review
        _touch(session, entry)


def add_visit(
    session: Session,
    user_id: uuid.UUID,
    kind: str,
    target_id: uuid.UUID,
    visit_kind: str,
    visited_on: dt.date | None,
    *,
    today: dt.date | None = None,
) -> None:
    """Record one more reading (texts only) or theatre visit; an unknown date stays unknown."""
    if visit_kind not in {"read", "watched"}:
        raise ValueError("Unbekannte Aktion.")
    if visit_kind == "read" and kind != "text":
        raise ValueError("Nur Texte können gelesen werden.")
    if visited_on and visited_on > (today or dt.datetime.now(dt.UTC).date()):
        raise ValueError("Datum liegt in der Zukunft. Für Zukünftiges bitte „Merken“ nutzen.")
    entry = _ensure(session, user_id, kind, target_id)
    session.add(LibraryVisit(entry_id=entry.id, kind=visit_kind, visited_on=visited_on))
    _touch(session, entry)


def delete_visit(session: Session, user_id: uuid.UUID, visit_id: uuid.UUID) -> None:
    """Remove one of the user's own visits; others and the review stay."""
    visit = session.exec(
        select(LibraryVisit)
        .join(LibraryEntry, col(LibraryEntry.id) == LibraryVisit.entry_id)
        .where(LibraryVisit.id == visit_id, LibraryEntry.user_id == user_id)
    ).first()
    if visit is None:
        raise ValueError("Besuch nicht gefunden.")
    session.delete(visit)


def status(session: Session, user_id: uuid.UUID, kind: str, target_id: uuid.UUID) -> dict[str, Any]:
    """Current personal state of one target; a text also counts visits to productions of its work family."""
    entry = _entry(session, user_id, kind, target_id, create=False)
    visits = (
        session.exec(
            select(LibraryVisit).where(LibraryVisit.entry_id == entry.id).order_by(col(LibraryVisit.created_at))
        ).all()
        if entry
        else []
    )
    production_visits = 0
    if kind == "text":
        production_visits = session.exec(
            select(func.count(col(LibraryVisit.id)))
            .join(LibraryEntry, col(LibraryEntry.id) == LibraryVisit.entry_id)
            .join(Production, col(Production.id) == LibraryEntry.production_id)
            .where(
                LibraryEntry.user_id == user_id,
                col(Production.text_id).in_([text.id for text in work_family(session, target_id)]),
            )
        ).one()
    lists = (
        set(session.exec(select(PlaylistItem.playlist_id).where(PlaylistItem.entry_id == entry.id)).all())
        if entry
        else set()
    )
    return {
        "want": bool(entry and entry.want_to_see),
        "rating": entry.rating if entry else None,
        "review": entry.review if entry else None,
        "reads": [visit for visit in visits if visit.kind == "read"],
        "watches": [visit for visit in visits if visit.kind == "watched"],
        "production_watches": production_visits,
        "playlists": lists,
    }


def playlists(session: Session, user_id: uuid.UUID) -> list[Playlist]:
    """The user's lists by name."""
    return list(session.exec(select(Playlist).where(Playlist.user_id == user_id).order_by(col(Playlist.name))).all())


def create_playlist(session: Session, user_id: uuid.UUID, name: str) -> Playlist:
    """Create a list, or return the existing one with that name."""
    name = " ".join(name.split())
    if not 1 <= len(name) <= 100:
        raise ValueError("Listenname braucht 1 bis 100 Zeichen.")
    existing = session.exec(select(Playlist).where(Playlist.user_id == user_id, Playlist.name == name)).first()
    if existing:
        return existing
    playlist = Playlist(user_id=user_id, name=name)
    session.add(playlist)
    session.flush()
    return playlist


def _own_playlist(session: Session, user_id: uuid.UUID, playlist_id: uuid.UUID) -> Playlist:
    playlist = session.get(Playlist, playlist_id)
    if playlist is None or playlist.user_id != user_id:
        raise ValueError("Liste nicht gefunden.")
    return playlist


def add_to_playlist(
    session: Session, user_id: uuid.UUID, playlist_id: uuid.UUID, kind: str, target_id: uuid.UUID
) -> None:
    """Append a text or production; adding again is a no-op."""
    _own_playlist(session, user_id, playlist_id)
    entry = _ensure(session, user_id, kind, target_id)
    exists = session.exec(
        select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.entry_id == entry.id)
    ).first()
    if exists is None:
        position = session.exec(
            select(func.coalesce(func.max(PlaylistItem.position) + 1, 0)).where(PlaylistItem.playlist_id == playlist_id)
        ).one()
        session.add(PlaylistItem(playlist_id=playlist_id, entry_id=entry.id, position=position))


def remove_from_playlist(session: Session, user_id: uuid.UUID, playlist_id: uuid.UUID, entry_id: uuid.UUID) -> None:
    """Remove one item; the entry's wish, visits and review stay."""
    _own_playlist(session, user_id, playlist_id)
    session.exec(
        delete(PlaylistItem).where(col(PlaylistItem.playlist_id) == playlist_id, col(PlaylistItem.entry_id) == entry_id)
    )


def delete_playlist(session: Session, user_id: uuid.UUID, playlist_id: uuid.UUID) -> None:
    """Delete a list and its items only."""
    playlist = _own_playlist(session, user_id, playlist_id)
    session.exec(delete(PlaylistItem).where(col(PlaylistItem.playlist_id) == playlist_id))
    session.delete(playlist)


def collection(session: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """All of the user's entries with titles, visits and list positions, most recently changed first."""
    rows = session.exec(
        select(LibraryEntry, Text.title, Production.name, Organization.name)
        .outerjoin(Text, col(Text.id) == LibraryEntry.text_id)
        .outerjoin(Production, col(Production.id) == LibraryEntry.production_id)
        .outerjoin(Organization, col(Organization.id) == Production.organization_id)
        .where(LibraryEntry.user_id == user_id)
        .order_by(col(LibraryEntry.updated_at).desc())
    ).all()
    ids = [entry.id for entry, *_ in rows]
    visits: dict[uuid.UUID, list[LibraryVisit]] = {}
    for visit in session.exec(select(LibraryVisit).where(col(LibraryVisit.entry_id).in_(ids))).all():
        visits.setdefault(visit.entry_id, []).append(visit)
    lists: dict[uuid.UUID, dict[uuid.UUID, int]] = {}
    for item in session.exec(select(PlaylistItem).where(col(PlaylistItem.entry_id).in_(ids))).all():
        lists.setdefault(item.entry_id, {})[item.playlist_id] = item.position
    return [
        {
            "entry_id": entry.id,
            "kind": "text" if entry.text_id else "production",
            "target_id": entry.text_id or entry.production_id,
            "title": text_title or production_name,
            "theatre": theatre,
            "want": entry.want_to_see,
            "rating": entry.rating,
            "review": entry.review,
            "visits": sorted(
                visits.get(entry.id, []), key=lambda v: (v.visited_on is None, v.visited_on), reverse=True
            ),
            "playlists": lists.get(entry.id, {}),
        }
        for entry, text_title, production_name, theatre in rows
    ]


def stagings(session: Session, text_id: uuid.UUID, *, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Productions of this text and its translations/adaptations, upcoming premieres first, then recent ones.

    shortcut: premieres only, not performance dates; add a Performance table when a programme source exists.
    """
    today = today or dt.datetime.now(dt.UTC).date()
    family = [text.id for text in work_family(session, text_id)]
    rows = session.exec(
        select(Production, Organization.name, Organization.city)
        .outerjoin(Organization, col(Organization.id) == Production.organization_id)
        .where(col(Production.text_id).in_(family))
    ).all()
    result = []
    for production, theatre, city in rows:
        status, label, start = "unknown", "Datum unbekannt", None
        if production.premiere and production.premiere_precision:
            start, end = date_interval(production.premiere, production.premiere_precision)
            label = format_date(production.premiere, production.premiere_precision)
            if start.date() > today:
                status = "upcoming"
            elif end is None or end.date() > today - dt.timedelta(days=RECENT_DAYS):
                status = "recent"
            else:
                status = "past"
        result.append(
            {
                "id": production.id,
                "name": production.name,
                "theatre": theatre,
                "city": city,
                "premiere": label,
                "status": status,
                "start": start,
            }
        )
    order = {"upcoming": 0, "recent": 1, "past": 2, "unknown": 3}
    return sorted(
        result,
        key=lambda row: (
            order[row["status"]],
            # upcoming soonest first, everything else newest first
            (row["start"] or dt.datetime.min).toordinal() * (1 if row["status"] == "upcoming" else -1),
            str(row["id"]),
        ),
    )


def merge_target(session: Session, kind: str, duplicate_id: uuid.UUID, target_id: uuid.UUID) -> None:
    """Move private entries during a catalogue merge without losing visits, wishes, lists or opinions."""
    if kind not in TARGETS:
        return
    column = _column(kind)
    for entry in session.exec(select(LibraryEntry).where(column == duplicate_id)).all():
        survivor = session.exec(
            select(LibraryEntry).where(LibraryEntry.user_id == entry.user_id, column == target_id)
        ).first()
        if survivor is None:
            setattr(entry, f"{kind}_id", target_id)
            session.add(entry)
            continue
        survivor.want_to_see = survivor.want_to_see or entry.want_to_see
        survivor.rating = survivor.rating or entry.rating
        if entry.review and entry.review != survivor.review:
            survivor.review = "\n\n".join(filter(None, (survivor.review, entry.review)))
        session.add(survivor)
        for visit in session.exec(select(LibraryVisit).where(LibraryVisit.entry_id == entry.id)).all():
            visit.entry_id = survivor.id
            session.add(visit)
        kept = set(session.exec(select(PlaylistItem.playlist_id).where(PlaylistItem.entry_id == survivor.id)).all())
        for item in session.exec(select(PlaylistItem).where(PlaylistItem.entry_id == entry.id)).all():
            if item.playlist_id in kept:
                session.delete(item)
            else:
                item.entry_id = survivor.id
                session.add(item)
        session.flush()
        session.delete(entry)
    session.flush()
