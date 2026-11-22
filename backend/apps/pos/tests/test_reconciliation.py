import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase
from django.utils import timezone

from apps.pos import payment_flow, services
from apps.pos.manual_mpesa import record_manual_mpesa_payment
from apps.pos.models import AuditEvent, Order, Outbox, Payment, PrintJob
from apps.pos.payment_states import PaymentEvent
from apps.pos.providers.mock_mpesa import MockLedger, MockLedgerSource, MockMpesaProvider, Outcome
from apps.pos.reconciliation import (
    ReconcileOutcome,
    is_near_miss,
    manager_confirm_payment,
    manager_defer_payment,
    manager_reject_payment,
    reconcile_payment,
    reconcile_pending,
)
from apps.pos.tests.base import PosTestCase

FOUR_EVENTS = [
    "order.paid_pending_verification",
    "payment.pending_verification",
    "order.paid",
    "payment.confirmed",
]


class NearMissTests(SimpleTestCase):
    def test_accepts_single_typos(self) -> None:
        for entered, recorded in [
            ("QGH7XXXXXX", "QGH7XXXXXX"),
            ("QGH7XXXXXX", "QGH7XXXXXY"),  # one wrong character
            ("QGH7XXXXX0", "QGH7XXXXXO"),  # zero typed for the letter O
            ("QGH7XXXXXI", "QGH7XXXXX1"),  # I and 1
            ("QGH7XXXXAB", "QGH7XXXXBA"),  # adjacent swap
        ]:
            with self.subTest(entered=entered, recorded=recorded):
                self.assertTrue(is_near_miss(entered, recorded))

    def test_rejects_bigger_differences(self) -> None:
        for entered, recorded in [
            ("QGH7XXXXXX", "QGH7XXXXYY"),  # two wrong characters
            ("QGH7XXXX", "QGH7XXXXXX"),  # different lengths
            ("QGH7XXXXXA", "AGH7XXXXXQ"),  # swap of non-adjacent characters
        ]:
            with self.subTest(entered=entered, recorded=recorded):
                self.assertFalse(is_near_miss(entered, recorded))


class ReconcileBase(PosTestCase):
    def setUp(self) -> None:
        self.ledger = MockLedger()
        self.source = MockLedgerSource(self.ledger)

    def manual(self, code: str = "QGH7XXXXXX", order: Order | None = None) -> Payment:
        payment, _ = record_manual_mpesa_payment(
            order=order or self.awaiting_order(),
            receipt_code=code,
            amount=Decimal("350.00"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        return payment

    def till_payment(self, code: str, amount: str = "350.00", minutes_ago: int = 0) -> None:
        self.ledger.record_till_payment(
            amount=Decimal(amount),
            phone_number="254712345678",
            receipt_code=code,
            occurred_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def events(self) -> list[str]:
        return list(Outbox.objects.order_by("sequence").values_list("event_type", flat=True))

    def order_of(self, payment: Payment) -> Order:
        return Order.objects.get(pk=payment.order_id)


class ReconcileTests(ReconcileBase):
    def test_exact_match_auto_confirms_without_a_manager(self) -> None:
        payment = self.manual()
        self.till_payment("QGH7XXXXXX")

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.CONFIRMED)
        payment.refresh_from_db()
        order = self.order_of(payment)
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertIsNone(payment.confirmed_by)
        self.assertIsNotNone(payment.last_reconciled_at)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(PrintJob.objects.filter(order=order).count(), 1)
        self.assertEqual(self.events(), FOUR_EVENTS)

    def test_reconciling_twice_changes_nothing(self) -> None:
        payment = self.manual()
        self.till_payment("QGH7XXXXXX")
        reconcile_payment(payment, self.source)

        again = reconcile_payment(payment, self.source)

        self.assertEqual(again.outcome, ReconcileOutcome.SKIPPED)
        self.assertEqual(self.events(), FOUR_EVENTS)

    def test_code_found_with_a_different_amount_goes_to_review(self) -> None:
        payment = self.manual()
        self.till_payment("QGH7XXXXXX", amount="300.00")

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.NEEDS_REVIEW)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(payment.review_reason, Payment.ReviewReason.FUZZY_MATCH)
        self.assertEqual(payment.suggested_receipt_code, "QGH7XXXXXX")
        self.assertIn("300", payment.reconciliation_note)
        self.assertEqual(self.order_of(payment).status, Order.Status.PAID_PENDING_VERIFICATION)

    def test_near_match_goes_to_review_and_a_manager_confirms_with_one_tap(self) -> None:
        payment = self.manual("QGH7XXXXX0")  # the cashier typed a zero
        self.till_payment("QGH7XXXXXO")  # the SMS had the letter O

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.NEEDS_REVIEW)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(payment.review_reason, Payment.ReviewReason.FUZZY_MATCH)
        self.assertEqual(payment.suggested_receipt_code, "QGH7XXXXXO")
        self.assertEqual(self.order_of(payment).status, Order.Status.PAID_PENDING_VERIFICATION)

        confirmed = manager_confirm_payment(payment=payment, manager=self.manager)

        self.assertEqual(confirmed.to_status, Payment.Status.CONFIRMED)
        payment.refresh_from_db()
        self.assertEqual(payment.mpesa_receipt_code, "QGH7XXXXXO")
        self.assertEqual(payment.confirmed_by_id, self.manager.pk)
        self.assertEqual(self.order_of(payment).status, Order.Status.PAID)
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type="payment.transition",
                actor=self.manager,
                target_id=str(payment.pk),
            ).exists()
        )

    def test_two_typos_is_not_a_near_match(self) -> None:
        payment = self.manual("QGH7XXXXXX")
        self.till_payment("QGH7XXXXYY")

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.STILL_PENDING)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)

    def test_near_record_outside_the_time_window_is_ignored(self) -> None:
        payment = self.manual("QGH7XXXXXX")
        self.till_payment("QGH7XXXXXY", minutes_ago=120)

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.STILL_PENDING)

    def test_no_match_stays_pending_then_goes_to_review_and_is_never_reversed(self) -> None:
        payment = self.manual()

        early = reconcile_payment(payment, self.source, now=timezone.now() + timedelta(hours=1))
        self.assertEqual(early.outcome, ReconcileOutcome.STILL_PENDING)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)
        self.assertNotEqual(payment.reconciliation_note, "")

        late = reconcile_payment(payment, self.source, now=timezone.now() + timedelta(hours=25))
        self.assertEqual(late.outcome, ReconcileOutcome.NEEDS_REVIEW)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(payment.review_reason, Payment.ReviewReason.NO_MATCH)
        self.assertEqual(self.order_of(payment).status, Order.Status.PAID_PENDING_VERIFICATION)

    def test_a_record_claimed_by_another_payment_is_not_suggested(self) -> None:
        first = self.manual("QGH7XXXXXX")
        second = self.manual("QGH7XXXXXY")
        self.till_payment("QGH7XXXXXY")

        self.assertEqual(
            reconcile_payment(first, self.source).outcome, ReconcileOutcome.STILL_PENDING
        )
        self.assertEqual(reconcile_payment(second, self.source).outcome, ReconcileOutcome.CONFIRMED)

    def test_unreachable_records_change_nothing(self) -> None:
        payment = self.manual()
        self.till_payment("QGH7XXXXXX")
        self.source.online = False

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.UNAVAILABLE)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)

    def test_code_already_held_by_another_order_goes_to_review_as_a_duplicate(self) -> None:
        payment = self.manual("QGH7XXXXXX")
        other_order = self.awaiting_order()
        other = Payment.objects.create(
            order=other_order,
            method=Payment.Method.MPESA_STK,
            status=Payment.Status.CONFIRMED,
            amount=other_order.total,
            mpesa_receipt_code="QGH7XXXXXX",
            recorded_by=self.user,
        )
        self.till_payment("QGH7XXXXXX")

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.NEEDS_REVIEW)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(payment.review_reason, Payment.ReviewReason.CROSS_METHOD_DUPLICATE)
        self.assertEqual(payment.duplicate_of_id, other.pk)

    def test_stuck_stk_then_manual_entry_settles_once(self) -> None:
        provider = MockMpesaProvider(self.ledger)
        order = self.awaiting_order()
        stk, _ = payment_flow.start_stk_payment(
            provider=provider,
            order=order,
            phone_number="0712345678",
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        payment_flow.time_out_payment(stk)
        attempt = stk.attempts.get()
        callback = provider.simulate(attempt.checkout_request_id, Outcome.SUCCESS)  # money moved
        code = self.ledger.get(attempt.checkout_request_id).receipt_code
        manual, _ = record_manual_mpesa_payment(
            order=order,
            receipt_code=code,
            amount=Decimal("350.00"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )

        # The late STK callback arrives: the order is already being settled manually.
        payment_flow.handle_provider_event(provider.parse_callback(callback))
        stk.refresh_from_db()
        self.assertEqual(stk.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(stk.review_reason, Payment.ReviewReason.CROSS_METHOD_DUPLICATE)
        self.assertEqual(stk.duplicate_of_id, manual.pk)

        # The manual entry reconciles against the same transaction, then the manager rejects
        # the duplicate record: one sale, one fulfillment, nothing double counted.
        result = reconcile_payment(manual, self.source)
        self.assertEqual(result.outcome, ReconcileOutcome.CONFIRMED)
        manager_reject_payment(
            payment=stk, manager=self.manager, reason="Same M-PESA transaction as manual entry"
        )

        for payment in (stk, manual):
            payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(manual.status, Payment.Status.CONFIRMED)
        self.assertEqual(stk.status, Payment.Status.FAILED)
        self.assertEqual(
            Payment.objects.filter(order=order, status=Payment.Status.CONFIRMED).count(), 1
        )
        self.assertEqual(PrintJob.objects.filter(order=order).count(), 1)
        self.assertEqual(self.events(), FOUR_EVENTS)

    def test_reconcile_pending_reports_counts_and_stops_when_offline(self) -> None:
        self.manual("QGH7XXXXXX")
        self.manual("QGH7AAAAAA")
        self.till_payment("QGH7XXXXXX")

        report = reconcile_pending(self.source)

        self.assertEqual(
            (report.checked, report.confirmed, report.still_pending, report.needs_review),
            (2, 1, 1, 0),
        )
        self.assertFalse(report.unavailable)

        self.source.online = False
        offline = reconcile_pending(self.source)
        self.assertTrue(offline.unavailable)
        self.assertEqual(offline.checked, 0)


class ManagerActionTests(ReconcileBase):
    def flagged(self, reason: Payment.ReviewReason = Payment.ReviewReason.NO_MATCH) -> Payment:
        payment = self.manual()
        payment_flow.apply_event(
            payment, PaymentEvent.FLAGGED_FOR_REVIEW, review_reason=reason.value
        )
        payment.refresh_from_db()
        return payment

    def test_only_a_manager_can_act(self) -> None:
        payment = self.flagged()
        with self.assertRaises(PermissionDenied):
            manager_confirm_payment(payment=payment, manager=self.user)
        with self.assertRaises(PermissionDenied):
            manager_reject_payment(payment=payment, manager=self.user, reason="x")
        with self.assertRaises(PermissionDenied):
            manager_defer_payment(payment=payment, manager=self.user)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)

    def test_a_duplicate_payment_cannot_be_confirmed(self) -> None:
        payment = self.flagged(Payment.ReviewReason.CROSS_METHOD_DUPLICATE)
        with self.assertRaises(ValidationError):
            manager_confirm_payment(payment=payment, manager=self.manager)

    def test_rejecting_the_last_live_payment_reopens_the_order_for_cash(self) -> None:
        payment = self.flagged()

        result = manager_reject_payment(
            payment=payment, manager=self.manager, reason="Customer never paid"
        )

        self.assertEqual(result.to_status, Payment.Status.FAILED)
        order = self.order_of(payment)
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertTrue(AuditEvent.objects.filter(event_type="order.reopened_unpaid").exists())

        services.confirm_cash_payment(
            order=order,
            tendered=Decimal("400"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_deferring_returns_the_payment_to_pending_verification(self) -> None:
        payment = self.flagged()

        result = manager_defer_payment(payment=payment, manager=self.manager)

        self.assertEqual(result.to_status, Payment.Status.PENDING_VERIFICATION)

    def test_rejecting_needs_a_reason(self) -> None:
        payment = self.flagged()
        with self.assertRaises(ValidationError):
            manager_reject_payment(payment=payment, manager=self.manager, reason="  ")
