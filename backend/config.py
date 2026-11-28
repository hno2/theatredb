"""Shared inline lookup tables for ingestion, reconciliation and the UI."""

import datetime as dt
import re
import uuid

from backend.models import (
    Award,
    AwardNomination,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    ProductionMembership,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
)

# Editorially controlled rollover; assessments remain stored against start years.
CURRENT_SEASON_START_YEAR = 2026

# Canonical role: display label, exact aliases, pattern for descriptive source credits.
ROLE_CONFIG: dict[str, tuple[str, tuple[str, ...], str | None]] = {
    "director": (
        "Regie",
        ("regie", "inszenierung", "schauspielregie", "opernregie", "musiktheaterregie"),
        r"Regie|Inszenierung",
    ),
    "set_designer": ("Bühnenbild", ("bühne", "bühnenbild", "ausstattung", "stage design"), r"Bühne|Ausstattung"),
    "costume_designer": ("Kostüme", ("kostüm", "kostüme", "kostümbild", "costume design"), r"Kostüm"),
    "composer": ("Musik", ("musik", "komposition", "bühnenmusik"), r"Musik|Komposition"),
    "lyricist": ("Liedtexte/Gesangstexte", ("liedtexte", "gesangstexte", "lyrics"), r"Liedtext|Gesangstext|Lyrics"),
    "choreographer": (
        "Choreografie",
        ("choreografie", "choreographie", "coreographer", "choreografie / direktion"),
        r"Choreograph|Tanz",
    ),
    "dramaturg": ("Dramaturgie", ("dramaturgie",), r"Dramaturgi"),
    "light_designer": ("Licht", ("licht", "lichtdesign", "lichtgestaltung", "lighting_designer"), r"Licht"),
    "video_designer": ("Video", ("video", "videodesign", "medien"), r"Video|Medien"),
    "theatre_educator": (
        "Theaterpädagogik",
        ("vermittlung", "theaterpädagoge", "theaterpädagogin", "theaterpädagogik"),
        r"Vermittlung|Theaterpädagog",
    ),
    "author": ("Text", ("autor", "autor*in", "creator", "text", "buch"), r"Text|Buch"),
    "translator": ("Übersetzung", ("übersetzung",), r"Übersetz"),
    "adapter": ("Bearbeitung", ("bearbeitung", "fassung"), r"Bearb|Fassung"),
    "actor": (
        "Schauspiel",
        (
            "spiel",
            "schauspiel",
            "schauspieler",
            "schauspielerin",
            "schauspiel (männliche rollen)",
            "schauspiel (weibliche rollen)",
            "schauspiel (diverse rollen)",
            "sonstige schauspiel",
            "darsteller",
            "darstellerin",
            "cast",
        ),
        None,
    ),
    "singer": (
        "Gesang",
        (
            "gesang",
            "gesang (männliche solopartien)",
            "gesang (weibliche solopartien)",
            "gesang (diverse solopartien)",
            "sopran",
        ),
        None,
    ),
    "dancer": ("Tanz", ("tanz (weibliche rollen)", "tanz (männliche rollen)"), None),
    "puppeteer": ("Puppenspiel / Figurentheater", ("puppenspiel / figurentheater",), None),
    "conductor": ("Dirigat / Musikalische Leitung", ("dirigat / musikalische leitung",), None),
    "choir_director": ("Chorleitung", ("chorleitung",), None),
    "sound_designer": ("Sounddesign", ("sounddesign",), None),
    "assistant_director": (
        "Regieassistenz",
        ("assistenz schauspielregie", "assistenz musiktheaterregie"),
        None,
    ),
    "directing_leadership": ("Leitung Schauspielregie", ("leitung schauspielregie",), None),
    "other": ("Weitere Mitwirkung", (), None),
}
ROLE_LABELS = {role: label for role, (label, _, _) in ROLE_CONFIG.items()}
PRESERVED_CREDIT_LABELS = {
    "musiktheaterregie": "Musiktheaterregie",
    "choreografie / direktion": "Choreografie / Direktion",
    "sopran": "Sopran",
    "assistenz schauspielregie": "Assistenz Schauspielregie",
    "assistenz musiktheaterregie": "Assistenz Musiktheaterregie",
    "leitung schauspielregie": "Leitung Schauspielregie",
}
PERFORMER_ROLES = frozenset({"actor", "singer", "dancer", "puppeteer"})
UNMAPPED_CREDIT_TITLES = frozenset({"performance", "sonstige musiktheater"})
RECLASSIFIED_ROLE_ALIASES = frozenset(
    alias
    for role in (
        "singer",
        "dancer",
        "puppeteer",
        "conductor",
        "choir_director",
        "sound_designer",
        "assistant_director",
        "directing_leadership",
    )
    for alias in (role, *ROLE_CONFIG[role][1])
) | {
    "musiktheaterregie",
    "choreografie / direktion",
    "schauspiel (diverse rollen)",
    "sonstige schauspiel",
    "autor*in",
}
LINEAGE_LABELS = {"translation": "Übersetzung", "adaptation": "Bearbeitung", "based_on": "Vorlage / nach"}
TEXT_GENRES = {
    "play": "Schauspiel",
    "musical": "Musical",
    "opera": "Oper",
    "novel": "Roman",
    "film": "Film",
    "other": "Andere",
}
ROLE_ALIASES = {alias.casefold(): role for role, (_, aliases, _) in ROLE_CONFIG.items() for alias in (role, *aliases)}
ROLE_TYPE_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), role) for role, (_, _, pattern) in ROLE_CONFIG.items() if pattern
]
CREDIT_ROLES = frozenset(ROLE_CONFIG) - {"other"}

ENTITY_LABELS = {
    "organization": "Organisationen",
    "person": "Personen",
    "text": "Texte",
    "production": "Produktionen",
    "award": "Auszeichnungen",
}
DISCOVERY_KINDS = {
    "organization": ("Theater", Organization),
    "production": (ENTITY_LABELS["production"], Production),
    "person": (ENTITY_LABELS["person"], Person),
}
ENTITY_MODELS = {
    **{EntityType(kind): model for kind, (_, model) in DISCOVERY_KINDS.items()},
    EntityType.TEXT: Text,
    EntityType.AWARD: Award,
}
FORM_FIELD_KINDS: dict[object, str] = {
    bool: "bool",
    int: "int",
    float: "float",
    dt.date: "datetime",
    dt.datetime: "datetime",
    uuid.UUID: "uuid",
}
LINK_IDENTITY_FIELDS: dict[type, tuple[str, ...]] = {
    ProductionMembership: ("production_id", "person_id", "role_name", "character_name"),
    TextContribution: ("text_id", "person_id", "role"),
    OrganizationMembership: (
        "organization_id",
        "person_id",
        "role",
        "start_date",
        "start_date_precision",
        "end_date",
        "end_date_precision",
    ),
    TextLineage: ("source_text_id", "derived_text_id", "relation"),
    TextTitle: ("text_id", "normalized"),
    AwardNomination: ("award_id", "production_id", "person_id", "category", "date", "date_precision", "result"),
}

PRODUCTION_FIELD_LABELS = {
    "name": "Titel",
    "premiere": "Premiere",
    "performance_dates": "Vorstellungstermine",
    "description": "Beschreibung",
    "duration": "Dauer (Min.)",
    "suggested_min_age": "Altersempfehlung",
    "url": "Website",
    "tags": "Schlagworte",
    "critics": "Kritiken",
}
IMPORT_COVERAGE_FIELDS = {
    "description": PRODUCTION_FIELD_LABELS["description"],
    "premiere": PRODUCTION_FIELD_LABELS["premiere"],
    "director": ROLE_LABELS["director"],
    "cast": "Besetzung",
    "text": "Werk",
}
IMPORT_RESULT_LABELS = {
    "merged": "Ergänzt",
    "created": "Neu angelegt",
    "review": "Zur Prüfung",
    "unchanged": "Unverändert",
}
FIELD_STATUS = {
    "same": "✅ gleich",
    "new": "🆕 wird ergänzt",
    "extended": "🔗 wird erweitert",
    "kept": "◻️ bleibt (nur Bestand)",
    "conflict": "⚠️ Konflikt - bitte wählen",
}
PEOPLE_STATUS = {
    "same": "✅ in beiden",
    "new_known": "🆕 wird verknüpft (Person existiert)",
    "new_person": "🆕 Person wird neu angelegt",
    "kept": "◻️ nur Bestand",
}
REVIEW_STATUS_LABELS = {"pending": "Offen", "approved": "Genehmigt", "rejected": "Abgelehnt"}
ROW_COLORS = {
    "conflict": "#fde2e1",
    "new": "#e3f6e8",
    "extended": "#e3f6e8",
    "new_known": "#e3f6e8",
    "new_person": "#fff4d6",
}
CONFLICT_CHOICE_LABELS = {"existing": "Bestand behalten", "incoming": "Upload übernehmen"}
SCRAPE_SCOPES = {"all": "vollständig"}
SCRAPE_FRESHNESS = ((30, "🟢"), (90, "🟡"))

TEXT_TRANSLATION = str.maketrans({"\u00ad": None, "\u200b": None, "\ufeff": None, "\u00a0": " "})
IMAGE_SIGNATURES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"GIF8": ".gif", b"RIFF": ".webp"}


def canonical_role(value: str | None) -> str | None:
    """Canonicalize known credit labels, preserving unknown roles."""
    if not value or not value.strip():
        return None
    value = value.strip().casefold()
    return ROLE_ALIASES.get(value, value)


def credit_role(value: str | None) -> str | None:
    """Normalize credit identity while retaining meaningful source-specific titles."""
    if value and (normalized := value.strip().casefold()) in PRESERVED_CREDIT_LABELS:
        return normalized
    return canonical_role(value)


def role_label(role: str | None) -> str:
    """Translate known canonical roles while retaining source-specific labels."""
    if role and (label := PRESERVED_CREDIT_LABELS.get(role.strip().casefold())):
        return label
    return ROLE_LABELS.get(canonical_role(role) or "", role or "")
