#!/usr/bin/env python3
"""Quick script to import theapolis data into the database."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from backend.ingestion.theapolis import download, import_all


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Download and import theapolis.de data")
    parser.add_argument(
        "command",
        choices=["download", "import"],
        help="download: fetch latest from API | import: load from productions_new.json",
    )
    parser.add_argument("--json", default="productions_new.json", help="Path to JSON file (for import)")

    args = parser.parse_args()

    if args.command == "download":
        print("Downloading theapolis data...")
        await download()
        print("Download complete. Run: python import_theapolis.py import")
    elif args.command == "import":
        print(f"Importing from {args.json}...")
        import_all(Path(args.json))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_theapolis.py [download|import]")
        print("  download — fetch latest productions from theapolis.de API")
        print("  import   — import from productions_new.json into database")
        sys.exit(1)

    asyncio.run(main())
