from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import User
from apps.pos import services
from apps.pos.models import MenuCategory, MenuItem, Order, Shift


class PosTestCase(TestCase):
    user: User
    manager: User
    shift: Shift
    item: MenuItem

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="cashier1", password="test-pass-123")
        cls.manager = User.objects.create_user(
            username="manager1", password="test-pass-123", role=User.Role.MANAGER
        )
        cls.shift = Shift.objects.create(opened_by=cls.user, opening_float=Decimal("1000"))
        category = MenuCategory.objects.create(name="Mains")
        cls.item = MenuItem.objects.create(category=category, name="Chips", price=Decimal("350.00"))

    def awaiting_order(self, *, quantity: int = 1) -> Order:
        order = services.create_order(shift=self.shift, created_by=self.user)
        services.add_item(order=order, menu_item=self.item, quantity=quantity)
        return services.charge_order(order=order)
