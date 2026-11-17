"""Opt-in AI suggestions: no helper writes domain records or titles."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable
from sqlmodel import Session, col, select

from backend import wikidata
from backend.models import EntitySource, EntityType, ReconciliationCase, Source, Text, TextTitle
from backend.reconciliation import normalize, stable_digest

TIMEOUT = 30
MAX_RESPONSE_BYTES = 2_000_000
_CREDIT_LISTS = (
    "authors",
    "translators",
    "adapters",
    "source_authors",
    "source_titles",
    "source_notes",
    "unparsed",
)


class AIError(RuntimeError):
    """AI transport, output, or schema validation failed."""


class AIDisabledError(AIError):
    """Required AI configuration is absent; no request was made."""


def enabled() -> bool:
    """Whether all explicit opt-in environment settings exist."""
    return all(os.environ.get(key, "").strip() for key in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL"))


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Invalid JSON number: {value}")
    return number


def _completion_body(request: Request) -> bytes:
    # URL scheme validated by complete_json.
    with urlopen(request, timeout=TIMEOUT) as response:  # ruff: ignore[suspicious-url-open-usage]
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise AIError("AI response exceeds size limit")
    return body


def complete_json(prompt: str, schema: dict) -> dict:
    """Validate completions with JSON Schema 2020-12; schema references stay offline."""
    if not enabled():
        raise AIDisabledError("AI disabled: set AI_BASE_URL, AI_API_KEY and AI_MODEL")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise AIError(f"Invalid JSON schema: {exc.message}") from exc
    validator = Draft202012Validator(schema, registry=Registry())
    base = os.environ["AI_BASE_URL"].strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment or parts.username:
        raise AIError("AI_BASE_URL must be an HTTP(S) API base URL without credentials or query")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": os.environ["AI_MODEL"].strip(),
        "messages": [
            {"role": "system", "content": "Return only a JSON object matching this schema: " + json.dumps(schema)},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    request = Request(  # ruff: ignore[suspicious-url-open-usage] - Scheme validated above.
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["AI_API_KEY"].strip(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        envelope = json.loads(_completion_body(request), parse_constant=_finite_number, parse_float=_finite_number)
        content = envelope["choices"][0]["message"]["content"]
        result = json.loads(content, parse_constant=_finite_number, parse_float=_finite_number)
    except HTTPError as exc:
        raise AIError(f"AI HTTP error {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise AIError(f"AI request failed: {exc.reason if isinstance(exc, URLError) else 'timeout'}") from exc
    except (KeyError, IndexError, TypeError, ValueError, UnicodeError) as exc:
        raise AIError("AI returned invalid JSON completion") from exc
    if not isinstance(result, dict):
        raise AIError("AI completion must be a JSON object")
    try:
        validator.validate(result)
    except ValidationError as exc:
        raise AIError(f"AI JSON schema mismatch at {exc.json_path}: {exc.message}") from exc
    except Unresolvable as exc:
        raise AIError(f"AI JSON schema reference cannot be resolved offline: {exc.ref}") from exc
    return result


def _object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STRIP = " \t\n,.;:/|\u2013-"
_CREDIT_MARKER = re.compile(
    r"(?P<translators>\b(?:deutsch(?:e\s+fassung)?|aus\s+dem\s+\w+|[üu]bersetz\w*|[üu]bertr\w*)\s+von\b)"
    r"|(?P<adapters>\b(?:in\s+(?:einer|der)\s+)?(?:bühnen)?(?:fassung|bearbeitung)\b(?:\s+von\b)?"
    r"|\bbearbeitet\s+von\b)"
    r"|(?P<source>\b(?:frei\s+)?nach\b)",
    re.IGNORECASE,
)
_QUOTED = re.compile(
    r"[\u201e\u201c\"\u00ab\u201a]([^\u201e\u201c\u201d\"\u00ab\u00bb\u201a\u2018\u2019]+)[\u201c\u201d\"\u00bb\u2018\u2019]"
)
# Start whitespace scans only at run boundaries; retries inside a run are quadratic.
_NAME_SEPARATOR = re.compile(r"(?:(?<!\s)\s+)?,\s*|(?<!\s)\s+(?:und|&)\s+")
_CLAUSE_SEPARATOR = re.compile(r"(?:(?<!\s)\s+)?[/;|·\n]\s*")


def _names(raw: str) -> list[str]:
    return [name for part in _NAME_SEPARATOR.split(raw) if (name := part.strip(_STRIP))]


def _add(result: dict, field: str, values: list[str]) -> None:
    result[field].extend(value for value in dict.fromkeys(values) if value and value not in result[field])


def _parse_source(result: dict, raw: str) -> bool:
    """Record source works/authors separately; returns whether the source shares the title."""
    description, separator, people = raw.rpartition(" von ")
    if not separator:
        description, people = raw, ""
    quoted = [title.strip() for title in _QUOTED.findall(description)]
    _add(result, "source_titles", quoted)
    rest = _QUOTED.sub("", description).strip(_STRIP)
    if not separator and not quoted:
        _add(result, "source_authors", _names(rest))
        return False
    if rest:
        _add(result, "source_notes", [rest])
    _add(result, "source_authors", _names(people))
    return re.search(r"gleichnamig", rest, re.IGNORECASE) is not None


def _parse_head(result: dict, head: str, first: bool) -> tuple[bool, str | None]:
    """Parse text before markers; returns (recognized, title candidate without credits)."""
    title, separator, people = head.rpartition(" von ")
    if re.match(r"von\s", head, re.IGNORECASE):
        _add(result, "authors", _names(head[4:]))
        return True, None
    if separator and first:
        result["title"] = title.strip(_STRIP)
        _add(result, "authors", _names(people))
        return True, None
    if head and first:
        return False, head
    if head:
        result["unparsed"].append(head)
    return False, None


def _regex_credits(credit: str) -> dict:
    result: dict = {"title": None, **{field: [] for field in _CREDIT_LISTS}}
    title_only, same_title, recognized = None, False, False
    clauses = [clause for part in _CLAUSE_SEPARATOR.split(credit) if (clause := part.strip(_STRIP))]
    for index, clause in enumerate(clauses):
        matches = list(_CREDIT_MARKER.finditer(clause))
        head = clause[: matches[0].start() if matches else len(clause)].strip(_STRIP)
        head_recognized, candidate = _parse_head(result, head, index == 0)
        recognized |= head_recognized
        title_only = title_only or candidate
        for position, match in enumerate(matches):
            stop = matches[position + 1].start() if position + 1 < len(matches) else len(clause)
            raw = re.sub(r"^von\s+", "", clause[match.end() : stop].strip(_STRIP), flags=re.IGNORECASE)
            if not raw:
                result["unparsed"].append(match.group())
            elif match.lastgroup == "source":
                same_title |= _parse_source(result, raw)
            else:
                _add(result, cast(str, match.lastgroup), _names(raw))
            recognized = True
    if title_only and recognized:
        result["title"] = title_only
    elif title_only:
        result["unparsed"].append(title_only)
    if same_title and result["title"]:
        _add(result, "source_titles", [result["title"]])
    return result


def parse_credits(credit: str, *, use_ai: bool = False) -> dict:
    """Parse German work credits into reviewable proposals; never resolves or writes people.

    Returns ``title`` plus lists ``authors`` (of this text), ``translators``, ``adapters``,
    ``source_authors``/``source_titles``/``source_notes`` (the Vorlage, never credited to this
    text) and ``unparsed``. The opt-in LLM fallback sees the full credit, and names it returns
    that do not occur in the credit are dropped.
    """
    result = _regex_credits(credit)
    if not use_ai or not result["unparsed"]:
        return result
    schema = _object_schema(
        {
            "title": {"type": ["string", "null"]},
            **{field: {"type": "array", "items": {"type": "string", "minLength": 1}} for field in _CREDIT_LISTS},
        }
    )
    suggested = complete_json(
        "Parse this German theatre credit for the text it describes. 'authors' are only authors of this "
        "text; people after 'nach' (source works) go to source_authors, source work titles to "
        "source_titles, never to authors. Keep translators and adapters separate. Only copy names "
        "verbatim from the credit; put anything uncertain in unparsed.\n"
        + json.dumps({"credit": credit, "regex_parse": result}, ensure_ascii=False),
        schema,
    )
    haystack = normalize(credit)

    def verbatim(value: str) -> bool:
        return bool(normalize(value)) and normalize(value) in haystack

    title = suggested["title"] if suggested["title"] and verbatim(suggested["title"]) else None
    parsed: dict = {"title": title}
    for field in _CREDIT_LISTS:
        values = [value.strip() for value in suggested[field]]
        parsed[field] = list(
            dict.fromkeys(v for v in values if verbatim(v) or (field == "source_titles" and v == title))
        )
    return parsed


def propose_text_lineage(
    session: Session,
    text_id: uuid.UUID | str,
    candidates: list[dict],
    *,
    use_ai: bool = False,
) -> ReconciliationCase | None:
    """Queue an AI-selected candidate only; never create a lineage or a text."""
    text_id = uuid.UUID(str(text_id))
    text = session.get(Text, text_id)
    if text is None:
        raise ValueError("Text does not exist")
    supplied = {}
    for candidate in candidates:
        candidate_id = uuid.UUID(str(candidate["id"]))
        if candidate_id == text_id or session.get(Text, candidate_id) is None:
            raise ValueError("Lineage candidates must be other existing texts")
        supplied[str(candidate_id)] = json.loads(json.dumps(candidate, default=str))
    if not use_ai or not supplied:
        return None
    schema = _object_schema(
        {
            "source_text_id": {"type": ["string", "null"], "enum": [*supplied, None]},
            "relation": {"type": "string", "enum": ["translation", "adaptation", "based_on"]},
            "reason": {"type": "string"},
        }
    )
    proposal = complete_json(
        "Suggest a source work for this derived text. Choose only a supplied candidate ID or null. "
        "No identical-title inference without evidence.\n"
        + json.dumps({"text": text.model_dump(mode="json"), "candidates": list(supplied.values())}, default=str),
        schema,
    )
    if proposal["source_text_id"] is None:
        return None
    if proposal["source_text_id"] not in supplied:
        raise AIError("AI selected an unsupplied candidate")
    if proposal["relation"] not in {"translation", "adaptation", "based_on"}:
        raise AIError("AI selected an unsupported lineage relation")
    incoming = {
        "source_candidate_id": proposal["source_text_id"],
        "relation": proposal["relation"],
        "reason": proposal["reason"],
    }
    key = "text_lineage:ai:" + stable_digest(
        {
            "target_id": str(text_id),
            "source_candidate_id": incoming["source_candidate_id"],
            "relation": incoming["relation"],
        }
    )
    existing = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
    if existing is not None:
        return existing
    case = ReconciliationCase(
        key=key,
        entity_type=EntityType.TEXT,
        operation="text_lineage",
        target_id=text_id,
        incoming=incoming,
        context={"candidates": list(supplied.values()), "ai": True},
        reason=proposal["reason"] or "AI lineage suggestion; human review required",
    )
    session.add(case)
    session.flush()
    return case


def _known_wikidata_titles(session: Session, text_id: uuid.UUID, known: dict) -> None:
    links = session.exec(
        select(EntitySource)
        .join(Source, col(Source.id) == col(EntitySource.source_id))
        .where(
            EntitySource.entity_type == EntityType.TEXT,
            EntitySource.entity_id == text_id,
            col(Source.name).ilike("wikidata"),
        )
    ).all()
    for link in links:
        if link.external_id:
            entity = wikidata.fetch_entity(link.external_id)
            for language, label in entity.get("labels", {}).items():
                known[normalize(label["value"]), language] = "wikidata"
            for language, aliases in entity.get("aliases", {}).items():
                for alias in aliases:
                    known[normalize(alias["value"]), language] = "wikidata"


def suggest_titles(session: Session, text_id: uuid.UUID | str, *, use_ai: bool = False) -> list[dict]:
    """Return known titles and optional unverified suggestions; never add a title."""
    text_id = uuid.UUID(str(text_id))
    text = session.get(Text, text_id)
    if text is None:
        raise ValueError("Text does not exist")
    titles = session.exec(select(TextTitle).where(TextTitle.text_id == text_id)).all()
    result = [{"title": row.title, "language": row.language, "verified": True, "source": "database"} for row in titles]
    if not any(normalize(row["title"]) == normalize(text.title) for row in result):
        result.append(
            {
                "title": text.title,
                "language": text.languages[0] if text.languages else None,
                "verified": True,
                "source": "database",
            }
        )
    if not use_ai:
        return result
    known = {(normalize(row["title"]), row["language"]): row["source"] for row in result}
    _known_wikidata_titles(session, text_id, known)
    proposal = complete_json(
        "Suggest alternative titles with their language codes. Do not claim verification.\n"
        + json.dumps({"text": text.model_dump(mode="json"), "known_titles": result}, default=str),
        _object_schema(
            {
                "titles": {
                    "type": "array",
                    "maxItems": 20,
                    "items": _object_schema(
                        {
                            "title": {"type": "string", "minLength": 1},
                            "language": {"type": ["string", "null"]},
                        }
                    ),
                }
            }
        ),
    )
    seen = {(normalize(row["title"]), row["language"]) for row in result}
    for suggestion in proposal["titles"]:
        key = (normalize(suggestion["title"]), suggestion["language"])
        if key not in seen:
            result.append({**suggestion, "verified": key in known, "source": known.get(key, "unverified_ai")})
            seen.add(key)
    return result
