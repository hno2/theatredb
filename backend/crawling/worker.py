"""Run with python -m backend.crawling.worker; initialize queue schema explicitly."""

import argparse
import asyncio
import logging
import uuid

from backend.crawling.queue import TASK_NAME, app, worker_app
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
    """Create missing tables/schema only on request, otherwise start the worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-schema", action="store_true", help="Create fresh Procrastinate schema once")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    init_db()
    if args.init_schema:
        with app().open():
            app().schema_manager.apply_schema()
    else:
        asyncio.run(_worker())


if __name__ == "__main__":
    main()
