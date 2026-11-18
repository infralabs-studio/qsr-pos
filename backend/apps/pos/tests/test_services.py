import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.pos import services
from apps.pos.models import (
    AuditEvent,
    MenuCategory,
    MenuItem,
    ModifierGroup,
    ModifierOption,
    Order,
    Outbox,
    Payment,
    PrintJob,
    Shift,
)


class ServiceTests(TestCase):
    user: User
    shift: Shift
    plain: MenuItem
    burger: MenuItem
    large: ModifierOption

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="cashier1", password="test-pass-123")
        cls.shift = Shift.objects.create(opened_by=cls.user, opening_float=Decimal("1000"))
        category = MenuCategory.objects.create(name="Mains")
        cls.plain = MenuItem.objects.create(
            category=category, name="Chips", price=Decimal("350.00")
        )
        cls.burger = MenuItem.objects.create(
            category=category, name="Beef Burger", price=Decimal("350.00")
        )
        size = ModifierGroup.objects.create(name="Size", min_select=1, max_select=1)
        cls.burger.modifier_groups.add(size)
        cls.large = ModifierOption.objects.create(
            group=size, name="Large", price_delta=Decimal("80.00")
        )

    def _awaiting_order(self) -> Order:
        order = services.create_order(shift=self.shift, created_by=self.user)
        services.add_item(order=order, menu_item=self.plain)
        return services.charge_order(order=order)

    def test_create_order_is_idempotent_for_client_supplied_id(self) -> None:
        order_id = uuid.uuid4()
        first = services.create_order(shift=self.shift, created_by=self.user, order_id=order_id)
        again = services.create_order(shift=self.shift, created_by=self.user, order_id=order_id)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Order.objects.filter(shift=self.shift).count(), 1)
        self.assertEqual(first.order_number, 1)

    def test_required_modifier_is_enforced_and_priced(self) -> None:
        order = services.create_order(shift=self.shift, created_by=self.user)
        with self.assertRaises(ValidationError):
            services.add_item(order=order, menu_item=self.burger)
        item = services.add_item(order=order, menu_item=self.burger, options=[self.large])
        self.assertEqual(item.unit_price, Decimal("430.00"))
        order.refresh_from_db()
        self.assertEqual(order.total, Decimal("430.00"))

    def test_totals_with_tax_inclusive_prices(self) -> None:
        order = services.create_order(shift=self.shift, created_by=self.user)
        item = MenuItem.objects.create(
            category=self.plain.category, name="Soda", price=Decimal("116.00")
        )
        services.add_item(order=order, menu_item=item)
        order.refresh_from_db()
        self.assertEqual(
            (order.subtotal, order.tax_total, order.total),
            (Decimal("100.00"), Decimal("16.00"), Decimal("116.00")),
        )

    @override_settings(POS_PRICES_TAX_INCLUSIVE=False)
    def test_totals_with_tax_exclusive_prices(self) -> None:
        order = services.create_order(shift=self.shift, created_by=self.user)
        item = MenuItem.objects.create(
            category=self.plain.category, name="Water", price=Decimal("100.00")
        )
        services.add_item(order=order, menu_item=item)
        order.refresh_from_db()
        self.assertEqual(
            (order.subtotal, order.tax_total, order.total),
            (Decimal("100.00"), Decimal("16.00"), Decimal("116.00")),
        )

    def test_cash_payment_happy_path(self) -> None:
        order = self._awaiting_order()
        payment, created = services.confirm_cash_payment(
            order=order,
            tendered=Decimal("500.00"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        order.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(payment.change_given, Decimal("150.00"))
        self.assertEqual(payment.shift_id, self.shift.pk)
        self.assertEqual(PrintJob.objects.filter(order=order).count(), 1)
        events = list(Outbox.objects.order_by("sequence").values_list("event_type", flat=True))
        self.assertEqual(events, ["order.paid", "payment.confirmed"])
        self.assertTrue(AuditEvent.objects.filter(event_type="payment.cash_confirmed").exists())

    def test_double_tap_with_same_key_creates_one_payment(self) -> None:
        order = self._awaiting_order()
        key = uuid.uuid4()
        first, created_first = services.confirm_cash_payment(
            order=order, tendered=Decimal("500.00"), recorded_by=self.user, idempotency_key=key
        )
        second, created_second = services.confirm_cash_payment(
            order=order, tendered=Decimal("500.00"), recorded_by=self.user, idempotency_key=key
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)
        self.assertEqual(Outbox.objects.count(), 2)
        self.assertEqual(PrintJob.objects.filter(order=order).count(), 1)

    def test_tender_below_total_is_rejected(self) -> None:
        order = self._awaiting_order()
        with self.assertRaises(ValidationError):
            services.confirm_cash_payment(
                order=order,
                tendered=Decimal("100.00"),
                recorded_by=self.user,
                idempotency_key=uuid.uuid4(),
            )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(Payment.objects.count(), 0)

    def test_cannot_pay_twice_with_different_keys(self) -> None:
        order = self._awaiting_order()
        services.confirm_cash_payment(
            order=order,
            tendered=Decimal("500.00"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        with self.assertRaises(ValidationError):
            services.confirm_cash_payment(
                order=order,
                tendered=Decimal("500.00"),
                recorded_by=self.user,
                idempotency_key=uuid.uuid4(),
            )
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)

    def test_failure_mid_payment_rolls_everything_back(self) -> None:
        """Scenario 4: a failure after the payment row is written must leave no trace."""
        order = self._awaiting_order()
        with patch.object(Outbox, "enqueue", side_effect=RuntimeError("disk gone")):
            with self.assertRaises(RuntimeError):
                services.confirm_cash_payment(
                    order=order,
                    tendered=Decimal("500.00"),
                    recorded_by=self.user,
                    idempotency_key=uuid.uuid4(),
                )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(PrintJob.objects.count(), 0)
        self.assertEqual(Outbox.objects.count(), 0)
        self.assertFalse(AuditEvent.objects.filter(event_type="payment.cash_confirmed").exists())
