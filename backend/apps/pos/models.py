"""QSR-102: database schema v1.

Rules enforced here:
  * Order.shift is required; Payment.order and Payment.shift are required (PROTECT, non-null).
  * Payment.shift must equal Order.shift (checked in Payment.save()).
  * AuditEvent is append-only (Python guards here, plus a Postgres trigger in migration 0002).
  * All business tables use UUID primary keys so records created offline can sync without clashes.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, NoReturn

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import connection, models, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

USER = settings.AUTH_USER_MODEL

# Postgres advisory-lock key that serialises Outbox sequence allocation.
OUTBOX_LOCK_ID = 7001


class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# --------------------------------------------------------------------------- inventory


class StockItem(BaseModel):
    name = models.CharField(max_length=120, unique=True)
    unit = models.CharField(max_length=20, default="pcs")
    # May go negative: a till must never refuse a sale because a stock count is off.
    quantity_on_hand = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    low_stock_threshold = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    is_active = models.BooleanField(default=True)

    @property
    def is_low(self) -> bool:
        return self.quantity_on_hand <= self.low_stock_threshold

    def __str__(self) -> str:
        return self.name


# --------------------------------------------------------------------------- menu


class MenuCategory(BaseModel):
    name = models.CharField(max_length=80, unique=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "menu categories"

    def __str__(self) -> str:
        return self.name


class ModifierGroup(BaseModel):
    name = models.CharField(max_length=80)
    min_select = models.PositiveSmallIntegerField(default=0)  # >= 1 means required
    max_select = models.PositiveSmallIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(max_select__gte=F("min_select")),
                name="modifiergroup_max_gte_min",
            )
        ]

    @property
    def is_required(self) -> bool:
        return self.min_select > 0

    def __str__(self) -> str:
        return self.name


class ModifierOption(BaseModel):
    group = models.ForeignKey(ModifierGroup, on_delete=models.CASCADE, related_name="options")
    name = models.CharField(max_length=80)
    price_delta = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    is_default = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return f"{self.group.name}: {self.name}"


class MenuItem(BaseModel):
    category = models.ForeignKey(MenuCategory, on_delete=models.PROTECT, related_name="items")
    name = models.CharField(max_length=120)
    price = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0.1600"), help_text="Fraction: 0.16 = 16%"
    )
    is_available = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    modifier_groups = models.ManyToManyField(ModifierGroup, blank=True, related_name="menu_items")
    stock_item = models.ForeignKey(
        StockItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="menu_items"
    )
    stock_per_sale = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("1"))

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["category", "name"], name="uniq_menuitem_per_category")
        ]

    def __str__(self) -> str:
        return self.name


# --------------------------------------------------------------------------- shifts


class Shift(BaseModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    opened_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="shifts_opened")
    opened_at = models.DateTimeField(default=timezone.now)
    opening_float = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    closed_by = models.ForeignKey(
        USER, on_delete=models.PROTECT, null=True, blank=True, related_name="shifts_closed"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    # Both figures and the variance are stored so a close is reproducible later.
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cash_variance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            # Single-terminal MVP: at most one open shift. Drop this when multi-terminal lands.
            models.UniqueConstraint(
                fields=["status"], condition=Q(status="open"), name="one_open_shift"
            )
        ]
        ordering = ["-opened_at"]

    def __str__(self) -> str:
        return f"Shift {self.opened_at:%Y-%m-%d %H:%M} ({self.status})"


# --------------------------------------------------------------------------- orders


class OrderManager(models.Manager["Order"]):
    def create_for_shift(self, shift: Shift, created_by: Any, **fields: Any) -> Order:
        """Create a live order with the next sequential number for the shift (race-safe).
        Backfilled orders have their own number range and never consume live numbers.
        """
        with transaction.atomic():
            locked = Shift.objects.select_for_update().get(pk=shift.pk)
            if locked.status != Shift.Status.OPEN:
                raise ValidationError("Cannot create an order on a closed shift.")
            live = self.filter(shift=locked, is_backfilled=False)
            last = live.aggregate(m=Max("order_number"))["m"] or 0
            return self.create(shift=locked, created_by=created_by, order_number=last + 1, **fields)


class Order(BaseModel):
    class Status(models.TextChoices):
        BUILDING = "building", "Building"
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        PAID = "paid", "Paid"
        PAID_PENDING_VERIFICATION = "paid_pending_verification", "Paid - pending verification"
        IN_KITCHEN = "in_kitchen", "In kitchen"
        FULFILLED = "fulfilled", "Fulfilled"
        VOIDED = "voided", "Voided"

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="orders")
    order_number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.BUILDING, db_index=True
    )
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="orders_created")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    notes = models.TextField(blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        USER, on_delete=models.PROTECT, null=True, blank=True, related_name="orders_voided"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True)

    # When the sale actually happened. Differs from created_at for backfilled orders.
    occurred_at = models.DateTimeField(default=timezone.now)
    # Backfill: taken on paper during a total outage, entered later by a manager.
    is_backfilled = models.BooleanField(default=False)
    backfill_reason = models.CharField(max_length=255, blank=True)
    backfilled_by = models.ForeignKey(
        USER, on_delete=models.PROTECT, null=True, blank=True, related_name="orders_backfilled"
    )

    objects = OrderManager()

    class Meta:
        constraints = [
            # Live and backfilled orders number independently within a shift.
            models.UniqueConstraint(
                fields=["shift", "is_backfilled", "order_number"],
                name="uniq_order_number_per_shift_kind",
            ),
            models.CheckConstraint(
                condition=Q(is_backfilled=False)
                | (~Q(backfill_reason="") & Q(backfilled_by__isnull=False)),
                name="order_backfill_requires_reason_and_actor",
            ),
        ]
        indexes = [models.Index(fields=["shift", "status"])]

    def __str__(self) -> str:
        prefix = "B-" if self.is_backfilled else "#"
        return f"{prefix}{self.order_number} ({self.status})"


class OrderItem(BaseModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT, related_name="order_items")
    # Snapshots: later menu edits must never rewrite history.
    name = models.CharField(max_length=120)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.1600"))
    quantity = models.PositiveIntegerField(default=1)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gte=1), name="orderitem_quantity_gte_1")
        ]

    def __str__(self) -> str:
        return f"{self.quantity} x {self.name}"


class OrderItemModifier(BaseModel):
    order_item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name="modifiers")
    option = models.ForeignKey(
        ModifierOption, on_delete=models.PROTECT, related_name="order_item_modifiers"
    )
    name = models.CharField(max_length=80)  # snapshot
    price_delta = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))


# --------------------------------------------------------------------------- payments


class Payment(BaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        TIMED_OUT = "timed_out", "Timed out"
        CANCELLED = "cancelled", "Cancelled"
        PENDING_VERIFICATION = "pending_verification", "Pending verification"
        MANAGER_REVIEW = "manager_review", "Manager review"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        MPESA_STK = "mpesa_stk", "M-PESA STK push"
        MPESA_MANUAL = "mpesa_manual", "M-PESA manual till entry"

    class ReviewReason(models.TextChoices):
        FUZZY_MATCH = "fuzzy_match", "Near match, needs manager confirmation"
        CROSS_METHOD_DUPLICATE = "cross_method_duplicate", "Cross-method duplicate"
        NO_MATCH = "no_match", "No matching transaction found"
        OTHER = "other", "Other"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=16, choices=Method.choices)
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Supplied by the client per "Confirm" action. A double-tap or an offline replay carries
    # the same key, so the second insert fails instead of creating a second payment.
    idempotency_key = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)

    # When money actually changed hands. Reconciliation matches on this, not created_at.
    occurred_at = models.DateTimeField(default=timezone.now)

    # Cash
    tendered_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_given = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # M-PESA. Deliberately NOT unique: an STK callback and a cashier's manual entry can carry
    # the same receipt code, and that must route to manager_review, not crash the callback.
    phone_number = models.CharField(max_length=15, blank=True)
    mpesa_receipt_code = models.CharField(max_length=20, blank=True, db_index=True)
    # Reconciliation
    review_reason = models.CharField(max_length=24, choices=ReviewReason.choices, blank=True)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="duplicates"
    )
    recorded_by = models.ForeignKey(
        USER, on_delete=models.PROTECT, related_name="payments_recorded"
    )
    confirmed_by = models.ForeignKey(
        USER, on_delete=models.PROTECT, null=True, blank=True, related_name="payments_confirmed"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_gt_0")
        ]
        indexes = [
            models.Index(fields=["order", "status"]),
            models.Index(fields=["shift", "status"]),
        ]

    def _check_shift(self) -> None:
        if self.shift_id != self.order.shift_id:
            raise ValidationError({"shift": "Payment.shift must match Order.shift."})

    def clean(self) -> None:
        super().clean()
        if self.order_id and self.shift_id:
            self._check_shift()

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.order_id:
            if not self.shift_id:
                self.shift_id = self.order.shift_id
            self._check_shift()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.method} {self.amount} ({self.status})"


class PaymentAttempt(BaseModel):
    """One STK push attempt. Retries are new attempts under the SAME Payment, so a retry is
    never confused with a second payment."""

    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        TIMED_OUT = "timed_out", "Timed out"
        CANCELLED = "cancelled", "Cancelled"

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="attempts")
    attempt_number = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.INITIATED, db_index=True
    )
    phone_number = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    checkout_request_id = models.CharField(max_length=100, blank=True)  # Daraja
    merchant_request_id = models.CharField(max_length=100, blank=True)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    mpesa_receipt_code = models.CharField(max_length=20, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    callback_payload = models.JSONField(default=dict, blank=True)
    callback_count = models.PositiveSmallIntegerField(default=0)  # duplicate callbacks seen

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "attempt_number"], name="uniq_attempt_number_per_payment"
            ),
            # Callback idempotency: one attempt row per Daraja CheckoutRequestID.
            models.UniqueConstraint(
                fields=["checkout_request_id"],
                condition=~Q(checkout_request_id=""),
                name="uniq_checkout_request_id",
            ),
        ]


# --------------------------------------------------------------------------- printing


class PrintJob(BaseModel):
    class JobType(models.TextChoices):
        KITCHEN_TICKET = "kitchen_ticket", "Kitchen ticket"
        CUSTOMER_RECEIPT = "customer_receipt", "Customer receipt"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PRINTING = "printing", "Printing"
        PRINTED = "printed", "Printed"
        FAILED = "failed", "Failed"  # stays in the queue, retryable

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="print_jobs")
    job_type = models.CharField(max_length=20, choices=JobType.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    is_reprint = models.BooleanField(default=False)
    requested_by = models.ForeignKey(
        USER, on_delete=models.PROTECT, null=True, blank=True, related_name="print_jobs_requested"
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    printed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            # One original ticket per order and type; only explicit reprints may repeat.
            models.UniqueConstraint(
                fields=["order", "job_type"],
                condition=Q(is_reprint=False),
                name="uniq_original_print_per_order_type",
            )
        ]
        indexes = [models.Index(fields=["status", "created_at"])]


# --------------------------------------------------------------------------- audit + outbox


class ImmutableRecordError(Exception):
    pass


class AppendOnlyQuerySet(models.QuerySet["AuditEvent"]):
    def update(self, **kwargs: Any) -> NoReturn:
        raise ImmutableRecordError("AuditEvent rows cannot be updated.")

    def delete(self) -> NoReturn:
        raise ImmutableRecordError("AuditEvent rows cannot be deleted.")


class AuditEvent(models.Model):
    """Append-only. No updated_at on purpose: rows never change."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)  # when the server stored it
    occurred_at = models.DateTimeField(default=timezone.now)  # when it actually happened
    event_type = models.CharField(max_length=64, db_index=True)
    actor = models.ForeignKey(
        USER, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events"
    )
    actor_role = models.CharField(max_length=16, blank=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    previous_state = models.JSONField(null=True, blank=True)
    new_state = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["event_type", "occurred_at"]),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("AuditEvent rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ImmutableRecordError("AuditEvent rows cannot be deleted.")


class Outbox(BaseModel):
    """Transactional outbox: written in the same DB transaction as the business change, then
    drained by a worker (cloud sync, notifications). Always create rows via Outbox.enqueue()."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    aggregate_type = models.CharField(max_length=64)
    aggregate_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    idempotency_key = models.CharField(max_length=128, unique=True)

    # Monotonic ordering key: an order's rows always drain before its payment's rows.
    sequence = models.PositiveBigIntegerField(unique=True, null=True, blank=True, editable=False)

    # Set while a drain worker holds the row, so a restarted worker can reclaim stale locks.
    locked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "available_at"]),
            models.Index(fields=["status", "sequence"]),
        ]

    @classmethod
    def enqueue(
        cls,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> tuple["Outbox", bool]:
        """Write an outbox row. Returns (row, created).
        Call this inside the SAME transaction as the business write. Re-enqueuing the same
        idempotency_key returns the existing row instead of creating a duplicate.
        """
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [OUTBOX_LOCK_ID])

            existing = cls.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing, False

            last = cls.objects.aggregate(m=Max("sequence"))["m"] or 0
            row = cls.objects.create(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
                sequence=last + 1,
            )
            return row, True
