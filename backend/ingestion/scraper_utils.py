"""Shared text and credit normalization helpers for configured scrapers."""

import re

from backend.config import CREDIT_ROLES, ROLE_TYPE_PATTERNS, UNMAPPED_CREDIT_TITLES, canonical_role

_NAME_NOISE = re.compile(
    r"\b(Herr|Frau|Dr\.?|Prof\.?)\b\.?|\s*\(.*?\)|\s*\(?\bStaats?schauspieler\b\)?",
    re.IGNORECASE,
)


def parse_date_dmy(text: str) -> str | None:
    """Extract DD.MM.YYYY date string, return as ISO YYYY-MM-DD or None."""
    if match := re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text):
        return f"{match.group(3)}-{match.group(2).zfill(2)}-{match.group(1).zfill(2)}"
    return None


def parse_duration_minutes(text: str) -> int | None:
    """Extract duration in minutes from German and abbreviated duration labels."""
    if match := re.search(r"(\d+)\s*:\s*([0-5]\d)\s*(?:Std\.?|h)", text, re.IGNORECASE):
        return int(match[1]) * 60 + int(match[2])
    hours = 0
    minutes = 0
    if match := re.search(r"(\d+)\s*(?:Stunden?|Std\.?|h)\b", text):
        hours = int(match.group(1))
    if (match := re.search(r"(\d+)\s*(?:Minuten?|Min\.?|m)\b", text)) or (
        match := re.search(r"(?:Stunden?|Std\.?|h)\s*(\d+)\b", text)
    ):
        minutes = int(match.group(1))
    total = hours * 60 + minutes
    return total or None


def infer_person_type(role: str) -> str:
    """Map a role label to a person type string; defaults to 'actor'."""
    canonical = canonical_role(role)
    if canonical in UNMAPPED_CREDIT_TITLES:
        return canonical
    if canonical is not None and canonical in CREDIT_ROLES:
        return canonical
    if canonical and canonical.startswith(("assistenz ", "leitung ")):
        return canonical
    for pattern, kind in ROLE_TYPE_PATTERNS:
        if pattern.search(role):
            return kind
    return "actor"


def normalize_person_name(name: str) -> str:
    """Strip honorifics/noise and normalize whitespace."""
    return re.sub(r"\s{2,}", " ", _NAME_NOISE.sub("", name)).strip()


def extract_author_info(line: str) -> tuple[str | None, str]:
    """Parse an author line like 'Musik von Hans Werner Henze'."""
    if not line:
        return None, "author"
    name = re.sub(r"^[^:]+:\s*|^.*?\b(?:von|nach)\b\s*", "", line, flags=re.IGNORECASE).strip()
    return (name or None), infer_person_type(line)
