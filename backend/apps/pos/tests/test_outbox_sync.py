import json
import random
import uuid
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.db.models import Sum
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.cloudmock.models import CloudEvent
from apps.pos import services
from apps.pos.models import MenuCategory, MenuItem, Outbox, Payment, Shift
from apps.pos.outbox_sync import (
    DeliveryStatus,
    HttpSink,
    OutboxSink,
    SinkResult,
    SinkUnavailable,
    claim_batch,
    drain_once,
    parse_results,
    queue_stats,
    requeue_failed,
)

RECEIVER = {"CLOUD_MOCK_ENABLED": True, "CLOUD_API_KEY": "test-key"}


class ClientSink:
    """Delivers through the real receiver view using Django's test client (no network)."""

    def __init__(self, api_key: str = "test-key") -> None:
        self.client = Client()
        self.api_key = api_key

    def deliver(self, events: Sequence[dict[str, Any]]) -> list[SinkResult]:
        response = self.client.post(
            "/cloud/ingest/",
            data=json.dumps({"events": list(events)}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.api_key}",
        )
        if response.status_code != 200:
            raise SinkUnavailable(f"HTTP {response.status_code}")
        return parse_results(response.json())


class ControlledSink:
    """Wraps a sink so tests can cut the connection or lose a response after delivery."""

    def __init__(self, inner: ClientSink) -> None:
        self.inner = inner
        self.online = True
        self.lose_responses = 0

    def deliver(self, events: Sequence[dict[str, Any]]) -> list[SinkResult]:
        if not self.online:
            raise SinkUnavailable("offline")
        results = self.inner.deliver(events)
        if self.lose_responses > 0:
            self.lose_responses -= 1
            raise SinkUnavailable("response lost after cloud accepted")
        return results


@override_settings(**RECEIVER)
class SyncTests(TestCase):
    user: User
    shift: Shift
    item: MenuItem

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="cashier1", password="test-pass-123")
        cls.shift = Shift.objects.create(opened_by=cls.user, opening_float=Decimal("1000"))
        category = MenuCategory.objects.create(name="Mains")
        cls.item = MenuItem.objects.create(category=category, name="Chips", price=Decimal("350.00"))

    # ---- helpers -------------------------------------------------------------------------

    def _sell(self) -> Payment:
        order = services.create_order(shift=self.shift, created_by=self.user)
        services.add_item(order=order, menu_item=self.item)
        services.charge_order(order=order)
        payment, _ = services.confirm_cash_payment(
            order=order,
            tendered=Decimal("500"),
            recorded_by=self.user,
            idempotency_key=uuid.uuid4(),
        )
        return payment

    def _sell_many(self, count: int) -> None:
        for _ in range(count):
            self._sell()

    def _drain_all(self, sink: OutboxSink, now: Any, batch_size: int | None = None) -> int:
        delivered = 0
        for _ in range(100):
            report = drain_once(sink, now=now, batch_size=batch_size)
            delivered += report.sent + report.duplicates
            if report.claimed == 0 or report.transport_error:
                break
        return delivered

    # ---- delivery guarantees -------------------------------------------------------------

    def test_drain_delivers_in_order_exactly_once(self) -> None:
        self._sell_many(3)
        sink = ClientSink()

        report = drain_once(sink)

        self.assertEqual((report.claimed, report.sent, report.duplicates), (6, 6, 0))
        arrived = list(CloudEvent.objects.order_by("id").values_list("event_type", "sequence"))
        self.assertEqual(
            arrived,
            [
                ("order.paid", 1),
                ("payment.confirmed", 2),
                ("order.paid", 3),
                ("payment.confirmed", 4),
                ("order.paid", 5),
                ("payment.confirmed", 6),
            ],
        )
        self.assertEqual(Outbox.objects.filter(status=Outbox.Status.SENT).count(), 6)
        again = drain_once(sink)
        self.assertEqual(again.claimed, 0)
        self.assertFalse(again.blocked)

    def test_internet_down_then_reconnect_loses_nothing(self) -> None:
        sink = ControlledSink(ClientSink())
        sink.online = False
        self._sell_many(2)
        t0 = timezone.now()

        report = drain_once(sink, now=t0)

        self.assertEqual(report.transport_error, "offline")
        self.assertEqual(CloudEvent.objects.count(), 0)
        self.assertEqual(Outbox.objects.filter(status=Outbox.Status.PENDING).count(), 4)

        self._sell_many(1)  # the till keeps working while sync is down
        sink.online = True
        self._drain_all(sink, t0 + timedelta(minutes=5))

        self.assertEqual(CloudEvent.objects.count(), 6)
        self.assertEqual(Outbox.objects.exclude(status=Outbox.Status.SENT).count(), 0)

    def test_backoff_blocks_the_head_of_the_queue(self) -> None:
        sink = ControlledSink(ClientSink())
        sink.online = False
        self._sell_many(1)
        t0 = timezone.now()
        drain_once(sink, now=t0)
        sink.online = True

        blocked = drain_once(sink, now=t0)
        self.assertEqual(blocked.claimed, 0)
        self.assertTrue(blocked.blocked)
        self.assertEqual(CloudEvent.objects.count(), 0)

        recovered = drain_once(sink, now=t0 + timedelta(seconds=10))
        self.assertEqual(recovered.sent, 2)

    def test_lost_response_is_deduplicated_by_the_cloud(self) -> None:
        self._sell_many(2)
        sink = ControlledSink(ClientSink())
        sink.lose_responses = 1
        t0 = timezone.now()

        first = drain_once(sink, now=t0)

        self.assertEqual(first.transport_error, "response lost after cloud accepted")
        self.assertEqual(CloudEvent.objects.count(), 4)  # the cloud has them...
        self.assertEqual(
            Outbox.objects.filter(status=Outbox.Status.PENDING).count(), 4
        )  # ...we don't know

        second = drain_once(sink, now=t0 + timedelta(minutes=1))

        self.assertEqual((second.sent, second.duplicates), (0, 4))
        self.assertEqual(CloudEvent.objects.count(), 4)
        self.assertEqual(Outbox.objects.filter(status=Outbox.Status.SENT).count(), 4)

    def test_worker_killed_mid_batch_is_recovered_after_the_lease(self) -> None:
        self._sell_many(2)
        t0 = timezone.now()
        claimed = claim_batch(t0, 50)  # a worker claims the batch, then dies before sending
        self.assertEqual(len(claimed), 4)
        sink = ClientSink()

        during = drain_once(sink, now=t0 + timedelta(seconds=5))
        self.assertEqual(during.claimed, 0)
        self.assertTrue(during.blocked)

        lease_over = t0 + timedelta(seconds=settings.OUTBOX_LEASE_SECONDS + 1)
        after = drain_once(sink, now=lease_over)
        self.assertEqual(after.sent, 4)
        self.assertEqual(CloudEvent.objects.count(), 4)

    def test_rejected_event_halts_the_queue_until_requeued(self) -> None:
        self._sell_many(3)
        Outbox.objects.filter(sequence=3).update(payload=[])  # invalid: payload must be an object
        sink = ClientSink()

        report = drain_once(sink)

        self.assertEqual((report.sent, report.rejected, report.skipped), (2, 1, 3))
        statuses = list(Outbox.objects.order_by("sequence").values_list("status", flat=True))
        self.assertEqual(statuses, ["sent", "sent", "failed", "pending", "pending", "pending"])

        blocked = drain_once(sink)
        self.assertEqual(blocked.claimed, 0)
        self.assertTrue(blocked.blocked)
        self.assertEqual(CloudEvent.objects.count(), 2)

        Outbox.objects.filter(sequence=3).update(payload={"fixed": True})  # operator fixes it
        self.assertEqual(requeue_failed(), 1)
        self._drain_all(sink, timezone.now() + timedelta(seconds=1))

        sequences = list(CloudEvent.objects.order_by("id").values_list("sequence", flat=True))
        self.assertEqual(sequences, [1, 2, 3, 4, 5, 6])

    def test_large_backlog_drains_in_batches_and_totals_match(self) -> None:
        self._sell_many(25)
        sink = ClientSink()

        delivered = self._drain_all(sink, timezone.now(), batch_size=20)

        self.assertEqual(delivered, 50)
        confirmed = CloudEvent.objects.filter(event_type="payment.confirmed")
        cloud_total = sum((Decimal(e.payload["amount"]) for e in confirmed), Decimal("0"))
        local_total = Payment.objects.aggregate(total=Sum("amount"))["total"]
        self.assertEqual(confirmed.count(), 25)
        self.assertEqual(cloud_total, local_total)

    def test_flapping_connection_never_duplicates_or_reorders(self) -> None:
        rng = random.Random(7)
        sink = ControlledSink(ClientSink())
        now = timezone.now()
        for _ in range(15):
            sink.online = rng.random() > 0.4
            sink.lose_responses = 1 if rng.random() < 0.3 else 0
            self._sell_many(1)
            now += timedelta(minutes=2)
            drain_once(sink, now=now)
        sink.online = True
        sink.lose_responses = 0
        now += timedelta(minutes=2)
        self._drain_all(sink, now)

        keys = list(CloudEvent.objects.order_by("id").values_list("idempotency_key", flat=True))
        sequences = list(CloudEvent.objects.order_by("id").values_list("sequence", flat=True))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), Outbox.objects.count())
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(Outbox.objects.exclude(status=Outbox.Status.SENT).count(), 0)

    def test_queue_stats_counts_unsent_rows(self) -> None:
        self._sell_many(1)
        self.assertEqual(queue_stats(), {"pending": 2, "processing": 0, "failed": 0})
        claim_batch(timezone.now(), 1)
        self.assertEqual(queue_stats(), {"pending": 1, "processing": 1, "failed": 0})

    # ---- transport -----------------------------------------------------------------------

    def test_http_sink_refuses_non_http_urls(self) -> None:
        with self.assertRaises(ValueError):
            HttpSink("file:///etc/passwd", "key")

    def test_malformed_cloud_response_is_a_transport_error(self) -> None:
        with self.assertRaises(SinkUnavailable):
            parse_results({"unexpected": 1})
        self.assertEqual(DeliveryStatus("accepted"), DeliveryStatus.ACCEPTED)


def _event(key: str, sequence: int) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "sequence": sequence,
        "aggregate_type": "order",
        "aggregate_id": "o-1",
        "event_type": "order.paid",
        "payload": {"total": "350.00"},
        "created_at": timezone.now().isoformat(),
    }


@override_settings(**RECEIVER)
class CloudReceiverTests(TestCase):
    def _post(self, body: object, *, key: str | None = "test-key", raw: str | None = None) -> Any:
        headers = {"Authorization": f"Bearer {key}"} if key is not None else None
        data = raw if raw is not None else json.dumps(body)
        return Client().post(
            "/cloud/ingest/", data=data, content_type="application/json", headers=headers
        )

    @override_settings(CLOUD_MOCK_ENABLED=False)
    def test_disabled_receiver_is_a_404(self) -> None:
        self.assertEqual(self._post({"events": []}).status_code, 404)

    def test_missing_or_wrong_key_is_unauthorized(self) -> None:
        self.assertEqual(self._post({"events": []}, key=None).status_code, 401)
        self.assertEqual(self._post({"events": []}, key="wrong").status_code, 401)

    @override_settings(CLOUD_API_KEY="")
    def test_empty_configured_key_never_authorizes(self) -> None:
        self.assertEqual(self._post({"events": []}, key="").status_code, 401)

    def test_malformed_body_is_a_400(self) -> None:
        self.assertEqual(self._post(None, raw="not json").status_code, 400)
        self.assertEqual(self._post({"events": "nope"}).status_code, 400)

    def test_accepts_then_reports_duplicates(self) -> None:
        body = {"events": [_event("k1", 1), _event("k2", 2)]}

        first = [r["status"] for r in self._post(body).json()["results"]]
        second = [r["status"] for r in self._post(body).json()["results"]]

        self.assertEqual(first, ["accepted", "accepted"])
        self.assertEqual(second, ["duplicate", "duplicate"])
        self.assertEqual(CloudEvent.objects.count(), 2)

    def test_invalid_event_is_rejected_and_the_rest_skipped(self) -> None:
        bad = _event("k2", 2)
        bad["payload"] = []
        body = {"events": [_event("k1", 1), bad, _event("k3", 3)]}

        statuses = [r["status"] for r in self._post(body).json()["results"]]

        self.assertEqual(statuses, ["accepted", "rejected", "skipped"])
        self.assertEqual(CloudEvent.objects.count(), 1)


class CommandTests(TestCase):
    @override_settings(CLOUD_SYNC_URL="")
    def test_drain_command_reports_when_sync_is_disabled(self) -> None:
        out = StringIO()
        call_command("drain_outbox", "--once", stdout=out)
        self.assertIn("not set", out.getvalue())

    @override_settings(DEBUG=True)
    def test_simulate_sales_creates_paid_orders_and_outbox_rows(self) -> None:
        call_command("simulate_sales", "3", stdout=StringIO())
        self.assertEqual(Payment.objects.count(), 3)
        self.assertEqual(Outbox.objects.count(), 6)
