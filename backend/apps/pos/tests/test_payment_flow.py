import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import User
from apps.pos import payment_flow, services
from apps.pos.models import (
    AuditEvent,
    MenuCategory,
    MenuItem,
    Order,
    Outbox,
    Payment,
    PaymentAttempt,
    PrintJob,
    Shift,
)
from apps.pos.payment_flow import TransitionResult, TransitionStatus
from apps.pos.payment_states import (
    TRANSITIONS,
    PaymentEvent,
    is_already_applied,
    target_status,
)
from apps.pos.providers.base import PaymentProvider, ProviderUnavailable
from apps.pos.providers.cash import CashProvider
from apps.pos.providers.mock_mpesa import MockMpesaProvider, Outcome


class StateTableTests(SimpleTestCase):
    def test_table_only_uses_real_statuses_and_events(self) -> None:
        statuses = set(Payment.Status.values)
        events = {e.value for e in PaymentEvent}
        for (source, event), destination in TRANSITIONS.items():
            self.assertIn(source, statuses)
            self.assertIn(event, events)
            self.assertIn(destination, statuses)

    def test_every_status_appears_in_the_table(self) -> None:
        seen = {source for source, _ in TRANSITIONS} | set(TRANSITIONS.values())
        self.assertEqual(seen, set(Payment.Status.values))

    def test_confirmed_is_terminal(self) -> None:
        self.assertEqual([key for key in TRANSITIONS if key[0] == "confirmed"], [])
        self.assertIsNone(target_status("confirmed", PaymentEvent.TIMED_OUT))

    def test_replays_are_recognised_as_already_applied(self) -> None:
        self.assertTrue(is_already_applied("confirmed", PaymentEvent.SUCCEEDED))
        self.assertTrue(is_already_applied("manager_review", PaymentEvent.SUCCEEDED))
        self.assertFalse(is_already_applied("confirmed", PaymentEvent.TIMED_OUT))


class PaymentFlowTests(TestCase):
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
        cls.item = MenuItem.objects.create(category=category, name="Chips", price=Decimal("350"))

    # ---- helpers -------------------------------------------------------------------------

    def _awaiting_order(self) -> Order:
        order = services.create_order(shift=self.shift, created_by=self.user)
        services.add_item(order=order, menu_item=self.item)
        return services.charge_order(order=order)

    def _stk(self, provider: MockMpesaProvider) -> tuple[Payment, PaymentAttempt]:
        payment, _ = payment_flow.start_stk_payment(
            provider=provider,
            order=self._awaiting_order(),
            phone_number="0712 345 678",
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        return payment, payment.attempts.get()

    def _deliver(
        self, provider: MockMpesaProvider, attempt: PaymentAttempt, outcome: Outcome
    ) -> TransitionResult | None:
        payload = provider.simulate(attempt.checkout_request_id, outcome)
        return payment_flow.handle_provider_event(provider.parse_callback(payload))

    def _order_status(self, payment: Payment) -> str:
        return Order.objects.get(pk=payment.order_id).status

    def _outbox_events(self) -> list[str]:
        return list(Outbox.objects.order_by("sequence").values_list("event_type", flat=True))

    # ---- STK outcomes --------------------------------------------------------------------

    def test_stk_success_pays_the_order(self) -> None:
        provider = MockMpesaProvider()
        payment, attempt = self._stk(provider)
        self.assertEqual(payment.phone_number, "254712345678")

        result = self._deliver(provider, attempt, Outcome.SUCCESS)

        assert result is not None
        self.assertEqual(result.status, TransitionStatus.APPLIED)
        payment.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertTrue(payment.mpesa_receipt_code.startswith("Q"))
        self.assertEqual(attempt.status, PaymentAttempt.Status.SUCCEEDED)
        self.assertEqual(attempt.result_code, 0)
        self.assertEqual(self._order_status(payment), Order.Status.PAID)
        self.assertEqual(PrintJob.objects.filter(order_id=payment.order_id).count(), 1)
        self.assertEqual(self._outbox_events(), ["order.paid", "payment.confirmed"])

    def test_duplicate_callback_changes_nothing(self) -> None:
        provider = MockMpesaProvider()
        payment, attempt = self._stk(provider)
        event = provider.parse_callback(
            provider.simulate(attempt.checkout_request_id, Outcome.SUCCESS)
        )

        first = payment_flow.handle_provider_event(event)
        second = payment_flow.handle_provider_event(event)

        assert first is not None and second is not None
        self.assertEqual(first.status, TransitionStatus.APPLIED)
        self.assertEqual(second.status, TransitionStatus.NOOP)
        attempt.refresh_from_db()
        self.assertEqual(attempt.callback_count, 2)
        self.assertEqual(Payment.objects.filter(order_id=payment.order_id).count(), 1)
        self.assertEqual(self._outbox_events(), ["order.paid", "payment.confirmed"])
        self.assertEqual(PrintJob.objects.filter(order_id=payment.order_id).count(), 1)

    def test_failed_and_cancelled_outcomes_leave_the_order_unpaid(self) -> None:
        expected = {
            Outcome.INSUFFICIENT_FUNDS: Payment.Status.FAILED,
            Outcome.WRONG_PIN: Payment.Status.FAILED,
            Outcome.UNREACHABLE: Payment.Status.FAILED,
            Outcome.CANCELLED: Payment.Status.CANCELLED,
        }
        for outcome, status in expected.items():
            with self.subTest(outcome=outcome):
                provider = MockMpesaProvider()
                payment, attempt = self._stk(provider)
                self._deliver(provider, attempt, outcome)
                payment.refresh_from_db()
                self.assertEqual(payment.status, status)
                self.assertEqual(self._order_status(payment), Order.Status.AWAITING_PAYMENT)
        self.assertEqual(self._outbox_events(), [])

    def test_contradictory_success_after_failure_goes_to_manager_review(self) -> None:
        provider = MockMpesaProvider()
        payment, attempt = self._stk(provider)

        self._deliver(provider, attempt, Outcome.INSUFFICIENT_FUNDS)
        result = self._deliver(provider, attempt, Outcome.SUCCESS)

        assert result is not None
        self.assertEqual(result.status, TransitionStatus.APPLIED)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(self._order_status(payment), Order.Status.AWAITING_PAYMENT)
        self.assertEqual(self._outbox_events(), [])

    def test_timeout_then_late_success_confirms_when_order_is_unsettled(self) -> None:
        provider = MockMpesaProvider()
        payment, attempt = self._stk(provider)

        timed_out = payment_flow.time_out_payment(payment)
        self.assertEqual(timed_out.to_status, Payment.Status.TIMED_OUT)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.TIMED_OUT)
        self.assertIsNone(attempt.result_code)

        self._deliver(provider, attempt, Outcome.SUCCESS)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(self._order_status(payment), Order.Status.PAID)

    def test_late_success_after_cash_goes_to_manager_review(self) -> None:
        provider = MockMpesaProvider()
        payment, attempt = self._stk(provider)
        payment_flow.time_out_payment(payment)
        cash, _ = services.confirm_cash_payment(
            order=Order.objects.get(pk=payment.order_id),
            tendered=Decimal("500"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )

        self._deliver(provider, attempt, Outcome.SUCCESS)

        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(payment.review_reason, Payment.ReviewReason.CROSS_METHOD_DUPLICATE)
        self.assertEqual(payment.duplicate_of_id, cash.pk)
        self.assertEqual(self._order_status(payment), Order.Status.PAID)
        # One fulfillment: only the cash payment produced order/payment events and a ticket.
        self.assertEqual(self._outbox_events(), ["order.paid", "payment.confirmed"])
        self.assertEqual(PrintJob.objects.filter(order_id=payment.order_id).count(), 1)
        self.assertEqual(Payment.objects.filter(order_id=payment.order_id).count(), 2)

    # ---- guards --------------------------------------------------------------------------

    def test_illegal_transition_is_rejected_and_logged(self) -> None:
        provider = MockMpesaProvider()
        payment, attempt = self._stk(provider)
        self._deliver(provider, attempt, Outcome.SUCCESS)

        result = payment_flow.apply_event(payment, PaymentEvent.TIMED_OUT)

        self.assertEqual(result.status, TransitionStatus.REJECTED)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type="payment.transition_rejected", target_id=str(payment.pk)
            ).exists()
        )

    def test_offline_provider_fails_the_prompt_and_cash_still_works(self) -> None:
        provider = MockMpesaProvider()
        provider.online = False
        order = self._awaiting_order()

        with self.assertRaises(ProviderUnavailable):
            payment_flow.start_stk_payment(
                provider=provider,
                order=order,
                phone_number="0712345678",
                recorded_by=self.user,
                idempotency_key=uuid.uuid4(),
            )

        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertEqual(self._order_status(payment), Order.Status.AWAITING_PAYMENT)

        services.confirm_cash_payment(
            order=order,
            tendered=Decimal("500"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        self.assertEqual(self._order_status(payment), Order.Status.PAID)

    def test_start_stk_is_idempotent_on_the_payment_key(self) -> None:
        provider = MockMpesaProvider()
        order = self._awaiting_order()
        key = uuid.uuid4()

        first, created_first = payment_flow.start_stk_payment(
            provider=provider,
            order=order,
            phone_number="0712345678",
            recorded_by=self.user,
            idempotency_key=key,
        )
        second, created_second = payment_flow.start_stk_payment(
            provider=provider,
            order=order,
            phone_number="0712345678",
            recorded_by=self.user,
            idempotency_key=key,
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PaymentAttempt.objects.filter(payment=first).count(), 1)
        self.assertEqual(len(provider.ledger.entries), 1)

    def test_invalid_phone_number_is_rejected_before_anything_is_written(self) -> None:
        order = self._awaiting_order()
        with self.assertRaises(ValidationError):
            payment_flow.start_stk_payment(
                provider=MockMpesaProvider(),
                order=order,
                phone_number="12345",
                recorded_by=self.user,
                idempotency_key=uuid.uuid4(),
            )
        self.assertEqual(Payment.objects.count(), 0)

    # ---- providers and manual-path transitions -------------------------------------------

    def test_cash_and_mock_mpesa_both_implement_the_provider_interface(self) -> None:
        cash = CashProvider()
        mpesa = MockMpesaProvider()
        self.assertIsInstance(cash, PaymentProvider)
        self.assertIsInstance(mpesa, PaymentProvider)
        payment = Payment(amount=Decimal("100"))
        initiated = cash.initiate(payment)
        assert initiated.event is not None
        self.assertEqual(initiated.event.event, PaymentEvent.SUCCEEDED)
        self.assertIsNone(cash.query_status(PaymentAttempt()))

    def test_manual_payment_review_and_confirmation_path(self) -> None:
        order = self._awaiting_order()
        Order.objects.filter(pk=order.pk).update(status=Order.Status.PAID_PENDING_VERIFICATION)
        payment = Payment.objects.create(
            order=order,
            method=Payment.Method.MPESA_MANUAL,
            status=Payment.Status.PENDING_VERIFICATION,
            amount=order.total,
            mpesa_receipt_code="QGH7XXXXXX",
            recorded_by=self.user,
        )

        flagged = payment_flow.apply_event(
            payment,
            PaymentEvent.FLAGGED_FOR_REVIEW,
            review_reason=Payment.ReviewReason.FUZZY_MATCH.value,
        )
        self.assertEqual(flagged.to_status, Payment.Status.MANAGER_REVIEW)

        confirmed = payment_flow.apply_event(
            payment, PaymentEvent.MANAGER_CONFIRMED, actor=self.manager
        )
        self.assertEqual(confirmed.to_status, Payment.Status.CONFIRMED)
        payment.refresh_from_db()
        self.assertEqual(payment.confirmed_by_id, self.manager.pk)
        self.assertEqual(self._order_status(payment), Order.Status.PAID)
        self.assertEqual(self._outbox_events(), ["order.paid", "payment.confirmed"])

        replay = payment_flow.apply_event(
            payment, PaymentEvent.MANAGER_CONFIRMED, actor=self.manager
        )
        self.assertEqual(replay.status, TransitionStatus.NOOP)

    def test_mock_ledger_records_direct_till_payments(self) -> None:
        provider = MockMpesaProvider()
        entry = provider.ledger.record_till_payment(
            amount=Decimal("350"), phone_number="254712345678", receipt_code="QGH7XXXXXX"
        )
        self.assertTrue(entry.succeeded)
        self.assertIs(provider.ledger.find_by_receipt("QGH7XXXXXX"), entry)
        self.assertIsNone(provider.ledger.find_by_receipt("NOPE000000"))
