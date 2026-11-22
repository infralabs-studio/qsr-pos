import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.pos import services
from apps.pos.backfill import BackfillLine, backfill_order
from apps.pos.models import AuditEvent, MenuItem, Order, Outbox, Payment, PrintJob, Shift
from apps.pos.providers.mock_mpesa import MockLedger, MockLedgerSource
from apps.pos.reconciliation import ReconcileOutcome, reconcile_payment
from apps.pos.tests.base import PosTestCase


class BackfillTests(PosTestCase):
    def setUp(self) -> None:
        # The shift has been open for three hours: the outage happened inside it.
        Shift.objects.filter(pk=self.shift.pk).update(opened_at=timezone.now() - timedelta(hours=3))
        self.shift.refresh_from_db()
        self.occurred = timezone.now() - timedelta(hours=2)

    def _backfill(self, **overrides: Any) -> tuple[Order, bool]:
        params: dict[str, Any] = {
            "shift": self.shift,
            "manager": self.manager,
            "reason": "Power cut, paper tickets",
            "occurred_at": self.occurred,
            "lines": [BackfillLine(self.item)],
            "payment_key": uuid.uuid4(),
            "cash_tendered": Decimal("400.00"),
        }
        params.update(overrides)
        return backfill_order(**params)

    def _events(self) -> list[str]:
        return list(Outbox.objects.order_by("sequence").values_list("event_type", flat=True))

    def test_cash_backfill_is_recorded_with_its_real_time_and_no_kitchen_ticket(self) -> None:
        order, created = self._backfill()

        self.assertTrue(created)
        self.assertTrue(order.is_backfilled)
        self.assertEqual(order.order_number, 1)
        self.assertEqual(str(order), "B-1 (paid)")
        self.assertEqual(order.occurred_at, self.occurred)
        self.assertEqual(order.backfilled_by_id, self.manager.pk)
        payment = order.payments.get()
        self.assertEqual(payment.occurred_at, self.occurred)
        self.assertEqual(payment.shift_id, self.shift.pk)
        self.assertEqual(PrintJob.objects.count(), 0)
        self.assertEqual(self._events(), ["order.paid", "payment.confirmed"])
        payload = Outbox.objects.get(event_type="order.paid").payload
        self.assertTrue(payload["is_backfilled"])
        self.assertTrue(
            AuditEvent.objects.filter(event_type="order.backfilled", actor=self.manager).exists()
        )

    def test_mpesa_backfill_reconciles_later_without_printing(self) -> None:
        order, _ = self._backfill(cash_tendered=None, mpesa_code="QGH7XXXXXX")
        payment = order.payments.get()
        self.assertEqual(order.status, Order.Status.PAID_PENDING_VERIFICATION)
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)

        ledger = MockLedger()
        ledger.record_till_payment(
            amount=Decimal("350.00"),
            phone_number="254712345678",
            receipt_code="QGH7XXXXXX",
            occurred_at=self.occurred,
        )
        result = reconcile_payment(payment, MockLedgerSource(ledger))

        self.assertEqual(result.outcome, ReconcileOutcome.CONFIRMED)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(PrintJob.objects.count(), 0)

    def test_backfilled_orders_do_not_consume_live_numbers(self) -> None:
        live = services.create_order(shift=self.shift, created_by=self.user)
        order, _ = self._backfill()
        another = services.create_order(shift=self.shift, created_by=self.user)

        self.assertEqual((live.order_number, order.order_number), (1, 1))
        self.assertEqual(another.order_number, 2)

    def test_only_a_manager_can_backfill(self) -> None:
        with self.assertRaises(PermissionDenied):
            self._backfill(manager=self.user)
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_requests_are_rejected_and_write_nothing(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "no reason": {"reason": "  "},
            "reason too long": {"reason": "x" * 300},
            "no lines": {"lines": []},
            "cash and mpesa": {"mpesa_code": "QGH7XXXXXX"},
            "neither cash nor mpesa": {"cash_tendered": None},
            "in the future": {"occurred_at": timezone.now() + timedelta(hours=1)},
            "before the shift opened": {"occurred_at": timezone.now() - timedelta(hours=4)},
            "naive time": {"occurred_at": datetime(2026, 1, 1, 12, 0)},
        }
        for name, overrides in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                self._backfill(**overrides)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_closed_shift_is_rejected(self) -> None:
        Shift.objects.filter(pk=self.shift.pk).update(status=Shift.Status.CLOSED)
        with self.assertRaises(ValidationError):
            self._backfill()
        self.assertEqual(Order.objects.count(), 0)

    def test_double_submit_with_the_same_ids_creates_one_order(self) -> None:
        order_id, key = uuid.uuid4(), uuid.uuid4()

        first, created_first = self._backfill(order_id=order_id, payment_key=key)
        second, created_second = self._backfill(order_id=order_id, payment_key=key)

        self.assertEqual((created_first, created_second), (True, False))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Order.objects.filter(is_backfilled=True).count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Outbox.objects.count(), 2)

    def test_failure_after_the_order_is_created_rolls_everything_back(self) -> None:
        with self.assertRaises(ValidationError):
            self._backfill(cash_tendered=Decimal("100.00"))  # less than the total

        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Outbox.objects.count(), 0)
        self.assertFalse(AuditEvent.objects.filter(event_type="order.backfilled").exists())

    def test_an_item_that_is_now_unavailable_can_still_be_backfilled(self) -> None:
        MenuItem.objects.filter(pk=self.item.pk).update(is_available=False)
        item = MenuItem.objects.get(pk=self.item.pk)

        order, _ = self._backfill(lines=[BackfillLine(item)])

        self.assertEqual(order.status, Order.Status.PAID)
