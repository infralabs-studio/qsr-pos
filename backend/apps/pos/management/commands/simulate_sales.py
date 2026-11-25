from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.pos import services

from ._demo import demo_context, require_debug


class Command(BaseCommand):
    help = "DEV ONLY: create N paid cash orders through the real services (thesis load test)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("count", type=int)
        parser.add_argument("--username", default="demo-cashier")

    def handle(self, *args: Any, **options: Any) -> None:
        require_debug()
        user, shift, item = demo_context(options["username"])
        for _i in range(options["count"]):
            order = services.create_order(shift=shift, created_by=user)
            services.add_item(order=order, menu_item=item)
            services.charge_order(order=order)
            services.confirm_cash_payment(
                order=order,
                tendered=Decimal("500.00"),
                recorded_by=user,
                idempotency_key=uuid.uuid4(),
            )
        self.stdout.write(f"Created {options['count']} paid order(s) on shift {shift.pk}.")
