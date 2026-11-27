from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import User
from apps.pos.manual_mpesa import order_label
from apps.pos.models import (
    MenuCategory,
    MenuItem,
    ModifierGroup,
    ModifierOption,
    Order,
    OrderItem,
    OrderItemModifier,
    Payment,
    Shift,
)
from apps.pos.providers.mock_mpesa import Outcome

ZERO = Decimal("0")


# ------------------------------------------------------------------------------ output


class UserSerializer(serializers.ModelSerializer):
    is_manager = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "role", "is_manager"]


class ShiftSerializer(serializers.ModelSerializer):
    opened_by = serializers.CharField(source="opened_by.username")

    class Meta:
        model = Shift
        fields = ["id", "status", "opened_at", "opening_float", "opened_by"]


class ModifierOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModifierOption
        fields = ["id", "name", "price_delta", "is_default", "is_available"]


class ModifierGroupSerializer(serializers.ModelSerializer):
    is_required = serializers.BooleanField(read_only=True)
    options = ModifierOptionSerializer(many=True, read_only=True)

    class Meta:
        model = ModifierGroup
        fields = ["id", "name", "min_select", "max_select", "is_required", "options"]


class MenuItemSerializer(serializers.ModelSerializer):
    modifier_groups = ModifierGroupSerializer(many=True, read_only=True)

    class Meta:
        model = MenuItem
        fields = ["id", "name", "price", "tax_rate", "is_available", "modifier_groups"]


class MenuCategorySerializer(serializers.ModelSerializer):
    items = MenuItemSerializer(many=True, read_only=True)

    class Meta:
        model = MenuCategory
        fields = ["id", "name", "items"]


class OrderItemModifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItemModifier
        fields = ["name", "price_delta"]


class OrderItemSerializer(serializers.ModelSerializer):
    modifiers = OrderItemModifierSerializer(many=True, read_only=True)

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "menu_item",
            "name",
            "unit_price",
            "tax_rate",
            "quantity",
            "line_total",
            "note",
            "modifiers",
        ]


class PaymentSerializer(serializers.ModelSerializer):
    order_label = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "order",
            "order_label",
            "shift",
            "method",
            "status",
            "amount",
            "tendered_amount",
            "change_given",
            "phone_number",
            "mpesa_receipt_code",
            "review_reason",
            "suggested_receipt_code",
            "reconciliation_note",
            "duplicate_of",
            "idempotency_key",
            "occurred_at",
            "confirmed_at",
            "created_at",
        ]

    def get_order_label(self, obj: Payment) -> str:
        return order_label(obj.order)


class OrderSerializer(serializers.ModelSerializer):
    order_label = serializers.SerializerMethodField()
    items = OrderItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "order_label",
            "status",
            "shift",
            "subtotal",
            "discount_total",
            "tax_total",
            "total",
            "notes",
            "paid_at",
            "occurred_at",
            "is_backfilled",
            "created_at",
            "items",
            "payments",
        ]

    def get_order_label(self, obj: Order) -> str:
        return order_label(obj)


# ------------------------------------------------------------------------------- input


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class OpenShiftSerializer(serializers.Serializer):
    opening_float = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=ZERO)


class OrderCreateSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False)


class LineSerializer(serializers.Serializer):
    menu_item = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1, max_value=99, default=1)
    options = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    note = serializers.CharField(max_length=255, allow_blank=True, default="")


class CashPaymentSerializer(serializers.Serializer):
    tendered = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=ZERO)
    idempotency_key = serializers.UUIDField()


class StkPaymentSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20)
    idempotency_key = serializers.UUIDField()


class ManualMpesaSerializer(serializers.Serializer):
    receipt_code = serializers.CharField(max_length=20)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=ZERO)
    phone_number = serializers.CharField(max_length=20, allow_blank=True, default="")
    idempotency_key = serializers.UUIDField()


class ConfirmSerializer(serializers.Serializer):
    receipt_code = serializers.CharField(max_length=20, allow_blank=True, default="")


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class BackfillSerializer(serializers.Serializer):
    id = serializers.UUIDField()  # required: it is what makes a double-submit safe
    reason = serializers.CharField(max_length=255)
    occurred_at = serializers.DateTimeField()
    lines = LineSerializer(many=True, allow_empty=False)
    payment_key = serializers.UUIDField()
    cash_tendered = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=ZERO, required=False, allow_null=True
    )
    mpesa_code = serializers.CharField(max_length=20, allow_blank=True, default="")
    mpesa_phone = serializers.CharField(max_length=20, allow_blank=True, default="")


class DevOnlineSerializer(serializers.Serializer):
    online = serializers.BooleanField()


class DevSimulateSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(choices=[o.value for o in Outcome])
