"""Backfill (Step 5): a manager enters an order that was taken on paper during a total outage.

Manager-only, audited, one transaction. The order is numbered B-1, B-2... within the shift,
independently of live numbers, carries its real time in occurred_at, and never prints a
kitchen ticket. Prices are the CURRENT menu prices.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.accounts.models import User

from . import services
from .manual_mpesa import record_manual_mpesa_payment
from .models import MenuItem, ModifierOption, Order, Payment, Shift
from .services import _audit


@dataclass(frozen=True)
class BackfillLine:
    menu_item: MenuItem
    quantity: int = 1
    options: Sequence[ModifierOption] = ()
    note: str = ""


def backfill_order(
    *,
    shift: Shift,
    manager: User,
    reason: str,
    occurred_at: datetime,
    lines: Sequence[BackfillLine],
    payment_key: uuid.UUID,
    cash_tendered: Decimal | None = None,
    mpesa_code: str = "",
    mpesa_phone: str = "",
    order_id: uuid.UUID | None = None,
) -> tuple[Order, bool]:
    """Enter a paper order. Returns (order, created).

    Pass the client-generated order_id so a double-submit returns the existing order.
    Exactly one of cash_tendered / mpesa_code is required.
    """
    if not manager.is_manager:
        raise PermissionDenied("Only a manager can backfill orders.")
    reason = reason.strip()
    if not reason or len(reason) > 255:
        raise ValidationError("Give a reason of up to 255 characters.")
    if not lines:
        raise ValidationError("Add at least one item.")
    has_cash = cash_tendered is not None
    has_mpesa = bool(mpesa_code.strip())
    if has_cash == has_mpesa:
        raise ValidationError("Enter either the cash received or an M-PESA code.")
    if timezone.is_naive(occurred_at):
        raise ValidationError("The time must include a timezone.")
    if occurred_at > timezone.now() + timedelta(minutes=5):
        raise ValidationError("An order cannot have happened in the future.")

    with transaction.atomic():
        # Locking the shift serialises backfills and makes a double-submit see the first one.
        locked = Shift.objects.select_for_update().get(pk=shift.pk)
        if order_id is not None:
            existing = Order.objects.filter(pk=order_id).first()
            if existing is not None:
                return existing, False
        if locked.status != Shift.Status.OPEN:
            raise ValidationError("Orders can only be backfilled into an open shift.")
        if occurred_at < locked.opened_at:
            raise ValidationError("That time is before the shift opened.")

        last = (
            Order.objects.filter(shift=locked, is_backfilled=True).aggregate(m=Max("order_number"))[
                "m"
            ]
            or 0
        )
        extra: dict[str, Any] = {"id": order_id} if order_id is not None else {}
        order = Order.objects.create(
            shift=locked,
            created_by=manager,
            order_number=last + 1,
            is_backfilled=True,
            backfill_reason=reason,
            backfilled_by=manager,
            occurred_at=occurred_at,
            **extra,
        )
        for line in lines:
            services.add_item(
                order=order,
                menu_item=line.menu_item,
                quantity=line.quantity,
                options=line.options,
                note=line.note,
                check_availability=False,
            )
        order = services.charge_order(order=order)

        if cash_tendered is not None:
            services.confirm_cash_payment(
                order=order,
                tendered=cash_tendered,
                recorded_by=manager,
                idempotency_key=payment_key,
                occurred_at=occurred_at,
            )
            method = Payment.Method.CASH
        else:
            record_manual_mpesa_payment(
                order=order,
                receipt_code=mpesa_code,
                amount=order.total,
                recorded_by=manager,
                idempotency_key=payment_key,
                phone_number=mpesa_phone,
                occurred_at=occurred_at,
            )
            method = Payment.Method.MPESA_MANUAL

        _audit(
            event_type="order.backfilled",
            actor=manager,
            target_type="order",
            target_id=order.pk,
            new_state={
                "order_number": f"B-{order.order_number}",
                "occurred_at": occurred_at.isoformat(),
                "method": method.value,
                "total": f"{order.total:.2f}",
            },
            reason=reason,
        )
        return Order.objects.get(pk=order.pk), True
