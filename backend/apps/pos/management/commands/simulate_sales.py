from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.models import User
from apps.pos import services
from apps.pos.models import MenuCategory, MenuItem, Shift


class Command(BaseCommand):
    help = "DEV ONLY: create N paid cash orders through the real services (thesis load test)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("count", type=int)
        parser.add_argument("--username", default="demo-cashier")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            raise CommandError("simulate_sales only runs with DJANGO_DEBUG=True.")
        user, created = User.objects.get_or_create(
            username=options["username"], defaults={"role": User.Role.CASHIER}
        )
        if created:
            user.set_unusable_password()
            user.save()
        shift = Shift.objects.filter(status=Shift.Status.OPEN).first() or Shift.objects.create(
            opened_by=user, opening_float=Decimal("1000")
        )
        category, _ = MenuCategory.objects.get_or_create(name="Demo")
        item, _ = MenuItem.objects.get_or_create(
            category=category, name="Demo meal", defaults={"price": Decimal("350.00")}
        )
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
