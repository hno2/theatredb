"""Persistent scrape history and readable, order-independent field diffs."""

import datetime as dt
import hashlib
import json
import tempfile
import textwrap
from operator import itemgetter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.ingestion.configurable import PageKind, ScraperConfig
from backend.ingestion.staging import HttpURL, StagedMembership, StagedProduction, validate_records


def digest(value: object) -> str:
    """Hash a stable JSON representation, not Python's randomized hash()."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_json(path: Path, data: object) -> None:
    """Write JSON atomically so an interrupted check cannot corrupt its baseline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _normalize(value: object) -> object:
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in sorted(value.items()) if item not in (None, "", [], {})}
    if isinstance(value, list):
        return sorted((_normalize(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _label(record: dict) -> str:
    return str(record.get("title") or record.get("name") or record["url"])


def _member_key(member: dict) -> str:
    return json.dumps([member.get("url") or member["name"], member.get("role"), member.get("character")])


def _value_lines(prefix: str, value: object) -> list[str]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return [prefix + line for block in text.splitlines() for line in (textwrap.wrap(block, width=100) or [""])]


def field_diff(before: dict, after: dict, *, prefix: str = "") -> list[str]:
    """Show old/new values and identify people by profile/name and credit, not list index."""
    lines = []
    for field in sorted(before.keys() | after.keys()):
        left, right = before.get(field), after.get(field)
        if _normalize(left) == _normalize(right):
            continue
        path = f"{prefix}{field}"
        if field == "memberships" and isinstance(left, list) and isinstance(right, list):
            old = {_member_key(member): member for member in left}
            new = {_member_key(member): member for member in right}
            for key in sorted(old.keys() | new.keys()):
                member = new.get(key) or old[key]
                label = f"{member['name']} ({member.get('role') or 'actor'})"
                if key not in old:
                    lines.append(f"  + {path}: {label}")
                elif key not in new:
                    lines.append(f"  - {path}: {label}")
                else:
                    lines.extend(field_diff(old[key], new[key], prefix=f"{path} > {label} > "))
        else:
            lines.append(f"  {path}:")
            lines.extend(_value_lines("    - ", left))
            lines.extend(_value_lines("    + ", right))
    return lines


def _by_url(records: list[dict]) -> dict[str, dict]:
    result = {}
    for record in records:
        url = record["url"]
        if url in result:
            raise ValueError(f"Duplicate production URL in snapshot: {url}")
        result[url] = record
    return result


def scrape_diff(before: list[dict], after: list[dict]) -> str:
    """Summarize additions, removals and changed fields without list-order noise."""
    old, new = _by_url(before), _by_url(after)
    added, removed = new.keys() - old.keys(), old.keys() - new.keys()
    common = old.keys() & new.keys()
    changed = {url for url in common if _normalize(old[url]) != _normalize(new[url])}
    lines = [
        f"Productions: +{len(added)} added, -{len(removed)} removed, "
        f"~{len(changed)} changed, ={len(common) - len(changed)} unchanged"
    ]
    lines.extend(f"+ {_label(new[url])} -- {url}" for url in sorted(added))
    lines.extend(f"- {_label(old[url])} -- {url}" for url in sorted(removed))
    for url in sorted(changed):
        lines.append(f"~ {_label(new[url])} -- {url}")
        lines.extend(field_diff(old[url], new[url]))
    return "\n".join(lines)


def save_scrape(
    records: list[dict],
    config: ScraperConfig,
    output_dir: Path,
    *,
    limit: int | None,
    enrich_people: bool,
) -> tuple[Path, str]:
    """Compare only equivalent scopes; never overwrite a previous same-day scrape."""
    records = sorted(validate_records(records), key=itemgetter("url"))
    _by_url(records)
    scope = f"{'all' if limit is None else f'limit-{limit}'}-{'people' if enrich_people else 'no-people'}"
    scope += "-" + digest(config.model_dump(mode="json"))[:12]
    directory = output_dir / config.source_id
    previous = sorted(directory.glob(f"*_{scope}_productions.json"))
    old = validate_records(json.loads(previous[-1].read_text(encoding="utf-8"))) if previous else []
    report = scrape_diff(old, records)
    if not previous:
        report = "No previous scrape for this config and scope; creating baseline.\n" + report
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S_%f")
    path = directory / f"{stamp}_{scope}_productions.json"
    write_json(path, records)
    return path, report


class PageSnapshot(BaseModel):
    """Versioned local baseline for exactly one page, parser config and page kind."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    url: HttpURL
    kind: PageKind
    config_digest: str
    html_digest: str
    captured_at: dt.datetime
    data: dict


def check_page(
    html: str,
    data: dict,
    url: str,
    kind: PageKind,
    config: ScraperConfig,
    state_dir: Path,
    *,
    accept: bool = False,
    data_only: bool = False,
) -> tuple[int, str]:
    """Create an initial baseline; later checks are read-only unless explicitly accepted."""
    model = StagedProduction if kind == "production" else StagedMembership
    data = model.model_validate(data).model_dump(mode="json")
    config_digest = digest(config.model_dump(mode="json"))
    path = state_dir / config.source_id / f"{kind}-{digest([url, kind, config_digest])}.json"
    current = PageSnapshot(
        url=url,
        kind=kind,
        config_digest=config_digest,
        html_digest=hashlib.sha256(html.encode()).hexdigest(),
        captured_at=dt.datetime.now(dt.UTC),
        data=data,
    )
    if not path.exists():
        write_json(path, current.model_dump(mode="json"))
        return 0, f"Baseline created for {url}\n{path}"
    before = PageSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    if (before.url, before.kind, before.config_digest) != (url, kind, config_digest):
        raise ValueError(f"Baseline identity mismatch: {path}")
    previous = model.model_validate(before.data).model_dump(mode="json")
    html_changed = before.html_digest != current.html_digest
    changes = field_diff(previous, data)
    lines = [
        f"Page: {url}",
        f"HTML: {'changed' if html_changed else 'unchanged'}; extracted data: {'changed' if changes else 'unchanged'}.",
        *changes,
    ]
    if accept:
        write_json(path, current.model_dump(mode="json"))
        lines.append("Current page accepted as new baseline.")
    return int(bool(changes) or (html_changed and not data_only)), "\n".join(lines)
