"""Manual M-PESA entry (Step 5): the customer paid the till from their own phone and the
cashier types the confirmation code. Used when the terminal is offline, or after an STK timeout.

The order goes to the kitchen immediately as paid_pending_verification. Reconciliation
(reconciliation.py) confirms it later against Safaricom's records.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User

from .models import Order, Outbox, Payment, PrintJob
from .providers.base import normalize_msisdn
from .services import _audit, _order_snapshot, _payment_snapshot

CODE_RE = re.compile(r"^[A-Z0-9]{10}$")

# A payment in one of these states still has a claim on its M-PESA code.
LIVE_PAYMENT_STATUSES = [
    Payment.Status.CONFIRMED.value,
    Payment.Status.PENDING_VERIFICATION.value,
    Payment.Status.MANAGER_REVIEW.value,
]


def order_label(order: Order) -> str:
    return f"{'B-' if order.is_backfilled else '#'}{order.order_number}"


def normalize_receipt_code(raw: str) -> str:
    code = re.sub(r"\s+", "", raw).upper()
    if not CODE_RE.match(code):
        raise ValidationError("M-PESA codes are 10 letters and numbers, like QGH7XXXXXX.")
    return code


def record_manual_mpesa_payment(
    *,
    order: Order,
    receipt_code: str,
    amount: Decimal,
    recorded_by: User,
    idempotency_key: uuid.UUID,
    phone_number: str = "",
    occurred_at: datetime | None = None,
) -> tuple[Payment, bool]:
    """Record a payment the customer made straight to the till. Returns (payment, created).

    Replays with the same idempotency_key return the original payment and change nothing.
    """
    code = normalize_receipt_code(receipt_code)
    msisdn = normalize_msisdn(phone_number) if phone_number.strip() else ""

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)

        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.order_id != locked.pk:
                raise ValidationError("This payment key belongs to a different order.")
            return existing, False

        if locked.status != Order.Status.AWAITING_PAYMENT:
            raise ValidationError("This order is not awaiting payment.")
        if amount != locked.total:
            raise ValidationError(f"Amount must equal the order total (KES {locked.total}).")

        clash = (
            Payment.objects.select_related("order")
            .filter(mpesa_receipt_code=code, status__in=LIVE_PAYMENT_STATUSES)
            .first()
        )
        if clash is not None:
            raise ValidationError(
                f"This code was recorded on order {order_label(clash.order)}. "
                "Check with the customer."
            )

        payment = Payment.objects.create(
            order=locked,
            method=Payment.Method.MPESA_MANUAL,
            status=Payment.Status.PENDING_VERIFICATION,
            amount=locked.total,
            phone_number=msisdn,
            mpesa_receipt_code=code,
            recorded_by=recorded_by,
            occurred_at=occurred_at or timezone.now(),
            idempotency_key=idempotency_key,
        )
        locked.status = Order.Status.PAID_PENDING_VERIFICATION
        locked.save(update_fields=["status", "updated_at"])

        # The kitchen proceeds now. A backfilled order was served long ago: no ticket.
        if not locked.is_backfilled:
            PrintJob.objects.get_or_create(
                order=locked,
                job_type=PrintJob.JobType.KITCHEN_TICKET,
                is_reprint=False,
                defaults={"payload": {"order_number": locked.order_number}},
            )

        Outbox.enqueue(
            aggregate_type="order",
            aggregate_id=str(locked.pk),
            event_type="order.paid_pending_verification",
            payload=_order_snapshot(locked),
            idempotency_key=f"order:{locked.pk}:pending_verification",
        )
        Outbox.enqueue(
            aggregate_type="payment",
            aggregate_id=str(payment.pk),
            event_type="payment.pending_verification",
            payload=_payment_snapshot(payment),
            idempotency_key=f"payment:{payment.pk}:pending_verification",
        )
        _audit(
            event_type="payment.manual_mpesa_recorded",
            actor=recorded_by,
            target_type="payment",
            target_id=payment.pk,
            new_state=_payment_snapshot(payment),
        )
        return payment, True
