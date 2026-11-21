"""Outbox drain: ordered, at-least-once delivery to the cloud (offline thesis, Step 4).

Guarantees:
- Order: rows are sent strictly by sequence. If the head of the queue cannot be sent
    (backoff, another worker's live lease, a rejected row), nothing behind it is sent.
- At-least-once: a row is marked sent only after the receiver acknowledged it. A crash at
    any point means the row is sent again, never lost.
- Exactly-once effect: the receiver dedupes on idempotency_key, so re-sends are harmless.
- The till never waits on this module: no network call happens inside a DB transaction.
- Being unable to reach the cloud is backoff, not failure: it never dead-letters a row.
- A row the receiver rejects goes to FAILED and halts the queue until requeue_failed().
Only rows created through Outbox.enqueue() (which assigns a sequence) are drained.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import Outbox

logger = logging.getLogger(__name__)

MAX_RECORDED_ATTEMPTS = 1000  # keeps the counter inside its column during very long outages


class DeliveryStatus(StrEnum):
    ACCEPTED = "accepted"  # stored for the first time
    DUPLICATE = "duplicate"  # already stored: a harmless re-send
    SKIPPED = "skipped"  # not processed because an earlier event in the batch was rejected
    REJECTED = "rejected"  # permanently invalid: needs an operator


@dataclass(frozen=True)
class SinkResult:
    idempotency_key: str
    status: DeliveryStatus
    detail: str = ""


class SinkUnavailable(Exception):
    """The cloud cannot be reached (or answered nonsense). Always retried, never fatal."""


class OutboxSink(Protocol):
    def deliver(self, events: Sequence[dict[str, Any]]) -> list[SinkResult]: ...


@dataclass
class DrainReport:
    claimed: int = 0
    sent: int = 0
    duplicates: int = 0
    skipped: int = 0
    rejected: int = 0
    transport_error: str = ""
    blocked: bool = False  # unsent rows exist but the head of the queue is not claimable


# --------------------------------------------------------------------------- transport


def parse_results(data: Any) -> list[SinkResult]:
    try:
        return [
            SinkResult(
                idempotency_key=str(item["idempotency_key"]),
                status=DeliveryStatus(item["status"]),
                detail=str(item.get("detail", "")),
            )
            for item in data["results"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise SinkUnavailable("Malformed response from cloud") from exc


class HttpSink:
    def __init__(self, url: str, api_key: str, timeout: float = 10.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("CLOUD_SYNC_URL must start with http:// or https://")
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def deliver(self, events: Sequence[dict[str, Any]]) -> list[SinkResult]:
        body = json.dumps({"events": list(events)}).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SinkUnavailable(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SinkUnavailable(str(exc)) from exc
        return parse_results(data)


def serialize(row: Outbox) -> dict[str, Any]:
    return {
        "idempotency_key": row.idempotency_key,
        "sequence": row.sequence,
        "aggregate_type": row.aggregate_type,
        "aggregate_id": row.aggregate_id,
        "event_type": row.event_type,
        "payload": row.payload,
        "created_at": row.created_at.isoformat(),
    }


# --------------------------------------------------------------------------- queue logic


def backoff_delay(attempts: int) -> timedelta:
    exponent = min(max(attempts - 1, 0), 10)
    seconds = settings.OUTBOX_BACKOFF_BASE_SECONDS * 2**exponent
    return timedelta(seconds=min(seconds, settings.OUTBOX_BACKOFF_CAP_SECONDS))


def _is_claimable(row: Outbox, now: datetime, lease: timedelta) -> bool:
    if row.status == Outbox.Status.PENDING:
        return row.available_at <= now
    if row.status == Outbox.Status.PROCESSING:
        return row.locked_at is None or row.locked_at + lease <= now  # stale lease
    return False  # FAILED needs an operator: requeue_failed()


def claim_batch(now: datetime, batch_size: int) -> list[Outbox]:
    """Lock the head of the queue and mark its claimable prefix as processing."""
    lease = timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
    with transaction.atomic():
        candidates = list(
            Outbox.objects.select_for_update()
            .filter(sequence__isnull=False)
            .exclude(status=Outbox.Status.SENT)
            .order_by("sequence")[:batch_size]
        )
        claimed: list[Outbox] = []
        for row in candidates:
            if not _is_claimable(row, now, lease):
                break  # head-of-line: never let a later row overtake an earlier one
            claimed.append(row)
        for row in claimed:
            row.status = Outbox.Status.PROCESSING
            row.locked_at = now
            row.attempts = min(row.attempts + 1, MAX_RECORDED_ATTEMPTS)
            row.save(update_fields=["status", "locked_at", "attempts", "updated_at"])
    return claimed


def _update_claimed(row: Outbox, **fields: Any) -> int:
    """Update a row only if we still hold the lease it was claimed under."""
    updated = Outbox.objects.filter(
        pk=row.pk, status=Outbox.Status.PROCESSING, locked_at=row.locked_at
    ).update(updated_at=timezone.now(), **fields)
    if not updated:
        logger.warning("Lost the lease on outbox row %s; leaving it to the new holder", row.pk)
    return updated


def _release_transient(rows: Sequence[Outbox], message: str, now: datetime) -> None:
    with transaction.atomic():
        for row in rows:
            _update_claimed(
                row,
                status=Outbox.Status.PENDING,
                locked_at=None,
                available_at=now + backoff_delay(row.attempts),
                last_error=message[:500],
            )


def _apply_results(
    rows: Sequence[Outbox], results: Sequence[SinkResult], report: DrainReport
) -> None:
    by_key = {result.idempotency_key: result for result in results}
    with transaction.atomic():
        for row in rows:
            result = by_key.get(row.idempotency_key)
            status = result.status if result is not None else DeliveryStatus.SKIPPED
            detail = result.detail if result is not None else ""
            if status in (DeliveryStatus.ACCEPTED, DeliveryStatus.DUPLICATE):
                held = _update_claimed(
                    row,
                    status=Outbox.Status.SENT,
                    processed_at=timezone.now(),
                    last_error="",
                    locked_at=None,
                )
                if held and status == DeliveryStatus.ACCEPTED:
                    report.sent += 1
                elif held:
                    report.duplicates += 1
            elif status == DeliveryStatus.REJECTED:
                failed = _update_claimed(
                    row,
                    status=Outbox.Status.FAILED,
                    last_error=(detail or "Rejected by cloud")[:500],
                    locked_at=None,
                )
                report.rejected += 1 if failed else 0
            else:  # skipped: put it back, and don't count this as an attempt
                back = _update_claimed(
                    row,
                    status=Outbox.Status.PENDING,
                    locked_at=None,
                    attempts=max(row.attempts - 1, 0),
                )
                report.skipped += 1 if back else 0


def drain_once(
    sink: OutboxSink, *, now: datetime | None = None, batch_size: int | None = None
) -> DrainReport:
    """Claim a batch, send it, record the outcome. Never holds a transaction while sending."""
    now = now or timezone.now()
    report = DrainReport()
    rows = claim_batch(now, batch_size or settings.OUTBOX_BATCH_SIZE)
    report.claimed = len(rows)
    if not rows:
        report.blocked = (
            Outbox.objects.filter(sequence__isnull=False)
            .exclude(status=Outbox.Status.SENT)
            .exists()
        )
        return report
    try:
        results = sink.deliver([serialize(row) for row in rows])
    except SinkUnavailable as exc:
        _release_transient(rows, str(exc), now)
        report.transport_error = str(exc)
        return report
    except Exception as exc:
        _release_transient(rows, f"Unexpected error: {exc!r}", now)
        raise
    _apply_results(rows, results, report)
    return report


def requeue_failed() -> int:
    """Operator action: put rejected rows back in the queue after the cause is fixed."""
    now = timezone.now()
    return Outbox.objects.filter(status=Outbox.Status.FAILED).update(
        status=Outbox.Status.PENDING, available_at=now, locked_at=None, updated_at=now
    )


def queue_stats() -> dict[str, int]:
    """Counts of unsent rows. This is what the 'N records waiting to sync' UI will read."""
    stats: dict[str, int] = (
        Outbox.objects.filter(sequence__isnull=False)
        .exclude(status=Outbox.Status.SENT)
        .aggregate(
            pending=Count("pk", filter=Q(status=Outbox.Status.PENDING)),
            processing=Count("pk", filter=Q(status=Outbox.Status.PROCESSING)),
            failed=Count("pk", filter=Q(status=Outbox.Status.FAILED)),
        )
    )
    return stats
