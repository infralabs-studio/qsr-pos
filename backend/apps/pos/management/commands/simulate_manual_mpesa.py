from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.pos import services
from apps.pos.manual_mpesa import record_manual_mpesa_payment

from ._demo import demo_context, require_debug


class Command(BaseCommand):
    help = "DEV ONLY: create one order paid by manual M-PESA entry (offline-thesis smoke test)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("code", help="The M-PESA confirmation code the cashier types in.")
        parser.add_argument("--username", default="demo-cashier")

    def handle(self, *args: Any, **options: Any) -> None:
        require_debug()
        user, shift, item = demo_context(options["username"])
        order = services.create_order(shift=shift, created_by=user)
        services.add_item(order=order, menu_item=item)
        order = services.charge_order(order=order)
        payment, _ = record_manual_mpesa_payment(
            order=order,
            receipt_code=options["code"],
            amount=order.total,
            recorded_by=user,
            idempotency_key=uuid.uuid4(),
        )
        self.stdout.write(
            f"Order #{order.order_number} total KES {order.total} is {order.status}; "
            f"payment {payment.status} (code {payment.mpesa_receipt_code})."
        )
