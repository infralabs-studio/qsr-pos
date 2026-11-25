from __future__ import annotations

import logging
import signal
import threading
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from django.db import close_old_connections

from apps.pos.providers.mpesa_records import HttpTransactionSource
from apps.pos.reconciliation import reconcile_pending

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Reconcile manual M-PESA payments against the M-PESA records. Idempotent."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
        parser.add_argument("--interval", type=float, default=None, help="Seconds between passes.")

    def handle(self, *args: Any, **options: Any) -> None:
        url = settings.MPESA_RECORDS_URL
        if options["once"]:
            self._run_once(url)
        else:
            self._run_forever(url, options["interval"] or settings.RECONCILE_POLL_SECONDS)

    def _run_once(self, url: str) -> None:
        if not url:
            self.stdout.write("MPESA_RECORDS_URL is not set; reconciliation is disabled.")
            return
        report = reconcile_pending(HttpTransactionSource(url, settings.CLOUD_API_KEY))
        suffix = " M-PESA records unreachable; nothing changed." if report.unavailable else ""
        self.stdout.write(
            f"Checked {report.checked}: confirmed {report.confirmed}, "
            f"needs review {report.needs_review}, still pending {report.still_pending}.{suffix}"
        )

    def _run_forever(self, url: str, interval: float) -> None:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        source = HttpTransactionSource(url, settings.CLOUD_API_KEY) if url else None
        if source is None:
            logger.warning("MPESA_RECORDS_URL is not set; reconciliation is disabled (idling).")
        was_unavailable = False
        while not stop.is_set():
            if source is not None:
                close_old_connections()  # survive a Postgres restart
                try:
                    report = reconcile_pending(source)
                except Exception:
                    logger.exception("Reconciliation pass failed")
                else:
                    if report.unavailable != was_unavailable:
                        was_unavailable = report.unavailable
                        logger.warning(
                            "M-PESA records %s",
                            (
                                "unreachable: payments stay pending"
                                if was_unavailable
                                else "reachable"
                            ),
                        )
                    if report.confirmed or report.needs_review:
                        logger.info(
                            "Reconciled: confirmed=%s needs_review=%s still_pending=%s",
                            report.confirmed,
                            report.needs_review,
                            report.still_pending,
                        )
            stop.wait(interval)
