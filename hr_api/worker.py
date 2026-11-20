"""Persistent PostgreSQL queue consumer; one active consumer per database."""
from __future__ import annotations

import logging
import signal
from threading import Event

from sqlalchemy import text

from storage.database import make_engine, make_session_factory
from .service import HRService


STOP = Event()
LOCK = "hr-platform-analysis-worker"
logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    engine = make_engine()
    service = HRService(make_session_factory(engine))
    try:
        while not STOP.is_set():
            try:
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
                    acquired = lock.scalar(text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": LOCK})
                    if not acquired:
                        STOP.wait(2)
                        continue
                    try:
                        service.recover_processing_tasks()
                        logger.info("HR worker ready")
                        while not STOP.is_set():
                            identifier = service.next_task_id()
                            if identifier is None:
                                STOP.wait(0.5)
                                continue
                            service.run_task(identifier)
                            logger.info("Finished task %s", identifier)
                    finally:
                        lock.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": LOCK})
            except Exception as error:
                # Exceptions can contain provider payloads or DB parameters.
                logger.error("Worker error: %s", type(error).__name__)
                STOP.wait(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
