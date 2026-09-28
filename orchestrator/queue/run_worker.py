"""Run the background worker: ``python -m orchestrator.queue.run_worker``.

Configuration: see orchestrator/wiring.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from orchestrator.queue.worker import Worker
from orchestrator.wiring import Settings, build


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    c = build(Settings.from_env())
    worker = Worker(
        queue=c.queue,
        store=c.store,
        translator=c.translator,
        model_format=c.fmt,
        quota=c.quota,
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
    )
    asyncio.run(worker.run_forever())


if __name__ == "__main__":
    main()
