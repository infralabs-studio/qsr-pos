"""Reconciliation (Step 5): match manual M-PESA entries against Safaricom's records.

For a payment sitting in pending_verification:
  * exact match (same code, same amount)  -> confirmed automatically, no manager step
  * near match (typo, or amount differs)  -> manager_review with a suggestion, one-tap confirm
  * no match                              -> stays pending; after the grace period it goes to
                                             manager_review. It is never auto-reversed.
Looking money up can need the network, so it always happens outside a DB transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User

from .manual_mpesa import LIVE_PAYMENT_STATUSES, normalize_receipt_code, order_label
from .models import Order, Payment
from .payment_flow import TransitionResult, TransitionStatus, apply_event
from .payment_states import PaymentEvent
from .providers.base import LedgerTransaction, ProviderUnavailable, TransactionSource
from .services import _audit

# ----------------------------------------------------------------------------- matching

_CONFUSABLE = str.maketrans({"O": "0", "I": "1"})


def is_near_miss(entered: str, recorded: str) -> bool:
    """True if two codes differ by at most one typo: one wrong character or one adjacent
    swap, treating O/0 and I/1 as the same character."""
    a = entered.upper().translate(_CONFUSABLE)
    b = recorded.upper().translate(_CONFUSABLE)
    if len(a) != len(b):
        return False
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diffs) <= 1:
        return True
    return (
        len(diffs) == 2
        and diffs[1] == diffs[0] + 1
        and a[diffs[0]] == b[diffs[1]]
        and a[diffs[1]] == b[diffs[0]]
    )


class MatchKind(StrEnum):
    EXACT = "exact"
    CODE_AMOUNT_MISMATCH = "code_amount_mismatch"
    NEAR = "near"
    NONE = "none"


@dataclass(frozen=True)
class Match:
    kind: MatchKind
    transaction: LedgerTransaction | None = None
    detail: str = ""


def find_match(payment: Payment, source: TransactionSource) -> Match:
    """Look the payment up in Safaricom's records. May raise ProviderUnavailable."""
    code = payment.mpesa_receipt_code
    recorded = source.lookup(code)
    if recorded is not None:
        if recorded.amount == payment.amount:
            return Match(MatchKind.EXACT, recorded)
        detail = (
            f"Code {code} was paid KES {recorded.amount}, "
            f"but the order total is KES {payment.amount}."
        )
        return Match(MatchKind.CODE_AMOUNT_MISMATCH, recorded, detail)

    window = timedelta(minutes=settings.RECONCILIATION_WINDOW_MINUTES)
    taken = set(
        Payment.objects.filter(status__in=LIVE_PAYMENT_STATUSES)
        .exclude(pk=payment.pk)
        .exclude(mpesa_receipt_code="")
        .values_list("mpesa_receipt_code", flat=True)
    )
    near = [
        c
        for c in source.candidates(payment.occurred_at - window, payment.occurred_at + window)
        if c.amount == payment.amount
        and c.receipt_code not in taken
        and is_near_miss(code, c.receipt_code)
    ]
    if not near:
        return Match(MatchKind.NONE)
    near.sort(key=lambda c: abs(c.occurred_at - payment.occurred_at))
    best = near[0]
    more = f" ({len(near)} possible records; showing the closest)" if len(near) > 1 else ""
    detail = (
        f"Entered {code}; closest record is {best.receipt_code}, "
        f"KES {best.amount} at {best.occurred_at:%H:%M}{more}."
    )
    return Match(MatchKind.NEAR, best, detail)


def _claimed_elsewhere(payment: Payment, code: str) -> Payment | None:
    """A live payment on a DIFFERENT order that already holds this M-PESA code."""
    return (
        Payment.objects.select_related("order")
        .filter(mpesa_receipt_code=code, status__in=LIVE_PAYMENT_STATUSES)
        .exclude(pk=payment.pk)
        .exclude(order_id=payment.order_id)
        .first()
    )


# ------------------------------------------------------------------------ reconciliation


class ReconcileOutcome(StrEnum):
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    STILL_PENDING = "still_pending"
    SKIPPED = "skipped"  # not waiting for verification any more
    UNAVAILABLE = "unavailable"  # could not reach the records: nothing changed


@dataclass(frozen=True)
class ReconcileResult:
    outcome: ReconcileOutcome
    payment: Payment
    detail: str = ""


def _record_check(
    payment: Payment,
    now: datetime,
    *,
    suggested: str = "",
    note: str = "",
    duplicate_of: Payment | None = None,
) -> None:
    fields: dict[str, Any] = {
        "last_reconciled_at": now,
        "suggested_receipt_code": suggested,
        "reconciliation_note": note[:255],
        "updated_at": now,
    }
    if duplicate_of is not None:
        fields["duplicate_of"] = duplicate_of
    Payment.objects.filter(pk=payment.pk).update(**fields)


def _skipped(payment: Payment) -> ReconcileResult:
    current = Payment.objects.get(pk=payment.pk)
    return ReconcileResult(ReconcileOutcome.SKIPPED, current, "Payment changed meanwhile")


def _confirm_exact(payment: Payment, now: datetime) -> ReconcileResult:
    code = payment.mpesa_receipt_code
    with transaction.atomic():
        result = apply_event(
            payment, PaymentEvent.VERIFIED_EXACT, detail=f"Matched {code} automatically"
        )
        if result.status != TransitionStatus.APPLIED:
            return _skipped(payment)
        _record_check(payment, now)
    return ReconcileResult(
        ReconcileOutcome.CONFIRMED, Payment.objects.get(pk=payment.pk), "Matched automatically"
    )


def _flag(
    payment: Payment,
    now: datetime,
    reason: Payment.ReviewReason,
    note: str,
    *,
    suggested: str = "",
    duplicate_of: Payment | None = None,
) -> ReconcileResult:
    with transaction.atomic():
        result = apply_event(
            payment, PaymentEvent.FLAGGED_FOR_REVIEW, review_reason=reason.value, detail=note
        )
        if result.status != TransitionStatus.APPLIED:
            return _skipped(payment)
        _record_check(payment, now, suggested=suggested, note=note, duplicate_of=duplicate_of)
    return ReconcileResult(ReconcileOutcome.NEEDS_REVIEW, Payment.objects.get(pk=payment.pk), note)


def reconcile_payment(
    payment: Payment, source: TransactionSource, *, now: datetime | None = None
) -> ReconcileResult:
    """Reconcile one manual M-PESA payment. Safe to run repeatedly."""
    now = now or timezone.now()
    current = Payment.objects.get(pk=payment.pk)
    if (
        current.method != Payment.Method.MPESA_MANUAL
        or current.status != Payment.Status.PENDING_VERIFICATION
    ):
        return ReconcileResult(ReconcileOutcome.SKIPPED, current, "Not waiting for verification")

    try:
        match = find_match(current, source)
    except ProviderUnavailable as exc:
        return ReconcileResult(ReconcileOutcome.UNAVAILABLE, current, str(exc))

    code = current.mpesa_receipt_code
    if match.kind == MatchKind.EXACT:
        other = _claimed_elsewhere(current, code)
        if other is None:
            return _confirm_exact(current, now)
        note = f"Code {code} is already recorded on order {order_label(other.order)}."
        return _flag(
            current, now, Payment.ReviewReason.CROSS_METHOD_DUPLICATE, note, duplicate_of=other
        )
    if match.kind == MatchKind.CODE_AMOUNT_MISMATCH:
        return _flag(current, now, Payment.ReviewReason.FUZZY_MATCH, match.detail, suggested=code)
    if match.kind == MatchKind.NEAR:
        assert match.transaction is not None
        return _flag(
            current,
            now,
            Payment.ReviewReason.FUZZY_MATCH,
            match.detail,
            suggested=match.transaction.receipt_code,
        )

    grace_hours = settings.RECONCILIATION_GRACE_HOURS
    if now - current.occurred_at >= timedelta(hours=grace_hours):
        note = f"No matching M-PESA record after {grace_hours} hours."
        return _flag(current, now, Payment.ReviewReason.NO_MATCH, note)
    note = "No matching record yet. Will keep checking."
    _record_check(current, now, note=note)
    return ReconcileResult(ReconcileOutcome.STILL_PENDING, Payment.objects.get(pk=current.pk), note)


@dataclass
class ReconcileReport:
    checked: int = 0
    confirmed: int = 0
    needs_review: int = 0
    still_pending: int = 0
    unavailable: bool = False


def reconcile_pending(source: TransactionSource, *, now: datetime | None = None) -> ReconcileReport:
    """Reconcile every manual payment waiting for verification, oldest first. Stops at the first
    sign the records are unreachable instead of hammering a dead connection."""
    report = ReconcileReport()
    waiting = Payment.objects.filter(
        method=Payment.Method.MPESA_MANUAL, status=Payment.Status.PENDING_VERIFICATION
    ).order_by("occurred_at")
    for payment in list(waiting):
        result = reconcile_payment(payment, source, now=now)
        if result.outcome == ReconcileOutcome.UNAVAILABLE:
            report.unavailable = True
            break
        report.checked += 1
        if result.outcome == ReconcileOutcome.CONFIRMED:
            report.confirmed += 1
        elif result.outcome == ReconcileOutcome.NEEDS_REVIEW:
            report.needs_review += 1
        elif result.outcome == ReconcileOutcome.STILL_PENDING:
            report.still_pending += 1
    return report


# --------------------------------------------------------------------------- manager actions


def _require_manager(actor: User) -> None:
    if not actor.is_manager:
        raise PermissionDenied("A manager must do this.")


def manager_confirm_payment(
    *, payment: Payment, manager: User, receipt_code: str = ""
) -> TransitionResult:
    """One-tap confirm. Uses the suggested code unless the manager supplies one."""
    _require_manager(manager)
    current = Payment.objects.get(pk=payment.pk)
    if (
        current.status == Payment.Status.MANAGER_REVIEW
        and current.review_reason == Payment.ReviewReason.CROSS_METHOD_DUPLICATE
    ):
        raise ValidationError(
            "A duplicate payment cannot be confirmed. Reject it, or refund the customer."
        )
    chosen = receipt_code or current.suggested_receipt_code
    code = normalize_receipt_code(chosen) if chosen else ""
    if code and _claimed_elsewhere(current, code) is not None:
        raise ValidationError("That M-PESA code is already recorded on another order.")
    return apply_event(
        current,
        PaymentEvent.MANAGER_CONFIRMED,
        actor=manager,
        receipt_code=code,
        detail="Confirmed by manager",
    )


def _reopen_if_unsettled(order_id: uuid.UUID, manager: User, reason: str) -> None:
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.Status.PAID_PENDING_VERIFICATION:
        return
    if Payment.objects.filter(order=order, status__in=LIVE_PAYMENT_STATUSES).exists():
        return
    order.status = Order.Status.AWAITING_PAYMENT
    order.save(update_fields=["status", "updated_at"])
    _audit(
        event_type="order.reopened_unpaid",
        actor=manager,
        target_type="order",
        target_id=order.pk,
        new_state={"status": order.status},
        reason=reason,
    )


def manager_reject_payment(*, payment: Payment, manager: User, reason: str) -> TransitionResult:
    """The payment is not valid. If it was the order's last live payment, the order goes back
    to awaiting_payment so the money can still be collected."""
    _require_manager(manager)
    if not reason.strip():
        raise ValidationError("Give a reason.")
    with transaction.atomic():
        result = apply_event(
            payment, PaymentEvent.MANAGER_REJECTED, actor=manager, detail=reason.strip()
        )
        if result.status == TransitionStatus.APPLIED:
            _reopen_if_unsettled(result.payment.order_id, manager, reason.strip())
    return result


def manager_defer_payment(*, payment: Payment, manager: User) -> TransitionResult:
    """Leave pending: back to pending_verification so reconciliation tries again."""
    _require_manager(manager)
    return apply_event(
        payment, PaymentEvent.MANAGER_DEFERRED, actor=manager, detail="Left pending by manager"
    )
