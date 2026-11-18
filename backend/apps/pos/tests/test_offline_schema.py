import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import User
from apps.pos.models import Order, Outbox, Payment, Shift


class OfflineSchemaTests(TestCase):
    user: User
    shift: Shift
    order: Order

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="cashier1", password="test-pass-123")
        cls.shift = Shift.objects.create(opened_by=cls.user, opening_float=Decimal("1000"))
        cls.order = Order.objects.create_for_shift(cls.shift, cls.user)

    def _cash_payment(self, key: uuid.UUID) -> Payment:
        return Payment.objects.create(
            order=self.order,
            method=Payment.Method.CASH,
            amount=Decimal("500.00"),
            recorded_by=self.user,
            idempotency_key=key,
        )

    def test_same_idempotency_key_cannot_create_two_payments(self) -> None:
        key = uuid.uuid4()
        self._cash_payment(key)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._cash_payment(key)
        self.assertEqual(Payment.objects.filter(order=self.order).count(), 1)

    def test_outbox_enqueue_is_idempotent_and_ordered(self) -> None:
        first, created_first = Outbox.enqueue(
            aggregate_type="order",
            aggregate_id=str(self.order.id),
            event_type="order.created",
            payload={},
            idempotency_key="order:1:created",
        )
        again, created_again = Outbox.enqueue(
            aggregate_type="order",
            aggregate_id=str(self.order.id),
            event_type="order.created",
            payload={},
            idempotency_key="order:1:created",
        )
        second, _ = Outbox.enqueue(
            aggregate_type="order",
            aggregate_id=str(self.order.id),
            event_type="payment.confirmed",
            payload={},
            idempotency_key="payment:1:confirmed",
        )

        self.assertTrue(created_first)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Outbox.objects.count(), 2)
        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)

    def test_backfilled_order_requires_reason_and_actor(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(
                shift=self.shift, created_by=self.user, order_number=1, is_backfilled=True
            )
        ok = Order.objects.create(
            shift=self.shift,
            created_by=self.user,
            order_number=1,
            is_backfilled=True,
            backfill_reason="Power cut, paper ticket",
            backfilled_by=self.user,
        )
        self.assertEqual(str(ok), "B-1 (building)")

    def test_backfilled_orders_do_not_consume_live_numbers(self) -> None:
        Order.objects.create(
            shift=self.shift,
            created_by=self.user,
            order_number=5,
            is_backfilled=True,
            backfill_reason="Power cut, paper ticket",
            backfilled_by=self.user,
        )
        live = Order.objects.create_for_shift(self.shift, self.user)
        self.assertEqual(live.order_number, 2)
