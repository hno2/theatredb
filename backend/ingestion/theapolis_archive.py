"""Content-addressed, resumable Theapolis snapshots. No application DB access."""

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
import zlib
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.locations import split_country

SCHEMA = """
create table if not exists meta (id text primary key);
create table if not exists run (
    id integer primary key, scope text not null, captured_at text not null,
    pages integer not null, total integer not null, partial integer not null,
    sealed integer not null default 0
);
create table if not exists body (hash blob primary key, data blob not null) without rowid;
create table if not exists page (
    run integer, number integer, meta blob not null, primary key (run, number)
) without rowid;
create table if not exists item (
    run integer, id text, page integer not null, position integer not null,
    org text, country text, raw blob not null, ready blob, detail blob,
    primary key (run, id)
) without rowid;
create index if not exists item_page on item (run, page, position);
create index if not exists item_org on item (run, org);
create table if not exists cache (
    kind text, key text, hash blob not null, captured_at real not null, primary key (kind, key)
) without rowid;
"""


def source_id(value: object) -> str:
    """Return a stable source key without accepting missing or malformed IDs."""
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip() or "\0" in str(value):
        raise ValueError(f"Invalid Theapolis source ID: {value!r}")
    return str(value)


@contextmanager
def open_archive(path: Path, *, write: bool = False) -> Generator[Archive]:
    """Open or initialize the independent SQLite archive."""
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    with_db = sqlite3.connect(f"{path.resolve().as_uri()}?mode={'rwc' if write else 'ro'}", uri=True)
    try:
        version = with_db.execute("pragma user_version").fetchone()[0]
        if write and version == 0:
            with_db.execute("pragma journal_mode = wal")
            with_db.execute("pragma synchronous = normal")
            with_db.executescript(SCHEMA)
            with with_db:
                with_db.execute("insert into meta values (?)", (str(uuid.uuid4()),))
                with_db.execute("pragma user_version = 3")
        elif version != 3:
            raise ValueError(f"Unsupported Theapolis archive version {version}: {path}")
        if write:
            with_db.execute("pragma synchronous = normal")
        yield Archive(with_db)
    finally:
        with_db.close()


class Archive:
    """Versioned Theapolis observations and content-addressed values."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Bind an open SQLite connection."""
        self.db = db
        self._get = lru_cache(maxsize=16384)(self._read)

    @property
    def identity(self) -> str:
        """Archive identifier stored by the DB checkpoint."""
        return self.db.execute("select id from meta").fetchone()[0]

    def _put(self, value: object) -> bytes:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        key = hashlib.sha256(raw).digest()
        if self.db.execute("select 1 from body where hash = ?", (key,)).fetchone() is None:
            self.db.execute("insert into body values (?, ?)", (key, zlib.compress(raw, 1)))
        return key

    def _read(self, key: bytes) -> Any:  # ruff: ignore[any-type] - decoding heterogeneous source JSON
        row = self.db.execute("select data from body where hash = ?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"Missing Theapolis archive object: {key.hex()}")
        raw = zlib.decompress(row[0])
        if hashlib.sha256(raw).digest() != key:
            raise ValueError(f"Corrupt Theapolis archive object: {key.hex()}")
        return json.loads(raw)

    def _pack(self, item: dict) -> bytes:
        fields = {}
        refs: dict[str, str | list[str]] = {}
        for key, value in item.items():
            if key == "organization" and isinstance(value, dict):
                refs[key] = self._put(value).hex()
            elif key in ("directors", "creators") and isinstance(value, list):
                refs[key] = [
                    self._put(
                        [{k: v for k, v in person.items() if k != "profile"}, self._put(person["profile"]).hex()]
                        if isinstance(person, dict) and "profile" in person
                        else [person]
                    ).hex()
                    for person in value
                ]
            else:
                fields[key] = value
        return self._put([fields, refs])

    def _unpack(self, key: bytes) -> dict:
        fields, refs = self._get(key)
        item = dict(fields)
        for field, value in refs.items():
            if field == "organization":
                item[field] = dict(self._get(bytes.fromhex(value)))
                continue
            people = []
            for ref in value:
                person, *profile = self._get(bytes.fromhex(ref))
                people.append({**person, "profile": self._get(bytes.fromhex(profile[0]))} if profile else person)
            item[field] = people
        return item

    def start(self, scope: str, pages: int, total: int, *, partial: bool, captured_at: str | None = None) -> int:
        """Start an unsealed capture."""
        with self.db:
            return self.db.execute(
                "insert into run (scope, captured_at, pages, total, partial) values (?, ?, ?, ?, ?) returning id",
                (scope, captured_at or dt.datetime.now(dt.UTC).isoformat(), pages, total, partial),
            ).fetchone()[0]

    def pending(self, scope: str) -> tuple[int, int, int] | None:
        """Find the resumable capture for this query scope."""
        return self.db.execute(
            "select id, pages, total from run where scope = ? and sealed = 0 order by id desc limit 1", (scope,)
        ).fetchone()

    def abandon(self, scope: str) -> None:
        """Keep incomplete evidence without letting future runs resume it."""
        with self.db:
            self.db.execute("update run set sealed = -1 where scope = ? and sealed = 0", (scope,))

    def completed_pages(self, run: int) -> set[int]:
        """Return page numbers already committed."""
        return {row[0] for row in self.db.execute("select number from page where run = ?", (run,))}

    def write_page(self, run: int, page: int, data: dict, *, ready: bool = False) -> None:
        """Commit a complete source page as one checkpoint."""
        with self.db:
            for pos, item in enumerate(data["items"]):
                org = item.get("organization") or {}
                country, _ = split_country((org.get("location") or {}).get("country"))
                key = self._pack(item)
                self.db.execute(
                    "insert into item values (?, ?, ?, ?, ?, ?, ?, ?, null)",
                    (
                        run,
                        source_id(item["id"]),
                        page,
                        pos,
                        source_id(org["id"]) if org else None,
                        country,
                        key,
                        key if ready else None,
                    ),
                )
            header = {key: value for key, value in data.items() if key != "items"}
            self.db.execute("insert into page values (?, ?, ?)", (run, page, self._put(header)))

    def raw_items(self, run: int, page: int) -> Iterator[dict]:
        """Rehydrate raw source observations for a page."""
        for (key,) in self.db.execute("select raw from item where run = ? and page = ? order by position", (run, page)):
            yield self._unpack(key)

    def put_ready(self, run: int, items: list[dict]) -> None:
        """Commit the enriched variants without replacing raw source objects."""
        with self.db:
            for item in items:
                self.db.execute(
                    "update item set ready = ? where run = ? and id = ?",
                    (self._pack(item), run, source_id(item["id"])),
                )

    def seal(self, run: int, *, carry_details: bool = False) -> None:
        """Publish the run only when its required list data is complete."""
        manifest = self.db.execute("select pages, total, partial from run where id = ?", (run,)).fetchone()
        pages, total, partial = manifest
        if self.completed_pages(run) != set(range(1, pages + 1)):
            raise ValueError("Theapolis archive has missing production pages")
        count, pending = self.db.execute(
            "select count(*), count(*) filter (where ready is null) from item where run = ?", (run,)
        ).fetchone()
        if not count or pending or (not partial and count != total):
            raise ValueError(
                f"Theapolis archive has unexpected coverage: {count}/{total} productions, {pending} pending"
            )
        with self.db:
            if carry_details:
                previous = self.latest()
                if previous is not None:
                    self.db.execute(
                        """update item as target set detail = prior.detail from item as prior
                           where target.run = ? and prior.run = ? and target.id = prior.id
                           and prior.detail is not null and target.detail is null""",
                        (run, previous),
                    )
            self.db.execute("update run set sealed = 1 where id = ?", (run,))

    def latest(self) -> int | None:
        """Return the most recent sealed full snapshot."""
        return self.db.execute("select max(id) from run where sealed = 1 and not partial").fetchone()[0]

    def captured(self, run: int) -> str:
        """Get capture time, rejecting unsealed runs."""
        row = self.db.execute("select captured_at from run where id = ? and sealed = 1", (run,)).fetchone()
        if row is None:
            raise ValueError(f"Theapolis archive run {run} is not sealed")
        return row[0]

    def cached(self, kind: str, since: float) -> dict:
        """Read fresh enrichment results."""
        return {
            key: self._get(value)
            for key, value in self.db.execute(
                "select key, hash from cache where kind = ? and captured_at >= ?", (kind, since)
            )
        }

    def cache_profile(self, key: str, profile: dict | None, captured_at: float) -> None:
        """Stage a profile lookup result, replacing any earlier outcome."""
        kind = "profile" if profile is not None else "no_profile"
        self.db.execute("delete from cache where key = ? and kind = ?", (key, "no_profile" if profile else "profile"))
        self.db.execute(
            """insert into cache values (?, ?, ?, ?) on conflict(kind, key)
               do update set hash = excluded.hash, captured_at = excluded.captured_at""",
            (kind, key, self._put(profile), captured_at),
        )

    def begin_details(self) -> int:
        """Resume or start an API-only credit capture without reloading list pages."""
        if pending := self.pending("api-participations"):
            return pending[0]
        if self.db.execute("select 1 from run where sealed = 0 and not partial limit 1").fetchone():
            raise ValueError("Finish the pending full Theapolis download before starting a separate credit capture")
        prior = self.latest()
        if prior is None:
            raise ValueError("A sealed full list snapshot is required before downloading credits")
        with self.db:
            run = self.db.execute(
                """insert into run (scope, captured_at, pages, total, partial)
                   select 'api-participations', ?, pages, total, partial from run where id = ? returning id""",
                (dt.datetime.now(dt.UTC).isoformat(), prior),
            ).fetchone()[0]
            self.db.execute("insert into page select ?, number, meta from page where run = ?", (run, prior))
            self.db.execute(
                "insert into item select ?, id, page, position, org, country, raw, ready, detail from item where run = ?",
                (run, prior),
            )
        return run

    def cache_profile_name(self, identity: str, name: str, captured_at: float) -> None:
        """Stage the provider's main name separately from name-based profile lookups."""
        self.db.execute(
            """insert into cache values ('profile-name', ?, ?, ?)
               on conflict(kind, key) do update set hash = excluded.hash, captured_at = excluded.captured_at""",
            (identity, self._put(name), captured_at),
        )

    def detail_targets(self, run: int, since: float) -> Iterator[str]:
        """Yield production IDs without fresh API participation evidence."""
        for (identity,) in self.db.execute(
            """select item.id from item left join cache on
                   cache.kind = 'participations-v2' and cache.key = item.id and cache.captured_at >= ?
               where item.run = ? and cache.key is null order by item.page, item.position""",
            (since, run),
        ):
            yield identity

    def put_participations(self, run: int, entries: list[tuple[str, dict, float]]) -> None:
        """Commit one bounded batch of credits and freshness checkpoints together."""
        with self.db:
            values = [(identity, self._put(entry), captured_at) for identity, entry, captured_at in entries]
            self.db.executemany(
                """insert into cache values ('participations-v2', ?, ?, ?)
                   on conflict(kind, key) do update set hash = excluded.hash, captured_at = excluded.captured_at""",
                values,
            )
            self.db.executemany(
                "update item set detail = ? where run = ? and id = ?",
                [(key, run, identity) for identity, key, _ in values],
            )

    def reuse_participations(self, run: int) -> None:
        """Attach previously captured API credits while retaining newer manual feeds."""
        with self.db:
            self.db.execute(
                """update item set detail = cache.hash from cache where item.run = ?
                   and item.id = cache.key and cache.kind = 'participations-v2' and item.detail is null""",
                (run,),
            )

    def add_details(self, entries: Iterator[dict]) -> int:
        """Apply validated detail observations to a copy of the latest run."""
        if self.db.execute("select 1 from run where sealed = 0 and not partial limit 1").fetchone():
            raise ValueError("Finish or restart the pending full Theapolis list before adding credits")
        prior = self.latest()
        if prior is None:
            raise ValueError("A sealed full Theapolis list or legacy snapshot is required before adding credits")
        with self.db:
            run = self.db.execute(
                """insert into run (scope, captured_at, pages, total, partial)
                   select 'details', ?, pages, total, partial from run where id = ? returning id""",
                (dt.datetime.now(dt.UTC).isoformat(), prior),
            ).fetchone()[0]
            self.db.execute("insert into page select ?, number, meta from page where run = ?", (run, prior))
            self.db.execute(
                "insert into item select ?, id, page, position, org, country, raw, ready, detail from item where run = ?",
                (run, prior),
            )
            seen = set()
            for entry in entries:
                identity = source_id(entry["production_id"])
                if identity in seen:
                    raise ValueError(f"Duplicate Theapolis detail production ID: {identity}")
                seen.add(identity)
                cursor = self.db.execute(
                    "update item set detail = ? where run = ? and id = ?", (self._put(entry), run, identity)
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Unknown Theapolis detail production ID: {identity}")
            if not seen:
                raise ValueError("No detail records supplied")
            self.db.execute("update run set sealed = 1 where id = ?", (run,))
        return run

    def items(self, run: int, *, since: int = 0, countries: tuple[str, ...] = ()) -> Iterator[dict]:
        """Decode changed in-scope items only."""
        self.captured(run)
        if since:
            self.captured(since)
            if since > run:
                raise ValueError("Cannot import an older Theapolis archive run")
            if since == run:
                return
        if countries:
            rows = self.db.execute(
                """with eligible as (
                       select run, org from item where run in (?, ?) and org is not null
                       group by run, org having count(*) > 1 and max(country) in (select value from json_each(?))
                   )
                   select new.ready, new.detail from item as new
                   join eligible as current on current.run = new.run and current.org = new.org
                   left join item as old on old.run = ? and old.id = new.id
                       and exists(select 1 from eligible where run = old.run and org = old.org)
                   where new.run = ? and (old.id is null or new.ready != old.ready or new.detail is not old.detail)
                   order by new.page, new.position""",
                (run, since, json.dumps(countries), since, run),
            )
        else:
            rows = self.db.execute(
                """select new.ready, new.detail from item as new
                   left join item as old on old.run = ? and old.id = new.id
                   where new.run = ? and (old.id is null or new.ready != old.ready or new.detail is not old.detail)
                   order by new.page, new.position""",
                (since, run),
            )
        for key, detail in rows:
            item = self._unpack(key)
            if detail is not None:
                observation = self._get(detail)
                item["theapolis_detail"] = observation["theapolis_detail"]
                item["theapolis_detail_status"] = observation["theapolis_detail_status"]
            yield item
