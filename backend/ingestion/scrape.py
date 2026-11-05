"""CLI for bounded scraping, schema validation and single-page change checks."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import yaml

from backend.ingestion.configurable import ConfigurableScraper, load_config
from backend.ingestion.snapshots import check_page, save_scrape, scrape_diff, write_json
from backend.ingestion.staging import validate_records, write_schema


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the public ingestion CLI; a full scrape always requires --all."""
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    schema = subcommands.add_parser("schema", help="Generate the staging JSON Schema")
    schema.add_argument("--output", type=Path, default=Path("backend/ingestion/staging.schema.json"))
    validate = subcommands.add_parser("validate", help="Validate staged JSON without accessing a database")
    validate.add_argument("json", type=Path)
    diff = subcommands.add_parser("diff", help="Compare two staged production snapshots offline")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    for command in ("scrape", "page", "check"):
        sub = subcommands.add_parser(command)
        sub.add_argument("--config", required=True, help="Bundled venue name or YAML/JSON config path")
        if command == "scrape":
            scope = sub.add_mutually_exclusive_group()
            scope.add_argument("--limit", type=int, default=1, help="Maximum productions (default: 1)")
            scope.add_argument("--all", action="store_true", help="Explicitly request all configured listing pages")
            sub.add_argument("--no-people", action="store_true", help="Do not fetch linked biographies")
            sub.add_argument("--output-dir", type=Path, default=Path("data/staged"))
        else:
            sub.add_argument("--url", required=True)
            sub.add_argument("--kind", choices=("production", "person"), default="production")
            if command == "page":
                sub.add_argument("--output", type=Path, help="Write parsed JSON; otherwise print it")
            else:
                sub.add_argument("--state-dir", type=Path, default=Path("data/scrape-checks"))
                sub.add_argument("--accept", action="store_true", help="Advance the baseline after showing differences")
                sub.add_argument("--data-only", action="store_true", help="Exit 1 only for extracted-data changes")
    return parser.parse_args(argv)


def _read_records(path: Path) -> list[dict]:
    return validate_records(json.loads(path.read_text(encoding="utf-8")))


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    scraper = ConfigurableScraper(config)
    if args.command == "scrape":
        limit = None if args.all else args.limit
        records = await scraper.scrape(limit=limit, enrich_people=not args.no_people)
        path, report = save_scrape(records, config, args.output_dir, limit=limit, enrich_people=not args.no_people)
        print(report)
        print(f"Saved {len(records)} productions to {path}")
        return 0
    url = scraper.check_url(args.url)
    async with scraper.session() as session:
        html = await scraper.fetch(session, url)
    record = scraper.parse_page(html, url, args.kind)
    if args.command == "page":
        data = [record] if args.kind == "production" else record
        if args.output:
            write_json(args.output, data)
            print(f"Saved {args.kind} to {args.output}")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    code, report = check_page(
        html,
        record,
        url,
        args.kind,
        config,
        args.state_dir,
        accept=args.accept,
        data_only=args.data_only,
    )
    print(report)
    return code


def _dispatch(args: argparse.Namespace) -> int:
    match args.command:
        case "schema":
            write_schema(args.output)
            print(f"Schema written to {args.output}")
            return 0
        case "validate":
            records = _read_records(args.json)
            print(f"Valid staging JSON: {len(records)} productions")
            return 0
        case "diff":
            print(scrape_diff(_read_records(args.before), _read_records(args.after)))
            return 0
        case _:
            return asyncio.run(_run(args))


def main(argv: list[str] | None = None) -> int:
    """Run one command; report validation, HTTP and filesystem errors explicitly."""
    args = parse_args(argv)
    try:
        return _dispatch(args)
    except (ValueError, TypeError, OSError, TimeoutError, aiohttp.ClientError, yaml.YAMLError) as error:
        print(f"Ingestion error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
