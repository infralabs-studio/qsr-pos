import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.pos import payment_flow
from apps.pos.models import (
    AuditEvent,
    MenuCategory,
    MenuItem,
    ModifierGroup,
    ModifierOption,
    Order,
    Outbox,
    Payment,
    Shift,
)
from apps.pos.payment_states import PaymentEvent
from apps.pos.providers.factory import get_mock_provider, reset_mock_provider
from apps.pos.providers.mock_mpesa import Outcome
from apps.pos.providers.stk_callback import parse_stk_callback
from apps.pos.tests.base import PosTestCase

SETTINGS: dict[str, Any] = {
    "POS_PRICES_TAX_INCLUSIVE": True,
    "MPESA_PROVIDER": "mock",
    "STK_TIMEOUT_SECONDS": 90,
    "CLOUD_SYNC_URL": "",
    "MPESA_CALLBACK_TOKEN": "cb-secret-token",
}
CALLBACK_URL = "/api/mpesa/callback/cb-secret-token/"


@override_settings(**SETTINGS)
class ApiTestCase(PosTestCase):
    burger: MenuItem
    large: ModifierOption

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.burger = MenuItem.objects.create(
            category=cls.item.category, name="Beef Burger", price=Decimal("300.00")
        )
        size = ModifierGroup.objects.create(name="Size", min_select=1, max_select=1)
        cls.burger.modifier_groups.add(size)
        ModifierOption.objects.create(group=size, name="Regular", price_delta=Decimal("0"))
        cls.large = ModifierOption.objects.create(
            group=size, name="Large", price_delta=Decimal("80.00")
        )

    def setUp(self) -> None:
        cache.clear()  # the login throttle lives in the cache
        reset_mock_provider()
        self.client = APIClient()
        self.client.force_login(self.user)

    # ---- helpers -------------------------------------------------------------------------

    def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self.client.post(path, data or {}, format="json")

    def new_order(self, quantity: int = 1) -> str:
        order_id = str(uuid.uuid4())
        self.assertEqual(self.post("/api/orders/", {"id": order_id}).status_code, 201)
        added = self.post(
            f"/api/orders/{order_id}/items/",
            {"menu_item": str(self.item.pk), "quantity": quantity},
        )
        self.assertEqual(added.status_code, 201)
        return order_id

    def charged_order(self) -> str:
        order_id = self.new_order()
        self.assertEqual(self.post(f"/api/orders/{order_id}/charge/").status_code, 200)
        return order_id

    def pay_cash(self, order_id: str, tendered: str = "500.00", key: str | None = None) -> Any:
        return self.post(
            f"/api/orders/{order_id}/payments/cash/",
            {"tendered": tendered, "idempotency_key": key or str(uuid.uuid4())},
        )

    def pay_manual(self, order_id: str, code: str = "QGH7XXXXXX") -> Any:
        return self.post(
            f"/api/orders/{order_id}/payments/manual-mpesa/",
            {
                "receipt_code": code,
                "amount": "350.00",
                "phone_number": "0712345678",
                "idempotency_key": str(uuid.uuid4()),
            },
        )

    def start_stk(self, order_id: str) -> Any:
        return self.post(
            f"/api/orders/{order_id}/payments/stk/",
            {"phone_number": "0712 345 678", "idempotency_key": str(uuid.uuid4())},
        )

    def as_manager(self) -> None:
        self.client.force_login(self.manager)


class AuthApiTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = APIClient()  # anonymous
        self.amina = User.objects.create_user(username="amina", password="s3cret-pass-1")

    def test_csrf_endpoint_sets_the_cookie(self) -> None:
        response = self.client.get("/api/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)

    def test_login_me_and_logout(self) -> None:
        bad = self.post("/api/auth/login/", {"username": "amina", "password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.json()["error"]["code"], "invalid_credentials")

        ok = self.post("/api/auth/login/", {"username": "amina", "password": "s3cret-pass-1"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["role"], "cashier")
        self.assertEqual(self.client.get("/api/auth/me/").json()["username"], "amina")

        self.assertEqual(self.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)

    def test_an_inactive_user_cannot_log_in(self) -> None:
        User.objects.filter(pk=self.amina.pk).update(is_active=False)
        response = self.post("/api/auth/login/", {"username": "amina", "password": "s3cret-pass-1"})
        self.assertEqual(response.status_code, 401)


class AnonymousApiTests(ApiTestCase):
    def test_every_endpoint_requires_a_session(self) -> None:
        anonymous = APIClient()
        some_id = uuid.uuid4()
        gets = [
            "/api/auth/me/",
            "/api/menu/",
            "/api/shifts/current/",
            "/api/orders/",
            f"/api/orders/{some_id}/",
            f"/api/payments/{some_id}/",
            "/api/manager/review/",
            "/api/sync/status/",
        ]
        posts = [
            "/api/auth/logout/",
            "/api/shifts/open/",
            "/api/orders/",
            f"/api/orders/{some_id}/items/",
            f"/api/orders/{some_id}/charge/",
            f"/api/orders/{some_id}/payments/cash/",
            f"/api/orders/{some_id}/payments/stk/",
            f"/api/orders/{some_id}/payments/manual-mpesa/",
            f"/api/payments/{some_id}/confirm/",
            f"/api/payments/{some_id}/reject/",
            f"/api/payments/{some_id}/defer/",
            "/api/backfill/",
        ]
        for path in gets:
            with self.subTest(method="GET", path=path):
                self.assertEqual(anonymous.get(path).status_code, 403)
        for path in posts:
            with self.subTest(method="POST", path=path):
                self.assertEqual(anonymous.post(path, {}, format="json").status_code, 403)


class ShiftAndMenuApiTests(ApiTestCase):
    def _close_shift(self) -> None:
        Shift.objects.filter(pk=self.shift.pk).update(status=Shift.Status.CLOSED)

    def test_a_manager_opens_a_shift(self) -> None:
        self._close_shift()
        self.as_manager()

        response = self.post("/api/shifts/open/", {"opening_float": "1500.00"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "open")
        self.assertEqual(response.json()["opening_float"], "1500.00")
        self.assertTrue(AuditEvent.objects.filter(event_type="shift.opened").exists())

    def test_a_cashier_cannot_open_a_shift(self) -> None:
        self._close_shift()
        self.assertEqual(self.post("/api/shifts/open/", {"opening_float": "100"}).status_code, 403)

    def test_a_second_open_shift_is_refused(self) -> None:
        self.as_manager()
        response = self.post("/api/shifts/open/", {"opening_float": "100"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("already open", response.json()["error"]["messages"][0])

    def test_a_negative_float_is_refused(self) -> None:
        self._close_shift()
        self.as_manager()
        response = self.post("/api/shifts/open/", {"opening_float": "-5"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertIn("opening_float", response.json()["error"]["fields"])

    def test_current_shift(self) -> None:
        self.assertEqual(self.client.get("/api/shifts/current/").json()["shift"]["status"], "open")
        self._close_shift()
        self.assertIsNone(self.client.get("/api/shifts/current/").json()["shift"])

    def test_menu_lists_items_with_their_modifier_groups(self) -> None:
        categories = self.client.get("/api/menu/").json()["categories"]
        self.assertEqual(categories[0]["name"], "Mains")
        items = {item["name"]: item for item in categories[0]["items"]}
        self.assertEqual(items["Chips"]["modifier_groups"], [])
        group = items["Beef Burger"]["modifier_groups"][0]
        self.assertTrue(group["is_required"])
        self.assertEqual({o["name"] for o in group["options"]}, {"Regular", "Large"})


class OrderApiTests(ApiTestCase):
    def test_orders_need_an_open_shift(self) -> None:
        Shift.objects.filter(pk=self.shift.pk).update(status=Shift.Status.CLOSED)
        response = self.post("/api/orders/", {"id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "no_open_shift")

    def test_create_is_idempotent_for_a_client_id(self) -> None:
        order_id = str(uuid.uuid4())
        first = self.post("/api/orders/", {"id": order_id})
        again = self.post("/api/orders/", {"id": order_id})
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertEqual(first.json()["order_label"], "#1")
        self.assertEqual(Order.objects.count(), 1)

    def test_required_modifier_is_enforced_and_priced(self) -> None:
        order_id = str(uuid.uuid4())
        self.post("/api/orders/", {"id": order_id})
        path = f"/api/orders/{order_id}/items/"

        missing = self.post(path, {"menu_item": str(self.burger.pk)})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "validation_error")

        added = self.post(path, {"menu_item": str(self.burger.pk), "options": [str(self.large.pk)]})
        self.assertEqual(added.status_code, 201)
        body = added.json()
        self.assertEqual(body["total"], "380.00")
        self.assertEqual(body["items"][0]["modifiers"][0]["name"], "Large")

    def test_unknown_references_are_refused(self) -> None:
        order_id = str(uuid.uuid4())
        self.post("/api/orders/", {"id": order_id})
        path = f"/api/orders/{order_id}/items/"
        unknown_item = self.post(path, {"menu_item": str(uuid.uuid4())})
        unknown_option = self.post(
            path, {"menu_item": str(self.burger.pk), "options": [str(uuid.uuid4())]}
        )
        self.assertEqual(unknown_item.status_code, 404)
        self.assertEqual(unknown_option.status_code, 400)

    def test_an_empty_order_cannot_be_charged(self) -> None:
        order_id = str(uuid.uuid4())
        self.post("/api/orders/", {"id": order_id})
        self.assertEqual(self.post(f"/api/orders/{order_id}/charge/").status_code, 400)

    def test_the_order_list_shows_the_current_shifts_orders(self) -> None:
        self.new_order()
        self.charged_order()
        everything = self.client.get("/api/orders/").json()["orders"]
        building = self.client.get("/api/orders/?status=building").json()["orders"]
        self.assertEqual(len(everything), 2)
        self.assertEqual(len(building), 1)


class PaymentApiTests(ApiTestCase):
    def test_cash_payment_pays_the_order_and_replays_safely(self) -> None:
        order_id = self.charged_order()
        key = str(uuid.uuid4())

        first = self.pay_cash(order_id, key=key)
        again = self.pay_cash(order_id, key=key)

        self.assertEqual((first.status_code, again.status_code), (201, 200))
        body = first.json()
        self.assertEqual(body["payment"]["status"], "confirmed")
        self.assertEqual(body["payment"]["change_given"], "150.00")
        self.assertEqual(body["order"]["status"], "paid")
        self.assertEqual(again.json()["payment"]["id"], body["payment"]["id"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_underpaying_cash_is_refused(self) -> None:
        order_id = self.charged_order()
        response = self.pay_cash(order_id, tendered="100.00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("less", response.json()["error"]["messages"][0])
        self.assertEqual(Payment.objects.count(), 0)

    def test_manual_mpesa_is_pending_verification(self) -> None:
        order_id = self.charged_order()

        response = self.pay_manual(order_id, code="qgh7xxxxxx")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["payment"]["status"], "pending_verification")
        self.assertEqual(body["payment"]["mpesa_receipt_code"], "QGH7XXXXXX")
        self.assertEqual(body["order"]["status"], "paid_pending_verification")

    def test_a_manual_code_used_on_another_order_is_refused(self) -> None:
        self.pay_manual(self.charged_order())
        response = self.pay_manual(self.charged_order())
        self.assertEqual(response.status_code, 400)
        self.assertIn("#1", response.json()["error"]["messages"][0])

    def test_stk_start_returns_a_pending_payment_with_a_countdown(self) -> None:
        response = self.start_stk(self.charged_order())

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["payment"]["status"], "pending")
        self.assertEqual(body["payment"]["phone_number"], "254712345678")
        self.assertEqual(body["timeout_seconds"], 90)
        self.assertIn("expires_at", body)

    def test_stk_offline_is_a_503_and_cash_still_works(self) -> None:
        get_mock_provider().online = False
        order_id = self.charged_order()

        response = self.start_stk(order_id)

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error"]["code"], "provider_unavailable")
        self.assertEqual(body["payment"]["status"], "failed")
        self.assertEqual(self.pay_cash(order_id).status_code, 201)

    @override_settings(STK_TIMEOUT_SECONDS=0)
    def test_an_unanswered_prompt_times_out_when_polled(self) -> None:
        started = self.start_stk(self.charged_order())
        payment_id = started.json()["payment"]["id"]

        polled = self.client.get(f"/api/payments/{payment_id}/")

        self.assertEqual(polled.status_code, 200)
        self.assertEqual(polled.json()["payment"]["status"], "timed_out")

    @override_settings(DEBUG=True)
    def test_dev_simulation_resolves_a_prompt(self) -> None:
        started = self.start_stk(self.charged_order())
        payment_id = started.json()["payment"]["id"]

        simulated = self.post(f"/api/dev/stk/{payment_id}/simulate/", {"outcome": "success"})

        self.assertEqual(simulated.status_code, 200)
        self.assertEqual(simulated.json()["payment"]["status"], "confirmed")
        self.assertEqual(simulated.json()["order"]["status"], "paid")

    @override_settings(DEBUG=True)
    def test_dev_toggle_takes_the_mock_provider_offline(self) -> None:
        order_id = self.charged_order()
        self.assertEqual(self.post("/api/dev/mpesa/online/", {"online": False}).status_code, 200)
        self.assertEqual(self.start_stk(order_id).status_code, 503)

    def test_dev_endpoints_do_not_exist_outside_debug(self) -> None:
        some_id = uuid.uuid4()
        self.assertEqual(self.post("/api/dev/mpesa/online/", {"online": False}).status_code, 404)
        simulate = self.post(f"/api/dev/stk/{some_id}/simulate/", {"outcome": "success"})
        self.assertEqual(simulate.status_code, 404)


class CallbackApiTests(ApiTestCase):
    def _prompt(self, outcome: Outcome = Outcome.SUCCESS) -> tuple[Payment, dict[str, Any]]:
        started = self.start_stk(self.charged_order())
        payment = Payment.objects.get(pk=started.json()["payment"]["id"])
        attempt = payment.attempts.get()
        return payment, get_mock_provider().simulate(attempt.checkout_request_id, outcome)

    def test_a_success_callback_confirms_the_payment(self) -> None:
        payment, payload = self._prompt()

        response = self.post(CALLBACK_URL, payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ResultCode"], 0)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(Order.objects.get(pk=payment.order_id).status, Order.Status.PAID)

    def test_a_duplicate_callback_changes_nothing(self) -> None:
        payment, payload = self._prompt()
        self.post(CALLBACK_URL, payload)
        self.post(CALLBACK_URL, payload)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(Outbox.objects.count(), 2)

    def test_a_wrong_amount_goes_to_manager_review(self) -> None:
        payment, payload = self._prompt()
        payload["Body"]["stkCallback"]["CallbackMetadata"]["Item"][0]["Value"] = 100.0

        response = self.post(CALLBACK_URL, payload)

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.MANAGER_REVIEW)
        self.assertEqual(payment.review_reason, Payment.ReviewReason.OTHER)
        audit = AuditEvent.objects.filter(
            event_type="payment.transition", target_id=str(payment.pk)
        ).latest("created_at")
        self.assertIn("100", audit.reason)
        self.assertEqual(
            Order.objects.get(pk=payment.order_id).status, Order.Status.AWAITING_PAYMENT
        )

    def test_a_wrong_token_is_a_404(self) -> None:
        _, payload = self._prompt()
        self.assertEqual(self.post("/api/mpesa/callback/wrong/", payload).status_code, 404)

    @override_settings(MPESA_CALLBACK_TOKEN="change-me")
    def test_the_placeholder_token_never_authorizes(self) -> None:
        _, payload = self._prompt()
        self.assertEqual(self.post("/api/mpesa/callback/change-me/", payload).status_code, 404)

    def test_a_malformed_callback_is_a_400(self) -> None:
        self.assertEqual(self.post(CALLBACK_URL, {"nonsense": 1}).status_code, 400)

    def test_an_unknown_prompt_is_acknowledged_and_ignored(self) -> None:
        payment, payload = self._prompt()
        payload["Body"]["stkCallback"]["CheckoutRequestID"] = "ws_CO_unknown"

        response = self.post(CALLBACK_URL, payload)

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)


class ManagerApiTests(ApiTestCase):
    def _flagged(self, reason: Payment.ReviewReason = Payment.ReviewReason.FUZZY_MATCH) -> Payment:
        created = self.pay_manual(self.charged_order())
        payment = Payment.objects.get(pk=created.json()["payment"]["id"])
        payment_flow.apply_event(
            payment, PaymentEvent.FLAGGED_FOR_REVIEW, review_reason=reason.value
        )
        self.as_manager()
        return payment

    def test_the_review_queue_is_manager_only(self) -> None:
        self.assertEqual(self.client.get("/api/manager/review/").status_code, 403)

    def test_the_review_queue_lists_payments_that_need_attention(self) -> None:
        self._flagged()
        self.pay_manual(self.charged_order(), code="QGH7AAAAAA")  # still pending verification

        body = self.client.get("/api/manager/review/").json()

        self.assertEqual(len(body["payments"]), 2)
        self.assertEqual(body["counts"], {"needs_review": 1, "pending_verification": 1})

    def test_a_cashier_cannot_act_on_a_payment(self) -> None:
        payment = self._flagged()
        self.client.force_login(self.user)
        for action in ("confirm", "reject", "defer"):
            with self.subTest(action=action):
                response = self.post(f"/api/payments/{payment.pk}/{action}/", {"reason": "x"})
                self.assertEqual(response.status_code, 403)

    def test_a_manager_confirms_a_payment(self) -> None:
        payment = self._flagged()

        response = self.post(f"/api/payments/{payment.pk}/confirm/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["result"], "applied")
        self.assertEqual(body["payment"]["status"], "confirmed")
        self.assertEqual(body["order"]["status"], "paid")

    def test_rejecting_needs_a_reason_and_reopens_the_order(self) -> None:
        payment = self._flagged()
        path = f"/api/payments/{payment.pk}/reject/"
        self.assertEqual(self.post(path, {}).status_code, 400)

        response = self.post(path, {"reason": "Customer never paid"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payment"]["status"], "failed")
        self.assertEqual(response.json()["order"]["status"], "awaiting_payment")

    def test_deferring_returns_the_payment_to_pending_verification(self) -> None:
        payment = self._flagged()
        response = self.post(f"/api/payments/{payment.pk}/defer/")
        self.assertEqual(response.json()["payment"]["status"], "pending_verification")

    def test_confirming_a_payment_that_is_not_in_review_is_a_409(self) -> None:
        created = self.pay_manual(self.charged_order())
        self.as_manager()
        response = self.post(f"/api/payments/{created.json()['payment']['id']}/confirm/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "illegal_transition")

    def test_a_duplicate_payment_cannot_be_confirmed(self) -> None:
        payment = self._flagged(Payment.ReviewReason.CROSS_METHOD_DUPLICATE)
        response = self.post(f"/api/payments/{payment.pk}/confirm/")
        self.assertEqual(response.status_code, 400)


class BackfillApiTests(ApiTestCase):
    def _body(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "reason": "Power cut, paper tickets",
            "occurred_at": (timezone.now() - timedelta(hours=2)).isoformat(),
            "lines": [{"menu_item": str(self.item.pk), "quantity": 1}],
            "payment_key": str(uuid.uuid4()),
            "cash_tendered": "400.00",
        }
        body.update(overrides)
        return body

    def setUp(self) -> None:
        super().setUp()
        Shift.objects.filter(pk=self.shift.pk).update(opened_at=timezone.now() - timedelta(hours=3))

    def test_a_manager_backfills_an_order_and_a_double_submit_is_safe(self) -> None:
        self.as_manager()
        body = self._body()

        first = self.post("/api/backfill/", body)
        again = self.post("/api/backfill/", body)

        self.assertEqual((first.status_code, again.status_code), (201, 200))
        order = first.json()
        self.assertEqual(order["order_label"], "B-1")
        self.assertTrue(order["is_backfilled"])
        self.assertEqual(order["status"], "paid")
        self.assertEqual(Order.objects.filter(is_backfilled=True).count(), 1)

    def test_a_cashier_cannot_backfill(self) -> None:
        self.assertEqual(self.post("/api/backfill/", self._body()).status_code, 403)

    def test_backfill_needs_an_open_shift(self) -> None:
        Shift.objects.filter(pk=self.shift.pk).update(status=Shift.Status.CLOSED)
        self.as_manager()
        response = self.post("/api/backfill/", self._body())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "no_open_shift")


class SystemApiTests(ApiTestCase):
    def test_sync_status_reports_unsent_rows(self) -> None:
        self.pay_cash(self.charged_order())

        body = self.client.get("/api/sync/status/").json()

        self.assertEqual(body["queue"], {"pending": 2, "processing": 0, "failed": 0})
        self.assertFalse(body["sync_enabled"])
        self.assertIsNotNone(body["oldest_unsent_at"])
        self.assertEqual((body["pending_verification"], body["needs_review"]), (0, 0))

    @override_settings(DEBUG=True)
    def test_seed_demo_creates_users_and_a_menu_and_is_idempotent(self) -> None:
        for _ in range(2):
            call_command("seed_demo", "--password", "pw-for-tests-1", stdout=StringIO())
        self.assertTrue(User.objects.get(username="manager").check_password("pw-for-tests-1"))
        self.assertTrue(User.objects.get(username="manager").is_manager)
        self.assertEqual(User.objects.filter(username__in=["cashier", "manager"]).count(), 2)
        self.assertEqual(MenuCategory.objects.filter(name="Burgers").count(), 1)
        self.assertEqual(
            MenuItem.objects.filter(category__name="Burgers", name="Beef Burger").count(), 1
        )


def _callback(code: int = 0, amount: Any = 350, receipt: str = "QGH7XXXXXX") -> dict[str, Any]:
    callback: dict[str, Any] = {
        "CheckoutRequestID": "ws_CO_1",
        "ResultCode": code,
        "ResultDesc": "x",
    }
    if code == 0:
        items: list[dict[str, Any]] = []
        if amount is not None:
            items.append({"Name": "Amount", "Value": amount})
        if receipt:
            items.append({"Name": "MpesaReceiptNumber", "Value": receipt})
        callback["CallbackMetadata"] = {"Item": items}
    return {"Body": {"stkCallback": callback}}


class StkCallbackParseTests(SimpleTestCase):
    def test_parses_a_success_callback(self) -> None:
        event = parse_stk_callback(_callback())
        self.assertEqual(event.event, PaymentEvent.SUCCEEDED)
        self.assertEqual(event.amount, Decimal("350"))
        self.assertEqual(event.receipt_code, "QGH7XXXXXX")

    def test_failure_codes_need_no_metadata(self) -> None:
        self.assertEqual(parse_stk_callback(_callback(code=1032)).event, PaymentEvent.CANCELLED)
        failed = parse_stk_callback(_callback(code=1))
        self.assertEqual(failed.event, PaymentEvent.FAILED)
        self.assertIsNone(failed.amount)

    def test_untrustworthy_payloads_are_rejected(self) -> None:
        bad: list[Any] = [
            [],
            {},
            {"Body": {}},
            _callback(amount=None),
            _callback(receipt=""),
            _callback(amount="abc"),
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_stk_callback(payload)
