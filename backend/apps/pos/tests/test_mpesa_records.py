import json
import urllib.parse
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from typing import Any
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.cloudmock.models import MpesaTransaction
from apps.pos.manual_mpesa import record_manual_mpesa_payment
from apps.pos.models import Order, Payment, PrintJob
from apps.pos.providers.base import ProviderUnavailable
from apps.pos.providers.mpesa_records import HttpTransactionSource
from apps.pos.reconciliation import ReconcileOutcome, reconcile_payment
from apps.pos.tests.base import PosTestCase

CLOUD = {"CLOUD_MOCK_ENABLED": True, "CLOUD_API_KEY": "test-key"}
BASE_PATH = "/cloud/mpesa/transactions/"
BASE_URL = "http://cloud.test" + BASE_PATH


class ClientRecordsSource(HttpTransactionSource):
    """Production parsing and error handling; only the transport is swapped for Django's test
    client, so nothing touches the network."""

    def _get(self, url: str) -> Any:
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        response = Client().get(path, HTTP_AUTHORIZATION=f"Bearer {self.api_key}")
        if response.status_code != 200:
            raise ProviderUnavailable(f"HTTP {response.status_code}")
        return response.json()


def add_record(code: str, amount: str = "350.00", minutes_ago: int = 0) -> MpesaTransaction:
    return MpesaTransaction.objects.create(
        receipt_code=code,
        amount=Decimal(amount),
        phone_number="254712345678",
        occurred_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


@override_settings(**CLOUD)
class CloudMpesaViewTests(TestCase):
    def _call(
        self, method: str, path: str, body: object = None, key: str | None = "test-key"
    ) -> Any:
        headers = {"Authorization": f"Bearer {key}"} if key is not None else None
        client = Client()
        if method == "POST":
            return client.post(
                path, data=json.dumps(body), content_type="application/json", headers=headers
            )
        return client.get(path, headers=headers)

    @override_settings(CLOUD_MOCK_ENABLED=False)
    def test_disabled_mock_is_a_404(self) -> None:
        for method, path in [
            ("GET", BASE_PATH),
            ("POST", BASE_PATH),
            ("GET", BASE_PATH + "QGH7XXXXXX/"),
        ]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self._call(method, path, {"amount": "1"}).status_code, 404)

    def test_missing_or_wrong_key_is_unauthorized(self) -> None:
        for key in (None, "wrong"):
            with self.subTest(key=key):
                response = self._call("GET", BASE_PATH + "QGH7XXXXXX/", key=key)
                self.assertEqual(response.status_code, 401)

    def test_create_then_lookup(self) -> None:
        created = self._call(
            "POST",
            BASE_PATH,
            {"receipt_code": "qgh7xxxxxx", "amount": "350.00", "phone_number": "254712345678"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["transaction"]["receipt_code"], "QGH7XXXXXX")

        found = self._call("GET", BASE_PATH + "QGH7XXXXXX/").json()

        self.assertTrue(found["found"])
        self.assertEqual(found["transaction"]["amount"], "350.00")

    def test_a_receipt_code_is_generated_when_omitted(self) -> None:
        response = self._call("POST", BASE_PATH, {"amount": "120"})
        code = response.json()["transaction"]["receipt_code"]
        self.assertEqual(response.status_code, 201)
        self.assertTrue(len(code) == 10 and code.isalnum() and code == code.upper())

    def test_unknown_code_is_an_explicit_not_found(self) -> None:
        response = self._call("GET", BASE_PATH + "QGH7XXXXXX/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})

    def test_duplicate_receipt_code_is_a_409_and_keeps_the_original(self) -> None:
        self._call("POST", BASE_PATH, {"receipt_code": "QGH7XXXXXX", "amount": "350"})
        again = self._call("POST", BASE_PATH, {"receipt_code": "QGH7XXXXXX", "amount": "999"})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(MpesaTransaction.objects.get().amount, Decimal("350.00"))

    def test_invalid_create_bodies_are_rejected(self) -> None:
        bodies: list[dict[str, Any]] = [
            {},
            {"amount": "abc"},
            {"amount": "-5"},
            {"amount": "0"},
            {"amount": "10.123"},
            {"amount": "NaN"},
            {"amount": "350", "receipt_code": "BAD"},
            {"amount": "350", "occurred_at": "not-a-date"},
            {"amount": "350", "occurred_at": "2026-01-01T10:00:00"},  # no timezone
            {"amount": "350", "phone_number": "1" * 16},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self._call("POST", BASE_PATH, body).status_code, 400)
        self.assertEqual(MpesaTransaction.objects.count(), 0)

    def test_a_non_json_body_is_a_400(self) -> None:
        response = Client().post(
            BASE_PATH,
            data="not json",
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-key",
        )
        self.assertEqual(response.status_code, 400)

    def test_candidates_returns_only_records_in_the_window(self) -> None:
        now = timezone.now()
        for code, delta in [
            ("QGH7AAAAAA", timedelta(hours=-2)),
            ("QGH7BBBBBB", timedelta(minutes=-10)),
            ("QGH7CCCCCC", timedelta(hours=2)),
        ]:
            MpesaTransaction.objects.create(
                receipt_code=code, amount=Decimal("350"), occurred_at=now + delta
            )
        query = urllib.parse.urlencode(
            {
                "start": (now - timedelta(minutes=30)).isoformat(),
                "end": (now + timedelta(minutes=30)).isoformat(),
            }
        )

        response = self._call("GET", f"{BASE_PATH}?{query}")

        codes = [t["receipt_code"] for t in response.json()["transactions"]]
        self.assertEqual(codes, ["QGH7BBBBBB"])

    def test_candidates_need_a_valid_window(self) -> None:
        self.assertEqual(self._call("GET", BASE_PATH).status_code, 400)
        naive = urllib.parse.urlencode(
            {"start": "2026-01-01T10:00:00", "end": "2026-01-01T11:00:00"}
        )
        self.assertEqual(self._call("GET", f"{BASE_PATH}?{naive}").status_code, 400)


@override_settings(**CLOUD)
class HttpTransactionSourceTests(TestCase):
    def setUp(self) -> None:
        self.source = ClientRecordsSource(BASE_URL, "test-key")

    def test_lookup_returns_a_ledger_transaction(self) -> None:
        add_record("QGH7XXXXXX")

        found = self.source.lookup("QGH7XXXXXX")

        assert found is not None
        self.assertEqual(found.receipt_code, "QGH7XXXXXX")
        self.assertEqual(found.amount, Decimal("350.00"))
        self.assertEqual(found.phone_number, "254712345678")

    def test_lookup_of_an_unknown_code_is_none(self) -> None:
        self.assertIsNone(self.source.lookup("QGH7XXXXXX"))

    def test_candidates_are_limited_to_the_window(self) -> None:
        add_record("QGH7AAAAAA", minutes_ago=120)
        add_record("QGH7BBBBBB", minutes_ago=10)
        now = timezone.now()

        found = self.source.candidates(now - timedelta(minutes=30), now + timedelta(minutes=30))

        self.assertEqual([t.receipt_code for t in found], ["QGH7BBBBBB"])

    @override_settings(CLOUD_MOCK_ENABLED=False)
    def test_a_404_means_unavailable_never_no_match(self) -> None:
        with self.assertRaises(ProviderUnavailable):
            self.source.lookup("QGH7XXXXXX")
        now = timezone.now()
        with self.assertRaises(ProviderUnavailable):
            self.source.candidates(now, now)

    def test_a_wrong_api_key_is_unavailable(self) -> None:
        with self.assertRaises(ProviderUnavailable):
            ClientRecordsSource(BASE_URL, "wrong").lookup("QGH7XXXXXX")

    def test_malformed_responses_are_unavailable(self) -> None:
        now = timezone.now()
        with patch.object(ClientRecordsSource, "_get", return_value={"unexpected": 1}):
            with self.assertRaises(ProviderUnavailable):
                self.source.lookup("QGH7XXXXXX")
        incomplete = {"found": True, "transaction": {"receipt_code": "QGH7XXXXXX"}}
        with patch.object(ClientRecordsSource, "_get", return_value=incomplete):
            with self.assertRaises(ProviderUnavailable):
                self.source.lookup("QGH7XXXXXX")
        with patch.object(ClientRecordsSource, "_get", return_value={"transactions": "nope"}):
            with self.assertRaises(ProviderUnavailable):
                self.source.candidates(now, now)

    def test_a_refused_connection_is_unavailable(self) -> None:
        source = HttpTransactionSource("http://127.0.0.1:1" + BASE_PATH, "key", timeout=1)
        with self.assertRaises(ProviderUnavailable):
            source.lookup("QGH7XXXXXX")

    def test_non_http_urls_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            HttpTransactionSource("file:///etc/passwd", "key")


@override_settings(**CLOUD)
class ReconcileOverHttpTests(PosTestCase):
    def setUp(self) -> None:
        self.source = ClientRecordsSource(BASE_URL, "test-key")

    def _manual(self, code: str = "QGH7XXXXXX") -> Payment:
        payment, _ = record_manual_mpesa_payment(
            order=self.awaiting_order(),
            receipt_code=code,
            amount=Decimal("350.00"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        return payment

    def test_scenario_5_offline_entry_confirms_after_reconnect(self) -> None:
        payment = self._manual()
        order = Order.objects.get(pk=payment.order_id)

        # The terminal cannot reach the records. Even long past the grace period this must
        # change nothing: an outage is not a "no match".
        with override_settings(CLOUD_MOCK_ENABLED=False):
            down = reconcile_payment(payment, self.source, now=timezone.now() + timedelta(hours=30))
        self.assertEqual(down.outcome, ReconcileOutcome.UNAVAILABLE)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)
        self.assertEqual(order.status, Order.Status.PAID_PENDING_VERIFICATION)

        # The customer's payment is in the records; the connection comes back.
        add_record("QGH7XXXXXX")
        up = reconcile_payment(payment, self.source)

        self.assertEqual(up.outcome, ReconcileOutcome.CONFIRMED)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(PrintJob.objects.filter(order=order).count(), 1)

    def test_a_typo_found_over_http_goes_to_manager_review_with_a_suggestion(self) -> None:
        payment = self._manual("QGH7XXXXX0")  # the cashier typed a zero
        add_record("QGH7XXXXXO")  # the SMS had the letter O

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.NEEDS_REVIEW)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(payment.suggested_receipt_code, "QGH7XXXXXO")

    def test_no_record_yet_stays_pending(self) -> None:
        payment = self._manual()

        result = reconcile_payment(payment, self.source)

        self.assertEqual(result.outcome, ReconcileOutcome.STILL_PENDING)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)


class CommandTests(PosTestCase):
    @override_settings(MPESA_RECORDS_URL="")
    def test_reconcile_reports_when_it_is_disabled(self) -> None:
        out = StringIO()
        call_command("reconcile_mpesa", "--once", stdout=out)
        self.assertIn("not set", out.getvalue())

    @override_settings(**CLOUD, MPESA_RECORDS_URL=BASE_URL)
    def test_reconcile_once_confirms_waiting_payments(self) -> None:
        payment, _ = record_manual_mpesa_payment(
            order=self.awaiting_order(),
            receipt_code="QGH7XXXXXX",
            amount=Decimal("350.00"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        add_record("QGH7XXXXXX")
        out = StringIO()

        target = "apps.pos.management.commands.reconcile_mpesa.HttpTransactionSource"
        with patch(target, ClientRecordsSource):
            call_command("reconcile_mpesa", "--once", stdout=out)

        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertIn("confirmed 1", out.getvalue())

    @override_settings(
        CLOUD_MOCK_ENABLED=False, CLOUD_API_KEY="test-key", MPESA_RECORDS_URL=BASE_URL
    )
    def test_reconcile_once_with_unreachable_records_changes_nothing(self) -> None:
        payment, _ = record_manual_mpesa_payment(
            order=self.awaiting_order(),
            receipt_code="QGH7XXXXXX",
            amount=Decimal("350.00"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        out = StringIO()

        target = "apps.pos.management.commands.reconcile_mpesa.HttpTransactionSource"
        with patch(target, ClientRecordsSource):
            call_command("reconcile_mpesa", "--once", stdout=out)

        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)
        self.assertIn("unreachable", out.getvalue())

    @override_settings(DEBUG=True)
    def test_simulate_manual_mpesa_creates_a_pending_payment(self) -> None:
        out = StringIO()
        call_command("simulate_manual_mpesa", "QGH7XXXXXX", stdout=out)
        payment = Payment.objects.get()
        self.assertEqual(payment.status, Payment.Status.PENDING_VERIFICATION)
        self.assertEqual(payment.mpesa_receipt_code, "QGH7XXXXXX")
