"""Mock cloud receiver. Stands in for the real cloud ingest during the offline-thesis spike.

Idempotent on idempotency_key. Events in a batch are processed in order; after the first
invalid event the rest are reported as skipped, so the sender never sees a later event stored
while an earlier one was not.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from typing import Any

from django.conf import settings
from django.http import Http404, HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.pos.outbox_sync import DeliveryStatus

from .models import CloudEvent

MAX_BATCH = 500
MAX_LENGTHS = {
    "idempotency_key": 128,
    "aggregate_type": 64,
    "aggregate_id": 64,
    "event_type": 64,
}


def _authorized(request: HttpRequest) -> bool:
    expected = settings.CLOUD_API_KEY
    header = request.headers.get("Authorization", "")
    if not expected or not header.startswith("Bearer "):
        return False  # an unset key must never mean "open"
    return hmac.compare_digest(header[len("Bearer ") :], expected)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_datetime(value)
    except ValueError:
        return None
    if parsed is None or timezone.is_naive(parsed):
        return None
    return parsed


def _validate(event: Any) -> tuple[datetime | None, str | None]:
    if not isinstance(event, dict):
        return None, "event must be an object"
    for name, limit in MAX_LENGTHS.items():
        value = event.get(name)
        if not isinstance(value, str) or not value or len(value) > limit:
            return None, f"{name} must be a non-empty string up to {limit} characters"
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        return None, "sequence must be a non-negative integer"
    if not isinstance(event.get("payload"), dict):
        return None, "payload must be an object"
    created_at = _parse_time(event.get("created_at"))
    if created_at is None:
        return None, "created_at must be a timezone-aware ISO datetime"
    return created_at, None


@csrf_exempt
@require_POST
def ingest(request: HttpRequest) -> JsonResponse:
    if not settings.CLOUD_MOCK_ENABLED:
        raise Http404("Cloud mock is disabled")
    if not _authorized(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        events = json.loads(request.body)["events"]
        if not isinstance(events, list) or len(events) > MAX_BATCH:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": 'expected {"events": [...]}'}, status=400)

    results: list[dict[str, str]] = []
    halted = False
    for event in events:
        key = str(event.get("idempotency_key", "")) if isinstance(event, dict) else ""
        if halted:
            detail = "An earlier event in the batch was rejected"
            results.append(
                {
                    "idempotency_key": key,
                    "status": DeliveryStatus.SKIPPED.value,
                    "detail": detail,
                }
            )
            continue
        created_at, error = _validate(event)
        if error is not None or created_at is None:
            halted = True
            results.append(
                {
                    "idempotency_key": key,
                    "status": DeliveryStatus.REJECTED.value,
                    "detail": error or "Invalid event",
                }
            )
            continue
        _, created = CloudEvent.objects.get_or_create(
            idempotency_key=event["idempotency_key"],
            defaults={
                "sequence": event["sequence"],
                "aggregate_type": event["aggregate_type"],
                "aggregate_id": event["aggregate_id"],
                "event_type": event["event_type"],
                "payload": event["payload"],
                "terminal_created_at": created_at,
            },
        )
        status = DeliveryStatus.ACCEPTED if created else DeliveryStatus.DUPLICATE
        results.append({"idempotency_key": key, "status": status.value, "detail": ""})
    return JsonResponse({"results": results})
