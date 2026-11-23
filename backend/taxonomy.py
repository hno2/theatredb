"""Two-layer controlled production tags: vocabulary, source mapping, editorial decisions and filters.

Profil answers "what is it" (form, premiere status, ...), Inhalt "what does it concern" (theme, period, ...).
Legacy free-text Production.tags stay untouched; reviewed aliases map them to source-backed assignments.
"""

import argparse
import datetime as dt
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable
from itertools import batched
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session as SASession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from backend.models import Production, ProductionTag, Tag, TagLayer, TagOrigin, TagStatus
from backend.scd2 import add_scd2_event

# facet -> (layer, German label); order defines display order.
FACETS: dict[str, tuple[TagLayer, str]] = {
    "form": (TagLayer.PROFILE, "Sparte"),
    "premiere": (TagLayer.PROFILE, "Premierenstatus"),
    "practice": (TagLayer.PROFILE, "Arbeitsweise"),
    "audience": (TagLayer.PROFILE, "Publikum"),
    "context": (TagLayer.PROFILE, "Produktionskontext"),
    "theme": (TagLayer.CONTENT, "Thema"),
    "period": (TagLayer.CONTENT, "Handlungszeit"),
    "setting": (TagLayer.CONTENT, "Schauplatz"),
    "reference": (TagLayer.CONTENT, "Vorlage & Bezug"),
}
LAYER_LABELS = {TagLayer.PROFILE: "Profil", TagLayer.CONTENT: "Inhalt"}
ORIGIN_LABELS = {TagOrigin.MANUAL: "redaktionell", TagOrigin.SOURCE: "laut Quelle", TagOrigin.MODEL: "automatisch"}

# (code, label, definition, legacy source aliases). Codes are permanent; deprecate instead of renaming.
VOCABULARY: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("form.schauspiel", "Schauspiel", "Sprechtheater mit Darstellerinnen und Darstellern.", ("Schauspiel",)),
    ("form.oper", "Oper & Operette", "Musiktheater mit Gesang und Orchester.", ("Oper/Operette", "Oper", "Operette")),
    ("form.musical", "Musical", "Populäres Musiktheater mit Song, Tanz und Dialog.", ("Musical",)),
    ("form.tanz", "Tanz", "Bewegung und Choreografie als primäre Ausdrucksform.", ("Tanz", "Ballett", "Tanztheater")),
    ("form.performance", "Performance", "Performative Formen jenseits einer dramatischen Fabel.", ("Performance",)),
    ("form.revue", "Revue & Varieté", "Nummernprogramm aus Musik, Tanz und Unterhaltung.", ("Revue",)),
    (
        "form.figuren",
        "Figuren- & Objekttheater",
        "Spiel mit Puppen, Figuren oder Objekten.",
        ("Figuren-/Puppen-/Objekttheater", "Puppentheater", "Figurentheater", "Objekttheater"),
    ),
    ("form.konzert", "Konzert & Oratorium", "Konzertante Musik ohne szenische Handlung.", ("Konzert/Oratorium",)),
    ("form.lesung", "Lesung", "Vortrag eines Textes ohne vollständige Inszenierung.", ("Lesung",)),
    (
        "premiere.ua",
        "Uraufführung",
        "Weltweit erste Aufführung dieses Werks in dieser Fassung.",
        ("UA", "Uraufführung"),
    ),
    (
        "premiere.dea",
        "Deutschsprachige Erstaufführung",
        "Erste Aufführung im deutschen Sprachraum.",
        ("DEA", "DSE", "Deutschsprachige Erstaufführung"),
    ),
    ("premiere.de", "Deutsche Erstaufführung", "Erste Aufführung in Deutschland.", ("Deutsche Erstaufführung",)),
    ("premiere.oea", "Österreichische Erstaufführung", "Erste Aufführung in Österreich.", ("ÖEA",)),
    ("premiere.sea", "Schweizerische Erstaufführung", "Erste Aufführung in der Schweiz.", ("SEA",)),
    ("premiere.wa", "Wiederaufnahme", "Erneute Aufnahme einer früheren Inszenierung.", ("Wiederaufnahme",)),
    (
        "practice.dokumentarisch",
        "Dokumentarisch",
        "Arbeitet mit realen Dokumenten, Interviews oder Biografien.",
        ("Dokumentartheater",),
    ),
    ("practice.partizipativ", "Partizipativ", "Publikum oder Laien gestalten aktiv mit.", ()),
    ("practice.immersiv", "Immersiv", "Publikum bewegt sich in einer begehbaren Spielwelt.", ()),
    ("practice.ortsspezifisch", "Ortsspezifisch", "Für einen bestimmten Ort außerhalb der Bühne entwickelt.", ()),
    (
        "audience.jung",
        "Kinder & Jugendliche",
        "Richtet sich an ein junges Publikum.",
        ("Kinder- und Jugendtheater", "Kindertheater", "Jugendtheater"),
    ),
    (
        "context.frei",
        "Freie Szene",
        "Produziert außerhalb fester Stadt- und Staatstheater.",
        ("Freie Darstellende Künste",),
    ),
    ("theme.familie", "Familie & Generationen", "Eltern, Kinder, Erbe und Konflikte zwischen Generationen.", ()),
    ("theme.liebe", "Liebe & Beziehungen", "Liebe, Begehren, Ehe, Trennung und Freundschaft.", ()),
    ("theme.macht", "Macht & Herrschaft", "Politische Macht, Herrschaft, Intrige und Widerstand.", ()),
    ("theme.krieg", "Krieg & Gewalt", "Krieg, Terror, Gewalt und deren Folgen.", ()),
    ("theme.migration", "Migration & Exil", "Flucht, Auswanderung, Exil und Ankommen.", ()),
    ("theme.identitaet", "Identität & Zugehörigkeit", "Wer man ist, wohin man gehört, Herkunft und Selbstbild.", ()),
    ("theme.geschlecht", "Geschlecht & Rollenbilder", "Geschlechterrollen, Feminismus und queere Lebensweisen.", ()),
    ("theme.arbeit", "Arbeit & Klasse", "Arbeit, Armut, Reichtum und soziale Ungleichheit.", ()),
    ("theme.umwelt", "Umwelt & Klima", "Natur, Klimakrise und ökologische Zerstörung.", ()),
    ("theme.erinnerung", "Erinnerung & Verlust", "Tod, Trauer, Gedächtnis und Vergangenheitsbewältigung.", ()),
    ("period.antike", "Antike", "Handlung im Altertum.", ()),
    ("period.mittelalter", "Mittelalter", "Handlung etwa zwischen 500 und 1500.", ()),
    ("period.fruehe_neuzeit", "Frühe Neuzeit", "Handlung etwa zwischen 1500 und 1800.", ()),
    ("period.19jh", "19. Jahrhundert", "Handlung im 19. Jahrhundert.", ()),
    ("period.1900_1945", "1900-1945", "Handlung zwischen Jahrhundertwende und Kriegsende.", ()),
    ("period.1945_1989", "1945-1989", "Handlung zwischen Kriegsende und Mauerfall.", ()),
    ("period.gegenwart", "Gegenwart", "Handlung in der heutigen Zeit.", ()),
    ("period.zukunft", "Zukunft", "Handlung in einer erdachten Zukunft.", ()),
    ("setting.grossstadt", "Großstadt", "Spielt in einer Metropole oder Großstadt.", ()),
    ("setting.land", "Ländlicher Raum", "Spielt auf dem Land, im Dorf oder in der Provinz.", ()),
    ("setting.haushalt", "Privater Haushalt", "Spielt in Wohnung, Haus oder Familie.", ()),
    ("setting.institution", "Institution", "Spielt in Schule, Klinik, Gefängnis, Firma oder Behörde.", ()),
    ("setting.unterwegs", "Unterwegs & Grenzraum", "Spielt auf Reisen, an Grenzen oder im Transit.", ()),
    ("reference.mythos", "Mythosbearbeitung", "Bearbeitet antike oder andere Mythen.", ()),
    ("reference.maerchen", "Märchenbearbeitung", "Bearbeitet ein Märchen oder eine Sage.", ()),
    ("reference.literatur", "Literarische Adaption", "Bühnenfassung eines Romans oder anderer Literatur.", ()),
)


def _key(value: str) -> str:
    return " ".join(value.casefold().split())


def sync_vocabulary(session: Session) -> None:
    """Insert or update the built-in vocabulary; retired codes are deactivated, never deleted."""
    session.info.pop("taxonomy_aliases", None)
    existing = {tag.code: tag for tag in session.exec(select(Tag))}
    for position, (code, label, definition, aliases) in enumerate(VOCABULARY):
        layer = FACETS[code.split(".", 1)[0]][0]
        tag = existing.pop(code, None) or Tag(code=code, label=label, layer=layer, facet=code.split(".", 1)[0])
        tag.label, tag.layer, tag.definition, tag.aliases = label, layer, definition, list(aliases)
        tag.position, tag.active = position, True
        session.add(tag)
    for tag in existing.values():
        tag.active = False
        session.add(tag)


def _alias_map(session: SASession) -> dict[str, str]:
    if "taxonomy_aliases" not in session.info:
        tags = session.scalars(select(Tag).where(col(Tag.active)))
        session.info["taxonomy_aliases"] = {_key(alias): tag.code for tag in tags for alias in tag.aliases or ()}
    return session.info["taxonomy_aliases"]


def _source_codes(tags: Iterable[str], aliases: dict[str, str]) -> dict[str, str]:
    """Map legacy tags to codes, keeping the first matching source label as evidence."""
    codes: dict[str, str] = {}
    for tag in tags or ():
        if isinstance(tag, str) and (code := aliases.get(_key(tag))):
            codes.setdefault(code, tag)
    return codes


def sync_source_tags(session: SASession, productions: list[Production]) -> None:
    """Align source-origin assignments with legacy tags; manual decisions always win."""
    aliases = _alias_map(session)
    if not aliases or not productions:
        return
    existing: dict[uuid.UUID, dict[str, ProductionTag]] = defaultdict(dict)
    for batch in batched([p.id for p in productions], 500, strict=False):
        for row in session.scalars(select(ProductionTag).where(col(ProductionTag.production_id).in_(batch))):
            existing[row.production_id][row.tag_code] = row
    for row in session.new:
        if isinstance(row, ProductionTag):
            existing[row.production_id][row.tag_code] = row
    for production in productions:
        _sync_one(session, production.id, _source_codes(production.tags or [], aliases), existing[production.id])


def _sync_one(
    session: SASession, production_id: uuid.UUID, wanted: dict[str, str], current: dict[str, ProductionTag]
) -> None:
    for code, label in wanted.items():
        row = current.get(code)
        if row is None:
            session.add(
                ProductionTag(
                    production_id=production_id,
                    tag_code=code,
                    status=TagStatus.PUBLISHED,
                    origin=TagOrigin.SOURCE,
                    evidence=f"Quellschlagwort: {label}",
                )
            )
        elif row.origin == TagOrigin.MODEL:
            # Source evidence supersedes an unreviewed model guess.
            row.origin, row.status, row.evidence, row.score = (
                TagOrigin.SOURCE,
                TagStatus.PUBLISHED,
                f"Quellschlagwort: {label}",
                None,
            )
    for code, row in current.items():
        if row.origin == TagOrigin.SOURCE and code not in wanted:
            (session.expunge if row in session.new else session.delete)(row)


def _changed_tag_productions(session: SASession) -> list[Production]:
    rows = [row for row in session.new if isinstance(row, Production)]
    rows += [
        row
        for row in session.dirty
        if isinstance(row, Production)
        and row not in session.deleted
        and sa_inspect(row).attrs.tags.history.has_changes()
    ]
    return rows


def on_before_flush(session: SASession) -> None:
    """Keep source assignments current on every write path (imports, merges, editors)."""
    if productions := _changed_tag_productions(session):
        sync_source_tags(session, productions)


def backfill_source_tags(session: Session, *, batch_size: int = 2000) -> int:
    """Map legacy tags of all productions in batches; returns the number of productions processed."""
    session.info.pop("taxonomy_aliases", None)
    last: uuid.UUID | None = None
    processed = 0
    while True:
        stmt = select(Production).order_by(col(Production.id))
        if last is not None:
            stmt = stmt.where(col(Production.id) > last)
        rows = list(session.exec(stmt.limit(batch_size)))
        if not rows:
            return processed
        sync_source_tags(session, rows)
        session.commit()
        processed += len(rows)
        last = rows[-1].id
        session.expunge_all()


def _audit(session: Session, row: ProductionTag, operation: str, actor_id: str | None) -> None:
    session.flush()
    add_scd2_event(
        session,
        entity_name="productiontag",
        entity_id=str(row.id),
        operation=operation,
        snapshot=row.model_dump(mode="json"),
        actor_id=actor_id,
    )


def _decide(
    session: Session, production_id: uuid.UUID, code: str, status: TagStatus, actor_id: str | None
) -> ProductionTag:
    row = session.exec(
        select(ProductionTag).where(ProductionTag.production_id == production_id, ProductionTag.tag_code == code)
    ).one_or_none()
    operation = "update"
    if row is None:
        row = ProductionTag(production_id=production_id, tag_code=code, status=status, origin=TagOrigin.MANUAL)
        operation = "insert"
    row.status, row.origin = status, TagOrigin.MANUAL
    row.decided_by, row.decided_at = actor_id, dt.datetime.now(dt.UTC)
    session.add(row)
    _audit(session, row, operation, actor_id)
    return row


def set_production_tags(session: Session, production_id: uuid.UUID, codes: set[str], actor_id: str | None) -> None:
    """Publish the selected codes manually; explicitly removed public tags become manual rejections."""
    known = set(session.exec(select(Tag.code).where(col(Tag.active))))
    if unknown := codes - known:
        raise ValueError(f"Unknown tags: {', '.join(sorted(unknown))}")
    current = {
        row.tag_code: row
        for row in session.exec(select(ProductionTag).where(ProductionTag.production_id == production_id))
    }
    for code in codes:
        row = current.get(code)
        if row is None or row.status != TagStatus.PUBLISHED or row.origin != TagOrigin.MANUAL:
            _decide(session, production_id, code, TagStatus.PUBLISHED, actor_id)
    for code, row in current.items():
        if code in known and code not in codes and row.status == TagStatus.PUBLISHED:
            _decide(session, production_id, code, TagStatus.REJECTED, actor_id)


def decide_suggestion(
    session: Session, production_id: uuid.UUID, code: str, *, accept: bool, actor_id: str | None
) -> None:
    """Accept or reject a pending suggestion; the decision becomes manual and sticky."""
    _decide(session, production_id, code, TagStatus.PUBLISHED if accept else TagStatus.REJECTED, actor_id)


def prepare_production_merge(session: Session, target_id: uuid.UUID, duplicate_id: uuid.UUID) -> None:
    """Drop colliding duplicate assignments before FK reassignment; a manual decision survives."""
    target = {
        row.tag_code: row for row in session.exec(select(ProductionTag).where(ProductionTag.production_id == target_id))
    }
    for row in session.exec(select(ProductionTag).where(ProductionTag.production_id == duplicate_id)):
        kept = target.get(row.tag_code)
        if kept is None:
            continue
        if row.origin == TagOrigin.MANUAL and kept.origin != TagOrigin.MANUAL:
            session.delete(kept)
        else:
            session.delete(row)
    session.flush()


def facet_filters(selected: dict[str, list[str]]) -> list[ColumnElement[bool]]:
    """OR within a facet, AND across facets; only published assignments count."""
    return [
        col(Production.id).in_(
            select(ProductionTag.production_id).where(
                col(ProductionTag.tag_code).in_(codes), ProductionTag.status == TagStatus.PUBLISHED
            )
        )
        for codes in selected.values()
        if codes
    ]


def facet_options(session: Session) -> dict[str, list[tuple[str, str]]]:
    """Active tags grouped by facet in display order."""
    options: dict[str, list[tuple[str, str]]] = {facet: [] for facet in FACETS}
    for tag in session.exec(select(Tag).where(col(Tag.active)).order_by(col(Tag.position), col(Tag.code))):
        options.setdefault(tag.facet, []).append((tag.code, tag.label))
    return {facet: values for facet, values in options.items() if values}


def production_tags(session: Session, production_id: uuid.UUID) -> list[dict[str, Any]]:
    """All assignments of one production with tag metadata, in facet and vocabulary order."""
    rows = session.exec(
        select(ProductionTag, Tag)
        .join(Tag, col(Tag.code) == col(ProductionTag.tag_code))
        .where(ProductionTag.production_id == production_id)
    ).all()
    facet_order = list(FACETS)
    rows = sorted(
        rows,
        key=lambda pair: (
            facet_order.index(pair[1].facet) if pair[1].facet in facet_order else len(facet_order),
            pair[1].position,
        ),
    )
    return [
        {
            "code": tag.code,
            "label": tag.label,
            "layer": tag.layer,
            "facet": tag.facet,
            "status": row.status,
            "origin": row.origin,
            "evidence": row.evidence,
            "score": row.score,
        }
        for row, tag in rows
    ]


def main(argv: list[str] | None = None) -> int:
    """Maintain the controlled vocabulary and legacy source mapping."""
    from backend.user_management import get_session, init_db  # ruff: ignore[import-outside-top-level]

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("command", choices=["backfill"], help="Map legacy tags of all productions")
    parser.parse_args(argv)
    init_db()
    with get_session() as session:
        count = backfill_source_tags(session)
    print(f"Mapped legacy tags of {count} productions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
