"""Mock M-PESA records (Step 6): stands in for Safaricom's transaction records on the mock cloud.

In production the cloud would store each till payment Safaricom reports and the terminal would
query that store. The mock lets tests and smoke tests play the customer who paid the till:
POST a transaction, and reconciliation finds it later.

"Not found" is an explicit {"found": false}. Any non-200 response means "cannot tell", so a
disabled or broken endpoint can never be mistaken for "no matching payment".
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .models import MpesaTransaction
from .views import _authorized  # same bearer-key check as the event receiver

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("10000000000")  # fits max_digits=12, decimal_places=2
RECEIPT_RE = re.compile(r"^[A-Z0-9]{10}$")
MAX_RESULTS = 500


def _guard(request: HttpRequest) -> JsonResponse | None:
    if not settings.CLOUD_MOCK_ENABLED:
        raise Http404("Cloud mock is disabled")
    if not _authorized(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return None


def _serialize(record: MpesaTransaction) -> dict[str, str]:
    return {
        "receipt_code": record.receipt_code,
        "amount": f"{record.amount:.2f}",
        "phone_number": record.phone_number,
        "occurred_at": record.occurred_at.isoformat(),
    }


def _parse_amount(raw: Any) -> Decimal | None:
    try:
        amount = Decimal(str(raw))
        if not amount.is_finite() or amount <= 0 or amount >= MAX_AMOUNT:
            return None
        if amount != amount.quantize(CENT):
            return None
    except (InvalidOperation, ValueError):
        return None
    return amount


def _parse_time(raw: Any) -> datetime | None:
    try:
        parsed = parse_datetime(str(raw))
    except ValueError:
        return None
    if parsed is None or timezone.is_naive(parsed):
        return None
    return parsed


def _bad(message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=400)


def _create(request: HttpRequest) -> JsonResponse:
    try:
        body = json.loads(request.body)
        raw_amount = body["amount"]
    except (ValueError, KeyError, TypeError):
        return _bad("expected a JSON object with an amount")
    amount = _parse_amount(raw_amount)
    if amount is None:
        return _bad("amount must be positive with at most 2 decimals")

    code = str(body.get("receipt_code") or "").strip().upper()
    if code and not RECEIPT_RE.match(code):
        return _bad("receipt_code must be 10 letters and numbers")
    if not code:
        code = "Q" + uuid.uuid4().hex[:9].upper()
    phone = str(body.get("phone_number") or "")
    if len(phone) > 15:
        return _bad("phone_number is too long")
    occurred = timezone.now()
    if body.get("occurred_at"):
        parsed = _parse_time(body["occurred_at"])
        if parsed is None:
            return _bad("occurred_at must be an ISO datetime with a timezone")
        occurred = parsed

    try:
        with transaction.atomic():
            record = MpesaTransaction.objects.create(
                receipt_code=code, amount=amount, phone_number=phone, occurred_at=occurred
            )
    except IntegrityError:
        return JsonResponse({"error": "receipt code already recorded"}, status=409)
    return JsonResponse({"transaction": _serialize(record)}, status=201)


def _list(request: HttpRequest) -> JsonResponse:
    start = _parse_time(request.GET.get("start"))
    end = _parse_time(request.GET.get("end"))
    if start is None or end is None:
        return _bad("start and end must be ISO datetimes with a timezone")
    records = MpesaTransaction.objects.filter(occurred_at__range=(start, end)).order_by(
        "occurred_at"
    )[:MAX_RESULTS]
    return JsonResponse({"transactions": [_serialize(r) for r in records]})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def records_collection(request: HttpRequest) -> JsonResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    return _create(request) if request.method == "POST" else _list(request)


@csrf_exempt
@require_GET
def records_detail(request: HttpRequest, receipt_code: str) -> JsonResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    record = MpesaTransaction.objects.filter(receipt_code=receipt_code.upper()).first()
    if record is None:
        return JsonResponse({"found": False})
    return JsonResponse({"found": True, "transaction": _serialize(record)})
