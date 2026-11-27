"""Mock Daraja for tests and the offline-thesis spike. No network, deterministic, scriptable.

Result codes follow the copy matrix. Verify them against the current Daraja documentation
before the real provider is built.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from apps.pos.models import Payment

from .base import (
    InitiationResult,
    LedgerTransaction,
    PaymentProvider,
    ProviderEvent,
    ProviderUnavailable,
)
from .stk_callback import event_for_result_code, parse_stk_callback

if TYPE_CHECKING:
    from apps.pos.models import PaymentAttempt


class Outcome(StrEnum):
    SUCCESS = "success"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CANCELLED = "cancelled"
    WRONG_PIN = "wrong_pin"
    UNREACHABLE = "unreachable"


RESULT_CODES: dict[Outcome, int] = {
    Outcome.SUCCESS: 0,
    Outcome.INSUFFICIENT_FUNDS: 1,
    Outcome.CANCELLED: 1032,
    Outcome.WRONG_PIN: 2001,
    Outcome.UNREACHABLE: 1037,
}

RESULT_DESCRIPTIONS: dict[Outcome, str] = {
    Outcome.SUCCESS: "The service request is processed successfully.",
    Outcome.INSUFFICIENT_FUNDS: "The balance is insufficient for the transaction.",
    Outcome.CANCELLED: "Request cancelled by user.",
    Outcome.WRONG_PIN: "Wrong PIN entered.",
    Outcome.UNREACHABLE: "The user cannot be reached.",
}


def new_receipt_code() -> str:
    return "Q" + uuid.uuid4().hex[:9].upper()  # 10 letters/digits, like QGH7XXXXXX


@dataclass
class LedgerEntry:
    amount: Decimal
    phone_number: str
    checkout_request_id: str = ""  # empty for a customer who paid the till directly
    receipt_code: str = ""
    result_code: int | None = None  # None = still pending
    result_desc: str = ""
    occurred_at: datetime = field(default_factory=timezone.now)

    @property
    def succeeded(self) -> bool:
        return self.result_code == 0


class MockLedger:
    """Stands in for Safaricom's transaction records. Reconciliation reads this later."""

    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []

    def add(self, entry: LedgerEntry) -> LedgerEntry:
        self.entries.append(entry)
        return entry

    def find(self, checkout_request_id: str) -> LedgerEntry | None:
        for entry in self.entries:
            if entry.checkout_request_id and entry.checkout_request_id == checkout_request_id:
                return entry
        return None

    def get(self, checkout_request_id: str) -> LedgerEntry:
        entry = self.find(checkout_request_id)
        if entry is None:
            raise KeyError(checkout_request_id)
        return entry

    def find_by_receipt(self, receipt_code: str) -> LedgerEntry | None:
        for entry in self.entries:
            if entry.receipt_code and entry.receipt_code == receipt_code:
                return entry
        return None

    def record_till_payment(
        self,
        *,
        amount: Decimal,
        phone_number: str,
        receipt_code: str | None = None,
        occurred_at: datetime | None = None,
    ) -> LedgerEntry:
        """A customer paid the till directly with no STK prompt: the manual M-PESA path."""
        return self.add(
            LedgerEntry(
                amount=amount,
                phone_number=phone_number,
                receipt_code=receipt_code or new_receipt_code(),
                result_code=0,
                result_desc=RESULT_DESCRIPTIONS[Outcome.SUCCESS],
                occurred_at=occurred_at or timezone.now(),
            )
        )

    def successful_between(self, start: datetime, end: datetime) -> list[LedgerEntry]:
        return [e for e in self.entries if e.succeeded and start <= e.occurred_at <= end]


class MockMpesaProvider(PaymentProvider):
    method = Payment.Method.MPESA_STK.value

    def __init__(self, ledger: MockLedger | None = None) -> None:
        self.ledger = ledger if ledger is not None else MockLedger()
        self.online = True  # flip to False to simulate internet loss

    def initiate(self, payment: Payment, *, phone_number: str = "") -> InitiationResult:
        if not self.online:
            raise ProviderUnavailable("M-PESA needs internet.")
        checkout_id = f"ws_CO_{uuid.uuid4().hex[:24]}"
        self.ledger.add(
            LedgerEntry(
                amount=payment.amount,
                phone_number=phone_number,
                checkout_request_id=checkout_id,
            )
        )
        return InitiationResult(
            checkout_request_id=checkout_id,
            merchant_request_id=f"mr_{uuid.uuid4().hex[:16]}",
        )

    def query_status(self, attempt: PaymentAttempt) -> ProviderEvent | None:
        if not self.online:
            raise ProviderUnavailable("M-PESA needs internet.")
        entry = self.ledger.find(attempt.checkout_request_id)
        if entry is None or entry.result_code is None:
            return None
        return ProviderEvent(
            event=event_for_result_code(entry.result_code),
            checkout_request_id=entry.checkout_request_id,
            receipt_code=entry.receipt_code,
            result_code=entry.result_code,
            result_desc=entry.result_desc,
        )

    # ---- test controls -------------------------------------------------------------------

    def simulate(self, checkout_request_id: str, outcome: Outcome) -> dict[str, Any]:
        """Resolve a pending prompt as Safaricom would, and return the callback payload.

        Timeout is not an outcome: it is the absence of one. Duplicate callbacks are the same
        payload delivered twice.
        """
        entry = self.ledger.get(checkout_request_id)
        entry.result_code = RESULT_CODES[outcome]
        entry.result_desc = RESULT_DESCRIPTIONS[outcome]
        callback: dict[str, Any] = {
            "MerchantRequestID": "mock-merchant-request",
            "CheckoutRequestID": checkout_request_id,
            "ResultCode": entry.result_code,
            "ResultDesc": entry.result_desc,
        }
        if outcome == Outcome.SUCCESS:
            entry.receipt_code = new_receipt_code()
            callback["CallbackMetadata"] = {
                "Item": [
                    {"Name": "Amount", "Value": float(entry.amount)},
                    {"Name": "MpesaReceiptNumber", "Value": entry.receipt_code},
                    {"Name": "PhoneNumber", "Value": int(entry.phone_number)},
                ]
            }
        return {"Body": {"stkCallback": callback}}

    def parse_callback(self, payload: dict[str, Any]) -> ProviderEvent:
        """Turn a Daraja-shaped STK callback into a ProviderEvent. Raises ValueError if
        malformed."""
        return parse_stk_callback(payload)


class MockLedgerSource:
    """Adapts MockLedger to the TransactionSource protocol. Flip .online to simulate losing
    the connection to Safaricom's records."""

    def __init__(self, ledger: MockLedger) -> None:
        self.ledger = ledger
        self.online = True

    def _check(self) -> None:
        if not self.online:
            raise ProviderUnavailable("Cannot reach M-PESA records.")

    @staticmethod
    def _convert(entry: LedgerEntry) -> LedgerTransaction:
        return LedgerTransaction(
            receipt_code=entry.receipt_code,
            amount=entry.amount,
            phone_number=entry.phone_number,
            occurred_at=entry.occurred_at,
        )

    def lookup(self, receipt_code: str) -> LedgerTransaction | None:
        self._check()
        entry = self.ledger.find_by_receipt(receipt_code)
        return self._convert(entry) if entry is not None and entry.succeeded else None

    def candidates(self, start: datetime, end: datetime) -> list[LedgerTransaction]:
        self._check()
        return [
            self._convert(entry)
            for entry in self.ledger.successful_between(start, end)
            if entry.receipt_code
        ]
