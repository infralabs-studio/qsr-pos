"""Shared bootstrap for the DEV-only demo commands."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.management.base import CommandError

from apps.accounts.models import User
from apps.pos.models import MenuCategory, MenuItem, Shift


def require_debug() -> None:
    if not settings.DEBUG:
        raise CommandError("This demo command only runs with DJANGO_DEBUG=True.")


def demo_context(username: str) -> tuple[User, Shift, MenuItem]:
    """A demo cashier, the open shift (opened if there is none) and a demo menu item."""
    user, created = User.objects.get_or_create(
        username=username, defaults={"role": User.Role.CASHIER}
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
    return user, shift, item
