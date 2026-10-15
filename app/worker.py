"""Entry point for the separate worker process:

    python -m app.worker

Run it next to the API with the same environment (.env) and set RUN_MODE=worker on the
API so it only queues runs. Start several copies to solve more trips at once.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import get_settings
from .db import build_engine, build_sessionmaker
from .services.worker import Worker


async def main() -> None:
    settings = get_settings()
    engine = build_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        disable_statement_cache=settings.db_disable_statement_cache,
    )
    worker = Worker(build_sessionmaker(engine), settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C is handled in run() below
            pass
    try:
        await worker.run_forever(stop)
    finally:
        await engine.dispose()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Windows Ctrl+C
        pass


if __name__ == "__main__":
    run()
