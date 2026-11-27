"""Payment state machine runner, provider-event handling and the STK start/timeout flow."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User

from .models import Order, Payment, PaymentAttempt
from .payment_states import PaymentEvent, is_already_applied, target_status
from .providers.base import PaymentProvider, ProviderEvent, ProviderUnavailable, normalize_msisdn
from .services import _audit, _payment_snapshot, finalize_paid_order

logger = logging.getLogger(__name__)


class TransitionStatus(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"  # already applied: an idempotent replay
    REJECTED = "rejected"  # illegal transition: refused and logged


@dataclass(frozen=True)
class TransitionResult:
    status: TransitionStatus
    payment: Payment
    from_status: str
    to_status: str
    detail: str = ""


_ATTEMPT_STATUS: dict[PaymentEvent, str] = {
    PaymentEvent.SUCCEEDED: PaymentAttempt.Status.SUCCEEDED.value,
    PaymentEvent.FAILED: PaymentAttempt.Status.FAILED.value,
    PaymentEvent.CANCELLED: PaymentAttempt.Status.CANCELLED.value,
    PaymentEvent.TIMED_OUT: PaymentAttempt.Status.TIMED_OUT.value,
}


def apply_event(
    payment: Payment,
    event: PaymentEvent,
    *,
    actor: User | None = None,
    receipt_code: str = "",
    review_reason: str = "",
    detail: str = "",
) -> TransitionResult:
    """Apply one event to a payment inside a transaction.

    Illegal transitions are refused and audited (never raised, so the audit row commits).
    Replays of an already-applied event change nothing.
    """
    with transaction.atomic():
        # Lock order first, then payment: the same order every other writer uses.
        order_id = Payment.objects.values_list("order_id", flat=True).get(pk=payment.pk)
        order = Order.objects.select_for_update().get(pk=order_id)
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        current = locked.status
        target = target_status(current, event)

        if target is None:
            if is_already_applied(current, event):
                return TransitionResult(TransitionStatus.NOOP, locked, current, current)
            message = f"Illegal payment transition: {current} cannot handle {event.value}"
            logger.warning("%s (payment=%s)", message, locked.pk)
            _audit(
                event_type="payment.transition_rejected",
                actor=actor,
                target_type="payment",
                target_id=locked.pk,
                new_state={"status": current, "event": event.value},
                reason=message,
            )
            return TransitionResult(TransitionStatus.REJECTED, locked, current, current, message)

        duplicate_of: Payment | None = None
        if event == PaymentEvent.SUCCEEDED and target == Payment.Status.CONFIRMED.value:
            # Money arrived, but the order may already be settled another way (cash, manual).
            duplicate_of = (
                Payment.objects.filter(
                    order=order,
                    status__in=[
                        Payment.Status.CONFIRMED.value,
                        Payment.Status.PENDING_VERIFICATION.value,
                    ],
                )
                .exclude(pk=locked.pk)
                .first()
            )
            if duplicate_of is not None or order.status != Order.Status.AWAITING_PAYMENT:
                target = Payment.Status.MANAGER_REVIEW.value
                review_reason = (
                    Payment.ReviewReason.CROSS_METHOD_DUPLICATE.value
                    if duplicate_of is not None
                    else Payment.ReviewReason.OTHER.value
                )

        now = timezone.now()
        locked.status = target
        if receipt_code:
            locked.mpesa_receipt_code = receipt_code
        if target == Payment.Status.CONFIRMED.value:
            locked.confirmed_at = now
            locked.confirmed_by = actor
        if target == Payment.Status.MANAGER_REVIEW.value:
            locked.review_reason = review_reason
            locked.duplicate_of = duplicate_of
        locked.save()

        if target == Payment.Status.CONFIRMED.value and order.status in (
            Order.Status.AWAITING_PAYMENT,
            Order.Status.PAID_PENDING_VERIFICATION,
        ):
            finalize_paid_order(order, locked, now)

        _audit(
            event_type="payment.transition",
            actor=actor,
            target_type="payment",
            target_id=locked.pk,
            previous_state={"status": current},
            new_state={
                "status": target,
                "event": event.value,
                "mpesa_receipt_code": locked.mpesa_receipt_code,
                "review_reason": locked.review_reason,
            },
            reason=detail,
        )
        return TransitionResult(TransitionStatus.APPLIED, locked, current, target, detail)


def _callback_mismatch(event: ProviderEvent, attempt: PaymentAttempt) -> str:
    """Why a success callback cannot be trusted as-is ('' when it can)."""
    if event.event != PaymentEvent.SUCCEEDED:
        return ""
    if event.amount is not None and event.amount != attempt.amount:
        return f"Paid KES {event.amount} but the prompt was for KES {attempt.amount}."
    return ""


def handle_provider_event(event: ProviderEvent) -> TransitionResult | None:
    """Apply a provider callback or status-query result.

    Returns None for an unknown request id. A second delivery of the same final result only
    bumps callback_count and changes nothing else. A contradictory success after a failure
    escalates to manager review, and a success whose amount does not match the prompt is
    never confirmed automatically.
    """
    if event.event not in _ATTEMPT_STATUS:
        raise ValueError(f"{event.event.value} is not a provider event")
    if event.result_code is None:
        raise ValueError("A provider event needs a result code")
    if not event.checkout_request_id:
        return None

    with transaction.atomic():
        attempt = (
            PaymentAttempt.objects.select_for_update()
            .filter(checkout_request_id=event.checkout_request_id)
            .first()
        )
        if attempt is None:
            logger.warning("Provider event for unknown request %s", event.checkout_request_id)
            return None

        payment = attempt.payment
        attempt.callback_count += 1
        contradictory_success = (
            attempt.result_code is not None
            and attempt.result_code != 0
            and event.event == PaymentEvent.SUCCEEDED
        )
        if attempt.result_code is not None and not contradictory_success:
            attempt.save(update_fields=["callback_count", "updated_at"])
            return TransitionResult(
                TransitionStatus.NOOP, payment, payment.status, payment.status, "Duplicate callback"
            )

        mismatch = _callback_mismatch(event, attempt)
        if contradictory_success:
            attempt.save(update_fields=["callback_count", "updated_at"])
        else:
            attempt.result_code = event.result_code
            attempt.result_desc = event.result_desc
            attempt.mpesa_receipt_code = event.receipt_code
            attempt.callback_payload = event.raw
            attempt.status = _ATTEMPT_STATUS[event.event]
            attempt.resolved_at = timezone.now()
            attempt.save()

        if mismatch:
            return apply_event(
                payment,
                PaymentEvent.FLAGGED_FOR_REVIEW,
                receipt_code=event.receipt_code,
                review_reason=Payment.ReviewReason.OTHER.value,
                detail=mismatch,
            )
        return apply_event(
            payment,
            event.event,
            receipt_code=event.receipt_code,
            review_reason=(Payment.ReviewReason.OTHER.value if contradictory_success else ""),
            detail=(
                "Success callback arrived after a failed attempt"
                if contradictory_success
                else event.result_desc
            ),
        )


def time_out_payment(payment: Payment) -> TransitionResult:
    """Local timeout elapsed with no answer. The prompt may still succeed later."""
    with transaction.atomic():
        result = apply_event(payment, PaymentEvent.TIMED_OUT, detail="No response before timeout")
        if result.status == TransitionStatus.APPLIED:
            PaymentAttempt.objects.filter(
                payment=payment,
                status__in=[
                    PaymentAttempt.Status.INITIATED.value,
                    PaymentAttempt.Status.PENDING.value,
                ],
            ).update(status=PaymentAttempt.Status.TIMED_OUT.value, updated_at=timezone.now())
        return result


def start_stk_payment(
    *,
    provider: PaymentProvider,
    order: Order,
    phone_number: str,
    recorded_by: User,
    idempotency_key: uuid.UUID,
) -> tuple[Payment, bool]:
    """Create a pending STK payment and send the prompt. Returns (payment, created).

    Phase 1 commits the pending payment and attempt. Phase 2 calls the provider with no
    transaction open, so a network call never holds row locks. Phase 3 records the provider's
    ids. Replays with the same key return the original payment and send nothing.
    """
    msisdn = normalize_msisdn(phone_number)  # raises ValidationError before anything is written

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.order_id != locked.pk:
                raise ValidationError("This payment key belongs to a different order.")
            return existing, False
        if locked.status != Order.Status.AWAITING_PAYMENT:
            raise ValidationError("This order is not awaiting payment.")
        payment = Payment.objects.create(
            order=locked,
            method=provider.method,
            status=Payment.Status.PENDING,
            amount=locked.total,
            phone_number=msisdn,
            recorded_by=recorded_by,
            idempotency_key=idempotency_key,
        )
        attempt = PaymentAttempt.objects.create(
            payment=payment,
            attempt_number=1,
            status=PaymentAttempt.Status.INITIATED,
            phone_number=msisdn,
            amount=payment.amount,
        )
        _audit(
            event_type="payment.stk_requested",
            actor=recorded_by,
            target_type="payment",
            target_id=payment.pk,
            new_state=_payment_snapshot(payment),
        )

    try:
        result = provider.initiate(payment, phone_number=msisdn)
    except ProviderUnavailable:
        with transaction.atomic():
            apply_event(payment, PaymentEvent.FAILED, detail="Provider unavailable")
            PaymentAttempt.objects.filter(pk=attempt.pk).update(
                status=PaymentAttempt.Status.FAILED.value,
                result_desc="Provider unavailable",
                resolved_at=timezone.now(),
                updated_at=timezone.now(),
            )
        raise

    with transaction.atomic():
        attempt.checkout_request_id = result.checkout_request_id
        attempt.merchant_request_id = result.merchant_request_id
        if result.event is not None:
            attempt.status = _ATTEMPT_STATUS.get(
                result.event.event, PaymentAttempt.Status.PENDING.value
            )
            attempt.resolved_at = timezone.now()
            attempt.save()
            apply_event(
                payment,
                result.event.event,
                receipt_code=result.event.receipt_code,
                detail=result.event.result_desc,
            )
        else:
            attempt.status = PaymentAttempt.Status.PENDING.value
            attempt.save()
    payment.refresh_from_db()
    return payment, True
