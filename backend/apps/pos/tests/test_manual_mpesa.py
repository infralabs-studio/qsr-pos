import uuid
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError

from apps.pos import services
from apps.pos.manual_mpesa import record_manual_mpesa_payment
from apps.pos.models import AuditEvent, Order, Outbox, Payment, PrintJob
from apps.pos.tests.base import PosTestCase


class ManualMpesaEntryTests(PosTestCase):
    def _record(
        self,
        order: Order,
        code: str = "QGH7XXXXXX",
        amount: str = "350.00",
        key: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> tuple[Payment, bool]:
        return record_manual_mpesa_payment(
            order=order,
            receipt_code=code,
            amount=Decimal(amount),
            recorded_by=self.user,
            idempotency_key=key or uuid.uuid4(),
            **kwargs,
        )

    def _events(self) -> list[str]:
        return list(Outbox.objects.order_by("sequence").values_list("event_type", flat=True))

    def test_records_pending_verification_and_the_kitchen_proceeds(self) -> None:
        order = self.awaiting_order()

        payment, created = self._record(order, code=" qgh7 xxxxxx ", phone_number="0712 345 678")

        order.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)
        self.assertEqual(payment.mpesa_receipt_code, "QGH7XXXXXX")
        self.assertEqual(payment.phone_number, "254712345678")
        self.assertEqual(order.status, Order.Status.PAID_PENDING_VERIFICATION)
        self.assertIsNone(order.paid_at)
        self.assertEqual(PrintJob.objects.filter(order=order).count(), 1)
        self.assertEqual(
            self._events(),
            ["order.paid_pending_verification", "payment.pending_verification"],
        )
        self.assertTrue(
            AuditEvent.objects.filter(event_type="payment.manual_mpesa_recorded").exists()
        )

    def test_rejects_malformed_codes(self) -> None:
        order = self.awaiting_order()
        for bad in ("", "QGH7", "QGH7XXXXXX1", "QGH7-XXXXX"):
            with self.subTest(code=bad), self.assertRaises(ValidationError):
                self._record(order, code=bad)
        self.assertEqual(Payment.objects.count(), 0)

    def test_amount_must_equal_the_order_total(self) -> None:
        order = self.awaiting_order()
        with self.assertRaises(ValidationError):
            self._record(order, amount="300.00")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(Payment.objects.count(), 0)

    def test_code_already_used_on_another_order_is_rejected(self) -> None:
        first = self.awaiting_order()
        self._record(first)
        second = self.awaiting_order()

        with self.assertRaises(ValidationError) as caught:
            self._record(second)

        self.assertIn("#1", str(caught.exception))
        self.assertEqual(Payment.objects.filter(order=second).count(), 0)

    def test_replay_with_the_same_key_creates_one_payment(self) -> None:
        order = self.awaiting_order()
        key = uuid.uuid4()

        first, created_first = self._record(order, key=key)
        second, created_second = self._record(order, key=key)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)
        self.assertEqual(Outbox.objects.count(), 2)
        self.assertEqual(PrintJob.objects.filter(order=order).count(), 1)

    def test_order_must_be_awaiting_payment(self) -> None:
        order = services.create_order(shift=self.shift, created_by=self.user)
        services.add_item(order=order, menu_item=self.item)
        with self.assertRaises(ValidationError):
            self._record(order)
        self.assertEqual(Payment.objects.count(), 0)

    def test_invalid_phone_number_is_rejected_before_anything_is_written(self) -> None:
        order = self.awaiting_order()
        with self.assertRaises(ValidationError):
            self._record(order, phone_number="12345")
        self.assertEqual(Payment.objects.count(), 0)
