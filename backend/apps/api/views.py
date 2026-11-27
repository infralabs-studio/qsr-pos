"""HTTP API (Step 7). Thin: validate input, call the services, serialize the result."""

from __future__ import annotations

import hmac
import json
from datetime import timedelta
from typing import Any, cast

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_POST
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.pos import payment_flow, services
from apps.pos.backfill import BackfillLine, backfill_order
from apps.pos.manual_mpesa import record_manual_mpesa_payment
from apps.pos.models import (
    MenuCategory,
    MenuItem,
    ModifierOption,
    Order,
    Outbox,
    Payment,
)
from apps.pos.outbox_sync import queue_stats
from apps.pos.payment_flow import TransitionResult, TransitionStatus
from apps.pos.providers.base import ProviderUnavailable
from apps.pos.providers.factory import get_mock_provider, get_stk_provider
from apps.pos.providers.mock_mpesa import Outcome
from apps.pos.providers.stk_callback import parse_stk_callback
from apps.pos.reconciliation import (
    manager_confirm_payment,
    manager_defer_payment,
    manager_reject_payment,
)

from .errors import NoOpenShift
from .permissions import IsManager
from .serializers import (
    BackfillSerializer,
    CashPaymentSerializer,
    ConfirmSerializer,
    DevOnlineSerializer,
    DevSimulateSerializer,
    LineSerializer,
    LoginSerializer,
    ManualMpesaSerializer,
    MenuCategorySerializer,
    OpenShiftSerializer,
    OrderCreateSerializer,
    OrderSerializer,
    PaymentSerializer,
    RejectSerializer,
    ShiftSerializer,
    StkPaymentSerializer,
    UserSerializer,
)

# ------------------------------------------------------------------------------ helpers


def _user(request: Request) -> User:
    return cast(User, request.user)


def validated(serializer_class: Any, request: Request) -> dict[str, Any]:
    serializer = serializer_class(data=request.data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def _order_queryset() -> QuerySet[Order]:
    return Order.objects.select_related("shift").prefetch_related("items__modifiers", "payments")


def _order_data(order_id: Any) -> Any:
    return OrderSerializer(get_object_or_404(_order_queryset(), pk=order_id)).data


def _payment_response(payment: Payment, created: bool, **extra: Any) -> Response:
    payment = Payment.objects.select_related("order").get(pk=payment.pk)
    body = {
        "payment": PaymentSerializer(payment).data,
        "order": _order_data(payment.order_id),
        **extra,
    }
    return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def resolve_line(data: dict[str, Any]) -> tuple[MenuItem, list[ModifierOption]]:
    menu_item = get_object_or_404(MenuItem, pk=data["menu_item"])
    ids = list(data.get("options", []))
    if len(set(ids)) != len(ids):
        raise ValidationError("Each option can only be chosen once.")
    options = list(ModifierOption.objects.filter(pk__in=ids))
    if len(options) != len(ids):
        raise ValidationError("Unknown modifier option.")
    return menu_item, options


# ---------------------------------------------------------------------------------- auth


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """Call once on load: sets the csrftoken cookie the client echoes in X-CSRFToken."""

    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        return Response({"detail": "ok"})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request: Request) -> Response:
        data = validated(LoginSerializer, request)
        user = authenticate(request, username=data["username"], password=data["password"])
        if user is None or not user.is_active:
            return Response(
                {
                    "error": {
                        "code": "invalid_credentials",
                        "messages": ["Wrong username or password."],
                    }
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        login(request, user)
        return Response(UserSerializer(user).data)


class LogoutView(APIView):
    def post(self, request: Request) -> Response:
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(_user(request)).data)


# ----------------------------------------------------------------------- shift and menu


class CurrentShiftView(APIView):
    def get(self, request: Request) -> Response:
        shift = services.get_open_shift()
        return Response({"shift": ShiftSerializer(shift).data if shift else None})


class OpenShiftView(APIView):
    permission_classes = [IsManager]

    def post(self, request: Request) -> Response:
        data = validated(OpenShiftSerializer, request)
        shift = services.open_shift(opened_by=_user(request), opening_float=data["opening_float"])
        return Response(ShiftSerializer(shift).data, status=status.HTTP_201_CREATED)


class MenuView(APIView):
    def get(self, request: Request) -> Response:
        categories = MenuCategory.objects.filter(is_active=True).prefetch_related(
            "items__modifier_groups__options"
        )
        return Response({"categories": MenuCategorySerializer(categories, many=True).data})


# ------------------------------------------------------------------------------- orders


class OrderListCreateView(APIView):
    def get(self, request: Request) -> Response:
        shift = services.get_open_shift()
        if shift is None:
            return Response({"orders": []})
        orders = _order_queryset().filter(shift=shift)
        wanted = request.query_params.get("status")
        if wanted:
            orders = orders.filter(status=wanted)
        page = orders.order_by("-created_at")[:100]
        return Response({"orders": OrderSerializer(page, many=True).data})

    def post(self, request: Request) -> Response:
        data = validated(OrderCreateSerializer, request)
        order_id = data.get("id")
        if order_id is not None:
            existing = Order.objects.filter(pk=order_id).first()
            if existing is not None:  # a replayed create returns the original
                return Response(_order_data(existing.pk))
        shift = services.get_open_shift()
        if shift is None:
            raise NoOpenShift()
        order = services.create_order(shift=shift, created_by=_user(request), order_id=order_id)
        return Response(_order_data(order.pk), status=status.HTTP_201_CREATED)


class OrderDetailView(APIView):
    def get(self, request: Request, pk: Any) -> Response:
        return Response(_order_data(pk))


class OrderItemsView(APIView):
    def post(self, request: Request, pk: Any) -> Response:
        order = get_object_or_404(Order, pk=pk)
        data = validated(LineSerializer, request)
        menu_item, options = resolve_line(data)
        services.add_item(
            order=order,
            menu_item=menu_item,
            quantity=data["quantity"],
            options=options,
            note=data["note"],
        )
        return Response(_order_data(order.pk), status=status.HTTP_201_CREATED)


class OrderChargeView(APIView):
    def post(self, request: Request, pk: Any) -> Response:
        order = get_object_or_404(Order, pk=pk)
        services.charge_order(order=order)
        return Response(_order_data(order.pk))


# ----------------------------------------------------------------------------- payments


class CashPaymentView(APIView):
    def post(self, request: Request, pk: Any) -> Response:
        order = get_object_or_404(Order, pk=pk)
        data = validated(CashPaymentSerializer, request)
        payment, created = services.confirm_cash_payment(
            order=order,
            tendered=data["tendered"],
            recorded_by=_user(request),
            idempotency_key=data["idempotency_key"],
        )
        return _payment_response(payment, created)


def _stk_extra(payment: Payment) -> dict[str, Any]:
    expires = payment.created_at + timedelta(seconds=settings.STK_TIMEOUT_SECONDS)
    return {"timeout_seconds": settings.STK_TIMEOUT_SECONDS, "expires_at": expires.isoformat()}


class StkPaymentView(APIView):
    def post(self, request: Request, pk: Any) -> Response:
        order = get_object_or_404(Order, pk=pk)
        data = validated(StkPaymentSerializer, request)
        try:
            payment, created = payment_flow.start_stk_payment(
                provider=get_stk_provider(),
                order=order,
                phone_number=data["phone_number"],
                recorded_by=_user(request),
                idempotency_key=data["idempotency_key"],
            )
        except ProviderUnavailable as exc:
            failed = (
                Payment.objects.select_related("order")
                .filter(idempotency_key=data["idempotency_key"])
                .first()
            )
            return Response(
                {
                    "error": {"code": "provider_unavailable", "messages": [str(exc)]},
                    "payment": PaymentSerializer(failed).data if failed else None,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return _payment_response(payment, created, **_stk_extra(payment))


class ManualMpesaView(APIView):
    def post(self, request: Request, pk: Any) -> Response:
        order = get_object_or_404(Order, pk=pk)
        data = validated(ManualMpesaSerializer, request)
        payment, created = record_manual_mpesa_payment(
            order=order,
            receipt_code=data["receipt_code"],
            amount=data["amount"],
            recorded_by=_user(request),
            idempotency_key=data["idempotency_key"],
            phone_number=data["phone_number"],
        )
        return _payment_response(payment, created)


class PaymentDetailView(APIView):
    """What the till polls while an STK prompt is open. An unanswered prompt times out here,
    using the server clock (the prompt may still succeed later: see the state machine)."""

    def get(self, request: Request, pk: Any) -> Response:
        payment = get_object_or_404(Payment.objects.select_related("order"), pk=pk)
        deadline = payment.created_at + timedelta(seconds=settings.STK_TIMEOUT_SECONDS)
        if (
            payment.method == Payment.Method.MPESA_STK
            and payment.status == Payment.Status.PENDING
            and deadline <= timezone.now()
        ):
            payment_flow.time_out_payment(payment)
        extra = _stk_extra(payment) if payment.method == Payment.Method.MPESA_STK else {}
        return _payment_response(payment, False, **extra)


# ------------------------------------------------------------------------------ manager


class ReviewQueueView(APIView):
    permission_classes = [IsManager]

    def get(self, request: Request) -> Response:
        waiting = Payment.objects.select_related("order").filter(
            status__in=[Payment.Status.MANAGER_REVIEW, Payment.Status.PENDING_VERIFICATION]
        )
        return Response(
            {
                "payments": PaymentSerializer(waiting.order_by("occurred_at"), many=True).data,
                "counts": {
                    "needs_review": waiting.filter(status=Payment.Status.MANAGER_REVIEW).count(),
                    "pending_verification": waiting.filter(
                        status=Payment.Status.PENDING_VERIFICATION
                    ).count(),
                },
            }
        )


class _PaymentAction(APIView):
    permission_classes = [IsManager]

    def respond(self, result: TransitionResult) -> Response:
        if result.status == TransitionStatus.REJECTED:
            return Response(
                {"error": {"code": "illegal_transition", "messages": [result.detail]}},
                status=status.HTTP_409_CONFLICT,
            )
        payment = Payment.objects.select_related("order").get(pk=result.payment.pk)
        return Response(
            {
                "result": result.status.value,
                "payment": PaymentSerializer(payment).data,
                "order": _order_data(payment.order_id),
            }
        )


class PaymentConfirmView(_PaymentAction):
    def post(self, request: Request, pk: Any) -> Response:
        payment = get_object_or_404(Payment, pk=pk)
        data = validated(ConfirmSerializer, request)
        result = manager_confirm_payment(
            payment=payment, manager=_user(request), receipt_code=data["receipt_code"]
        )
        return self.respond(result)


class PaymentRejectView(_PaymentAction):
    def post(self, request: Request, pk: Any) -> Response:
        payment = get_object_or_404(Payment, pk=pk)
        data = validated(RejectSerializer, request)
        result = manager_reject_payment(
            payment=payment, manager=_user(request), reason=data["reason"]
        )
        return self.respond(result)


class PaymentDeferView(_PaymentAction):
    def post(self, request: Request, pk: Any) -> Response:
        payment = get_object_or_404(Payment, pk=pk)
        return self.respond(manager_defer_payment(payment=payment, manager=_user(request)))


class BackfillView(APIView):
    permission_classes = [IsManager]

    def post(self, request: Request) -> Response:
        data = validated(BackfillSerializer, request)
        existing = Order.objects.filter(pk=data["id"]).first()
        if existing is not None:  # a double-submit returns the original
            return Response(_order_data(existing.pk))
        shift = services.get_open_shift()
        if shift is None:
            raise NoOpenShift()
        lines = []
        for raw in data["lines"]:
            menu_item, options = resolve_line(raw)
            lines.append(
                BackfillLine(
                    menu_item=menu_item, quantity=raw["quantity"], options=options, note=raw["note"]
                )
            )
        order, created = backfill_order(
            shift=shift,
            manager=_user(request),
            reason=data["reason"],
            occurred_at=data["occurred_at"],
            lines=lines,
            payment_key=data["payment_key"],
            cash_tendered=data.get("cash_tendered"),
            mpesa_code=data["mpesa_code"],
            mpesa_phone=data["mpesa_phone"],
            order_id=data["id"],
        )
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(_order_data(order.pk), status=code)


# ------------------------------------------------------------------------------- system


class SyncStatusView(APIView):
    """Feeds the 'N records waiting to sync' banner and the review badges."""

    def get(self, request: Request) -> Response:
        unsent = Outbox.objects.filter(sequence__isnull=False).exclude(status=Outbox.Status.SENT)
        oldest = unsent.order_by("created_at").values_list("created_at", flat=True).first()
        last_error = (
            unsent.exclude(last_error="")
            .order_by("-updated_at")
            .values_list("last_error", flat=True)
            .first()
        )
        return Response(
            {
                "queue": queue_stats(),
                "oldest_unsent_at": oldest.isoformat() if oldest else None,
                "last_error": last_error or "",
                "sync_enabled": bool(settings.CLOUD_SYNC_URL),
                "pending_verification": Payment.objects.filter(
                    status=Payment.Status.PENDING_VERIFICATION
                ).count(),
                "needs_review": Payment.objects.filter(
                    status=Payment.Status.MANAGER_REVIEW
                ).count(),
            }
        )


@csrf_exempt
@require_POST
def stk_callback(request: HttpRequest, token: str) -> JsonResponse:
    """Daraja's STK callback. The secret path token is the authenticity check, and a success
    whose amount differs from the prompt goes to manager review. In production this endpoint
    belongs on the cloud (Safaricom cannot reach a terminal behind a store router)."""
    expected = settings.MPESA_CALLBACK_TOKEN
    if expected in ("", "change-me") or not hmac.compare_digest(token.encode(), expected.encode()):
        raise Http404("Not found")
    try:
        event = parse_stk_callback(json.loads(request.body))
    except ValueError:
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Malformed callback"}, status=400)
    payment_flow.handle_provider_event(event)
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


# ----------------------------------------------------------------- dev-only (mock provider)


class _DevView(APIView):
    """Only exists with DEBUG on and the mock provider: lets the frontend exercise every M-PESA
    state without Safaricom."""

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        if not settings.DEBUG or settings.MPESA_PROVIDER != "mock":
            raise Http404("Not found")
        super().initial(request, *args, **kwargs)


class DevMpesaOnlineView(_DevView):
    def post(self, request: Request) -> Response:
        data = validated(DevOnlineSerializer, request)
        provider = get_mock_provider()
        provider.online = data["online"]
        return Response({"online": provider.online})


class DevSimulateStkView(_DevView):
    def post(self, request: Request, pk: Any) -> Response:
        payment = get_object_or_404(Payment, pk=pk)
        data = validated(DevSimulateSerializer, request)
        attempt = payment.attempts.order_by("-attempt_number").first()
        if attempt is None or not attempt.checkout_request_id:
            raise Http404("No STK prompt for this payment")
        try:
            payload = get_mock_provider().simulate(
                attempt.checkout_request_id, Outcome(data["outcome"])
            )
        except KeyError:
            raise ValidationError("The mock prompt is gone (the server restarted).") from None
        payment_flow.handle_provider_event(parse_stk_callback(payload))
        return _payment_response(payment, False)
