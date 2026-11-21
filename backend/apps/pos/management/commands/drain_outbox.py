from __future__ import annotations

import logging
import signal
import threading
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from django.db import close_old_connections

from apps.pos.outbox_sync import HttpSink, drain_once, queue_stats, requeue_failed

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Drain the Outbox to the cloud: ordered, at-least-once, idempotent on the receiver."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Drain until empty/blocked, exit.")
        parser.add_argument("--interval", type=float, default=None, help="Seconds between polls.")
        parser.add_argument(
            "--requeue-failed", action="store_true", help="Put rejected rows back in the queue."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["requeue_failed"]:
            self.stdout.write(f"Requeued {requeue_failed()} rejected row(s).")
            return
        url = settings.CLOUD_SYNC_URL
        if options["once"]:
            self._run_once(url)
        else:
            self._run_forever(url, options["interval"] or settings.OUTBOX_POLL_SECONDS)

    def _run_once(self, url: str) -> None:
        if not url:
            self.stdout.write("CLOUD_SYNC_URL is not set; cloud sync is disabled.")
            return
        sink = HttpSink(url, settings.CLOUD_API_KEY)
        delivered = 0
        error = ""
        while True:
            report = drain_once(sink)
            delivered += report.sent + report.duplicates
            error = report.transport_error
            if report.claimed == 0 or error or report.rejected:
                break
        suffix = f" Cloud unreachable: {error}." if error else ""
        self.stdout.write(f"Delivered {delivered}. Queue: {queue_stats()}.{suffix}")

    def _run_forever(self, url: str, interval: float) -> None:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        sink = HttpSink(url, settings.CLOUD_API_KEY) if url else None
        if sink is None:
            logger.warning("CLOUD_SYNC_URL is not set; cloud sync is disabled (idling).")
        last_error = ""
        while not stop.is_set():
            delay = interval
            if sink is not None:
                close_old_connections()  # survive a Postgres restart
                try:
                    report = drain_once(sink)
                except Exception:
                    logger.exception("Outbox drain failed")
                else:
                    if report.claimed:
                        logger.info(
                            "Drain: sent=%s duplicates=%s skipped=%s rejected=%s",
                            report.sent,
                            report.duplicates,
                            report.skipped,
                            report.rejected,
                        )
                    if report.transport_error != last_error:
                        last_error = report.transport_error
                        logger.warning("Cloud: %s", last_error or "reachable again")
                    full = report.claimed >= settings.OUTBOX_BATCH_SIZE
                    if full and not report.transport_error and not report.rejected:
                        delay = 0  # more backlog waiting: don't sleep
            stop.wait(delay)
