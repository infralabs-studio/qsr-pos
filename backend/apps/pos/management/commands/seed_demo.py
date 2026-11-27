from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.accounts.models import User
from apps.pos.models import MenuCategory, MenuItem, ModifierGroup, ModifierOption

from ._demo import require_debug

# category -> [(item, price, modifier groups)]
MENU: dict[str, list[tuple[str, str, list[str]]]] = {
    "Burgers": [
        ("Beef Burger", "450.00", ["Size", "Add-ons"]),
        ("Chicken Burger", "420.00", ["Size", "Add-ons"]),
    ],
    "Sides": [("Fries", "180.00", [])],
    "Drinks": [("Soda", "100.00", []), ("Water", "60.00", [])],
}
GROUPS: dict[str, tuple[int, int, list[tuple[str, str]]]] = {
    "Size": (1, 1, [("Regular", "0.00"), ("Large", "80.00")]),
    "Add-ons": (0, 3, [("Extra cheese", "50.00"), ("Bacon", "70.00"), ("No onions", "0.00")]),
}


class Command(BaseCommand):
    help = "DEV ONLY: seed demo users (cashier, manager) and a small menu. Idempotent."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--password", required=True, help="Password for both demo users.")

    def handle(self, *args: Any, **options: Any) -> None:
        require_debug()
        for username, role in [("cashier", User.Role.CASHIER), ("manager", User.Role.MANAGER)]:
            user, _ = User.objects.get_or_create(username=username, defaults={"role": role})
            user.role = role
            user.set_password(options["password"])
            user.save()

        groups: dict[str, ModifierGroup] = {}
        for name, (low, high, choices) in GROUPS.items():
            group, _ = ModifierGroup.objects.get_or_create(
                name=name, defaults={"min_select": low, "max_select": high}
            )
            for position, (label, delta) in enumerate(choices):
                ModifierOption.objects.get_or_create(
                    group=group,
                    name=label,
                    defaults={
                        "price_delta": Decimal(delta),
                        "sort_order": position,
                        "is_default": low > 0 and position == 0,
                    },
                )
            groups[name] = group

        for position, (category_name, items) in enumerate(MENU.items()):
            category, _ = MenuCategory.objects.get_or_create(
                name=category_name, defaults={"sort_order": position}
            )
            for item_name, price, group_names in items:
                item, _ = MenuItem.objects.get_or_create(
                    category=category, name=item_name, defaults={"price": Decimal(price)}
                )
                item.modifier_groups.set([groups[g] for g in group_names])
        self.stdout.write("Seeded users 'cashier' and 'manager' and the demo menu.")
