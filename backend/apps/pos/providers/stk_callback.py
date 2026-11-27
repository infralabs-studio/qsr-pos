"""Parsing of Daraja STK Push callbacks and result-code mapping, shared by the callback
endpoint, the mock provider and the dev simulator. Result codes follow the copy matrix: verify
them against the current Daraja documentation before relying on them."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from apps.pos.payment_states import PaymentEvent

from .base import ProviderEvent


def event_for_result_code(code: int) -> PaymentEvent:
    if code == 0:
        return PaymentEvent.SUCCEEDED
    if code == 1032:
        return PaymentEvent.CANCELLED
    return PaymentEvent.FAILED


def parse_stk_callback(payload: Any) -> ProviderEvent:
    """Turn a Daraja-shaped STK callback into a ProviderEvent. Raises ValueError for anything
    malformed, and for a success that lacks a receipt number or an amount."""
    try:
        callback = payload["Body"]["stkCallback"]
        if not isinstance(callback, dict):
            raise TypeError("stkCallback must be an object")
        checkout_id = str(callback["CheckoutRequestID"])
        code = int(callback["ResultCode"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Malformed STK callback") from exc
    if not checkout_id:
        raise ValueError("Malformed STK callback")

    metadata = callback.get("CallbackMetadata") or {}
    items = metadata.get("Item", []) if isinstance(metadata, dict) else []
    receipt = ""
    amount: Decimal | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name")
        if name == "MpesaReceiptNumber":
            receipt = str(item.get("Value", ""))
        elif name == "Amount":
            try:
                amount = Decimal(str(item["Value"]))
            except (KeyError, InvalidOperation) as exc:
                raise ValueError("Malformed amount in STK callback") from exc
            if not amount.is_finite():
                raise ValueError("Malformed amount in STK callback")

    event = event_for_result_code(code)
    if event == PaymentEvent.SUCCEEDED and (not receipt or amount is None):
        raise ValueError("Success callback without a receipt number or an amount")
    return ProviderEvent(
        event=event,
        checkout_request_id=checkout_id,
        receipt_code=receipt,
        result_code=code,
        result_desc=str(callback.get("ResultDesc", "")),
        raw=dict(payload),
        amount=amount,
    )
