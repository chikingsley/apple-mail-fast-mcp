"""Standalone near-real-time Junk enforcement without replacing the live MCP."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import signal
import threading
from contextlib import closing

from .index import LEDGER_PATH, JunkLedger, JunkSupervisor, MailIndex
from .mail import Mail

DEFAULT_INTERVAL_SECONDS = 30
MINIMUM_INTERVAL_SECONDS = 15
LOCK_PATH = LEDGER_PATH.parent / "cleaner.lock"
logger = logging.getLogger("apple_mail.cleaner")


def run_once() -> dict[str, object]:
    """Run one fully recorded inventory and approved deletion cycle."""
    return JunkSupervisor(MailIndex(), Mail(), JunkLedger()).run()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Seconds between cycles (minimum 15)",
    )
    return parser.parse_args()


def main() -> int:
    """Run one cleaner instance, serializing all cycles with an owner-only lock."""
    arguments = _arguments()
    if arguments.interval < MINIMUM_INTERVAL_SECONDS:
        raise ValueError("Cleaner interval must be at least 15 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOCK_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with closing(LOCK_PATH.open("a+", encoding="utf-8")) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error("Another cleaner instance already owns %s", LOCK_PATH)
            return 1
        while not stop.is_set():
            try:
                logger.info("%s", json.dumps(run_once(), sort_keys=True))
            except Exception:
                logger.exception("Junk cleaner cycle failed")
            if arguments.once or stop.wait(arguments.interval):
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
