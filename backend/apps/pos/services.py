"""Transactional services for the order -> cash payment path (offline thesis, Step 2).

Every service runs in one DB transaction: the business write, its Outbox rows and its audit
event commit together or not at all. Order rows are locked with select_for_update, so
concurrent calls (double-tap, retries, replays) serialise.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User

from .models import (
    AuditEvent,
    MenuItem,
    ModifierOption,
    Order,
    OrderItem,
    OrderItemModifier,
    Outbox,
    Payment,
    PrintJob,
    Shift,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


# --------------------------------------------------------------------------- helpers


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _line_amounts(
    unit_price: Decimal, quantity: int, rate: Decimal, inclusive: bool
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (net, tax, gross) for a line. gross is what the customer pays."""
    base = (unit_price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
    if inclusive:
        net = (base / (1 + rate)).quantize(CENT, rounding=ROUND_HALF_UP)
        return net, base - net, base
    tax = (base * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    return base, tax, base + tax


def _recalculate(order: Order) -> None:
    inclusive = settings.POS_PRICES_TAX_INCLUSIVE
    subtotal = tax_total = total = ZERO
    for item in order.items.all():
        net, tax, gross = _line_amounts(item.unit_price, item.quantity, item.tax_rate, inclusive)
        subtotal += net
        tax_total += tax
        total += gross
    order.subtotal = subtotal
    order.tax_total = tax_total
    order.total = total - order.discount_total
    order.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])


def _validate_modifiers(menu_item: MenuItem, options: Sequence[ModifierOption]) -> None:
    groups = {group.pk: group for group in menu_item.modifier_groups.all()}
    chosen = {pk: 0 for pk in groups}
    for option in options:
        if option.group_id not in groups:
            raise ValidationError(f"{option.name} is not offered with {menu_item.name}.")
        if not option.is_available:
            raise ValidationError(f"{option.name} is not available.")
        chosen[option.group_id] += 1
    for pk, group in groups.items():
        if chosen[pk] < group.min_select:
            raise ValidationError(f"Choose {group.name}.")
        if chosen[pk] > group.max_select:
            raise ValidationError(f"Too many options chosen for {group.name}.")


def _audit(
    *,
    event_type: str,
    actor: User | None,
    target_type: str,
    target_id: Any,
    new_state: dict[str, Any],
    previous_state: dict[str, Any] | None = None,
    reason: str = "",
) -> None:
    AuditEvent.objects.create(
        event_type=event_type,
        actor=actor,
        actor_role=actor.role if actor else "",
        target_type=target_type,
        target_id=str(target_id),
        previous_state=previous_state,
        new_state=new_state,
        reason=reason,
    )


def _order_snapshot(order: Order) -> dict[str, Any]:
    items = []
    for item in order.items.prefetch_related("modifiers").all():
        items.append(
            {
                "id": str(item.pk),
                "menu_item_id": str(item.menu_item_id),
                "name": item.name,
                "quantity": item.quantity,
                "unit_price": _money(item.unit_price),
                "tax_rate": str(item.tax_rate),
                "line_total": _money(item.line_total),
                "note": item.note,
                "modifiers": [
                    {"name": m.name, "price_delta": _money(m.price_delta)}
                    for m in item.modifiers.all()
                ],
            }
        )
    return {
        "id": str(order.pk),
        "status": order.status,
        "shift_id": str(order.shift_id),
        "order_number": order.order_number,
        "is_backfilled": order.is_backfilled,
        "occurred_at": order.occurred_at.isoformat(),
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "subtotal": _money(order.subtotal),
        "tax_total": _money(order.tax_total),
        "discount_total": _money(order.discount_total),
        "total": _money(order.total),
        "items": items,
    }


def _payment_snapshot(payment: Payment) -> dict[str, Any]:
    return {
        "id": str(payment.pk),
        "order_id": str(payment.order_id),
        "shift_id": str(payment.shift_id),
        "method": payment.method,
        "status": payment.status,
        "amount": _money(payment.amount),
        "tendered_amount": _money(payment.tendered_amount) if payment.tendered_amount else None,
        "change_given": _money(payment.change_given) if payment.change_given else None,
        "idempotency_key": str(payment.idempotency_key),
        "occurred_at": payment.occurred_at.isoformat(),
        "mpesa_receipt_code": payment.mpesa_receipt_code,
    }


def mark_order_paid(*, order: Order, payment: Payment, paid_at: datetime) -> None:
    """Shared settlement step for cash and M-PESA. Must be called inside transaction.atomic()
    with the Order row already locked."""
    order.status = Order.Status.PAID
    order.paid_at = paid_at
    order.save(update_fields=["status", "paid_at", "updated_at"])

    # Stub queue only: the real print service drains this in Sprint 2.
    PrintJob.objects.get_or_create(
        order=order,
        job_type=PrintJob.JobType.KITCHEN_TICKET,
        is_reprint=False,
        defaults={"payload": {"order_number": order.order_number}},
    )

    # Same transaction, in causal order: the order drains before its payment.
    Outbox.enqueue(
        aggregate_type="order",
        aggregate_id=str(order.pk),
        event_type="order.paid",
        payload=_order_snapshot(order),
        idempotency_key=f"order:{order.pk}:paid",
    )
    Outbox.enqueue(
        aggregate_type="payment",
        aggregate_id=str(payment.pk),
        event_type="payment.confirmed",
        payload=_payment_snapshot(payment),
        idempotency_key=f"payment:{payment.pk}:confirmed",
    )


# --------------------------------------------------------------------------- services


def create_order(*, shift: Shift, created_by: User, order_id: uuid.UUID | None = None) -> Order:
    """Create a live order. Passing the same client-generated order_id again returns the
    existing order, so a replayed request never makes a second one."""
    fields: dict[str, Any] = {"id": order_id} if order_id else {}
    try:
        with transaction.atomic():
            order = Order.objects.create_for_shift(shift, created_by, **fields)
            _audit(
                event_type="order.created",
                actor=created_by,
                target_type="order",
                target_id=order.pk,
                new_state={"order_number": order.order_number, "shift_id": str(shift.pk)},
            )
            return order
    except IntegrityError:
        existing = Order.objects.filter(pk=order_id).first() if order_id else None
        if existing is None:
            raise
        return existing


def add_item(
    *,
    order: Order,
    menu_item: MenuItem,
    quantity: int = 1,
    options: Sequence[ModifierOption] = (),
    note: str = "",
    check_availability: bool = True,
) -> OrderItem:
    """Add a line to an order that is still being built. Prices are snapshotted.

    check_availability=False is for backfill only: the item was sold while it was available.
    """
    if quantity < 1:
        raise ValidationError("Quantity must be at least 1.")
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.Status.BUILDING:
            raise ValidationError("Items can only be added while the order is being built.")
        if check_availability and not menu_item.is_available:
            raise ValidationError(f"{menu_item.name} is not available right now.")
        _validate_modifiers(menu_item, options)

        unit_price = menu_item.price + sum((o.price_delta for o in options), ZERO)
        gross = _line_amounts(
            unit_price, quantity, menu_item.tax_rate, settings.POS_PRICES_TAX_INCLUSIVE
        )[2]
        item = OrderItem.objects.create(
            order=locked,
            menu_item=menu_item,
            name=menu_item.name,
            unit_price=unit_price,
            tax_rate=menu_item.tax_rate,
            quantity=quantity,
            line_total=gross,
            note=note,
        )
        OrderItemModifier.objects.bulk_create(
            [
                OrderItemModifier(order_item=item, option=o, name=o.name, price_delta=o.price_delta)
                for o in options
            ]
        )
        _recalculate(locked)
        return item


def charge_order(*, order: Order) -> Order:
    """Cashier taps Charge: building -> awaiting_payment. Safe to call twice."""
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status not in (Order.Status.BUILDING, Order.Status.AWAITING_PAYMENT):
            raise ValidationError("This order can no longer be charged.")
        if not locked.items.exists() or locked.total <= 0:
            raise ValidationError("Add at least one item before charging.")
        locked.status = Order.Status.AWAITING_PAYMENT
        locked.save(update_fields=["status", "updated_at"])
        return locked


def finalize_paid_order(order: Order, payment: Payment, now: datetime) -> None:
    """Order -> paid, queue the (stub) kitchen ticket and the Outbox events.

    Shared by cash confirmation and M-PESA confirmation. Must run inside the caller's
    transaction with the order row locked. Safe to call again for the same order.
    """
    order.status = Order.Status.PAID
    order.paid_at = order.paid_at or now
    order.save(update_fields=["status", "paid_at", "updated_at"])

    # Stub queue only: the real print service drains this in Sprint 2.
    # A backfilled order was served long ago: it never prints a kitchen ticket.
    if not order.is_backfilled:
        PrintJob.objects.get_or_create(
            order=order,
            job_type=PrintJob.JobType.KITCHEN_TICKET,
            is_reprint=False,
            defaults={"payload": {"order_number": order.order_number}},
        )

    # Same transaction, in causal order: the order drains before its payment.
    Outbox.enqueue(
        aggregate_type="order",
        aggregate_id=str(order.pk),
        event_type="order.paid",
        payload=_order_snapshot(order),
        idempotency_key=f"order:{order.pk}:paid",
    )
    Outbox.enqueue(
        aggregate_type="payment",
        aggregate_id=str(payment.pk),
        event_type="payment.confirmed",
        payload=_payment_snapshot(payment),
        idempotency_key=f"payment:{payment.pk}:confirmed",
    )


def confirm_cash_payment(
    *,
    order: Order,
    tendered: Decimal,
    recorded_by: User,
    idempotency_key: uuid.UUID,
    occurred_at: datetime | None = None,
) -> tuple[Payment, bool]:
    """Confirm a cash payment. Returns (payment, created).

    Generate idempotency_key once per tap of "Confirm" on the client. Replays with the same key
    (double-tap, retry after a crash) return the original payment and change nothing.
    occurred_at is for backfill: when the money actually changed hands.
    """
    with transaction.atomic():
        locked = (
            Order.objects.select_for_update(of=("self",)).select_related("shift").get(pk=order.pk)
        )

        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.order_id != locked.pk:
                raise ValidationError("This payment key belongs to a different order.")
            return existing, False

        if locked.status != Order.Status.AWAITING_PAYMENT:
            raise ValidationError("This order is not awaiting payment.")
        total = locked.total
        if tendered < total:
            raise ValidationError(f"Amount received is KES {total - tendered} less than the total.")

        now = timezone.now()
        payment = Payment.objects.create(
            order=locked,
            method=Payment.Method.CASH,
            status=Payment.Status.CONFIRMED,
            amount=total,
            tendered_amount=tendered,
            change_given=tendered - total,
            recorded_by=recorded_by,
            confirmed_by=recorded_by,
            confirmed_at=now,
            occurred_at=occurred_at or now,
            idempotency_key=idempotency_key,
        )
        finalize_paid_order(locked, payment, now)
        _audit(
            event_type="payment.cash_confirmed",
            actor=recorded_by,
            target_type="payment",
            target_id=payment.pk,
            new_state=_payment_snapshot(payment),
        )
        return payment, True


def get_open_shift() -> Shift | None:
    return Shift.objects.filter(status=Shift.Status.OPEN).first()


def open_shift(*, opened_by: User, opening_float: Decimal) -> Shift:
    """Open the shift (manager only). The one-open-shift rule is enforced by the database."""
    if not opened_by.is_manager:
        raise PermissionDenied("A manager must open the shift.")
    if opening_float < 0:
        raise ValidationError("The starting float cannot be negative.")
    try:
        with transaction.atomic():
            shift = Shift.objects.create(opened_by=opened_by, opening_float=opening_float)
            _audit(
                event_type="shift.opened",
                actor=opened_by,
                target_type="shift",
                target_id=shift.pk,
                new_state={"opening_float": _money(opening_float)},
            )
    except IntegrityError as exc:
        raise ValidationError("A shift is already open.") from exc
    return shift
