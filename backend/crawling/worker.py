"""Run with python -m backend.crawling.worker; initialize missing schema on startup."""

import argparse
import asyncio
import logging
import uuid

from backend.crawling.queue import TASK_NAME, worker_app
from backend.crawling.runner import execute
from backend.user_management import init_db


@worker_app().task(name=TASK_NAME)
async def crawl(run_id: str) -> None:
    """Execute one source-owned crawl; retries happen at the HTTP layer."""
    await execute(uuid.UUID(run_id))


async def _worker() -> None:
    async with worker_app().open_async():
        await worker_app().run_worker_async(concurrency=2)


def main() -> None:
    """Ensure database/queue tables exist, then optionally start the worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-schema", action="store_true", help="Ensure missing schema exists, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    init_db()
    if not args.init_schema:
        asyncio.run(_worker())


if __name__ == "__main__":
    main()
