from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from apps.accounts.models import User
from apps.pos.models import AuditEvent, ImmutableRecordError, Order, Payment, Shift


class SchemaRuleTests(TestCase):
    user: User
    shift: Shift
    order: Order

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="cashier1", password="test-pass-123")
        cls.shift = Shift.objects.create(opened_by=cls.user, opening_float=Decimal("1000"))
        cls.order = Order.objects.create_for_shift(cls.shift, cls.user)

    def test_payment_has_all_required_states(self) -> None:
        expected = {
            "pending",
            "confirmed",
            "failed",
            "timed_out",
            "cancelled",
            "pending_verification",
            "manager_review",
        }
        self.assertEqual(set(Payment.Status.values), expected)

    def test_order_numbers_are_sequential_per_shift(self) -> None:
        second = Order.objects.create_for_shift(self.shift, self.user)
        self.assertEqual(self.order.order_number, 1)
        self.assertEqual(second.order_number, 2)

    def test_payment_inherits_shift_from_order(self) -> None:
        payment = Payment.objects.create(
            order=self.order,
            method=Payment.Method.CASH,
            amount=Decimal("500.00"),
            recorded_by=self.user,
        )
        self.assertEqual(payment.shift_id, self.shift.id)

    def test_payment_shift_must_match_order_shift(self) -> None:
        other = Shift.objects.create(opened_by=self.user, status=Shift.Status.CLOSED)
        payment = Payment(
            order=self.order,
            shift=other,
            method=Payment.Method.CASH,
            amount=Decimal("500.00"),
            recorded_by=self.user,
        )
        with self.assertRaises(ValidationError):
            payment.save()

    def test_only_one_open_shift(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            Shift.objects.create(opened_by=self.user)

    def test_audit_events_are_append_only(self) -> None:
        event = AuditEvent.objects.create(event_type="order.created", actor=self.user)
        event.reason = "tampered"
        with self.assertRaises(ImmutableRecordError):
            event.save()
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.all().delete()

    def test_audit_table_rejects_raw_sql_updates(self) -> None:
        AuditEvent.objects.create(event_type="order.created", actor=self.user)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE pos_auditevent SET reason = 'tampered'")
