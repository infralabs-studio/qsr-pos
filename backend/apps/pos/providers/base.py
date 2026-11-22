"""Provider contract shared by cash and M-PESA (QSR-133)."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from django.core.exceptions import ValidationError

from apps.pos.payment_states import PaymentEvent

if TYPE_CHECKING:
    from apps.pos.models import Payment, PaymentAttempt


class ProviderUnavailable(Exception):
    """The provider cannot be reached (terminal offline, Daraja down)."""


@dataclass(frozen=True)
class ProviderEvent:
    """A result reported by a provider: a callback or a status-query answer."""

    event: PaymentEvent
    checkout_request_id: str = ""
    receipt_code: str = ""
    result_code: int | None = None
    result_desc: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InitiationResult:
    checkout_request_id: str = ""
    merchant_request_id: str = ""
    # Set when the outcome is known immediately (cash). None means "waiting for a callback".
    event: ProviderEvent | None = None


class PaymentProvider(ABC):
    method: str

    @abstractmethod
    def initiate(self, payment: Payment, *, phone_number: str = "") -> InitiationResult:
        """Start the payment with the provider. Talks to the outside world only: no DB writes.

        Raises ProviderUnavailable if the provider cannot be reached.
        """

    @abstractmethod
    def query_status(self, attempt: PaymentAttempt) -> ProviderEvent | None:
        """Ask the provider for the outcome of an attempt. None means no final answer yet."""


_MSISDN_RE = re.compile(r"^(?:\+?254|0)?([71]\d{8})$")


def normalize_msisdn(raw: str) -> str:
    """Return a Safaricom number as 2547XXXXXXXX / 2541XXXXXXXX, or raise (copy M-01)."""
    match = _MSISDN_RE.match(re.sub(r"[\s-]", "", raw))
    if match is None:
        raise ValidationError("Enter a Safaricom number like 07XX XXX XXX.")
    return "254" + match.group(1)


@dataclass(frozen=True)
class LedgerTransaction:
    """One transaction in Safaricom's records."""

    receipt_code: str
    amount: Decimal
    phone_number: str
    occurred_at: datetime


class TransactionSource(Protocol):
    """Where reconciliation looks money up. Both methods raise ProviderUnavailable when the
    records cannot be reached (terminal offline, Daraja down)."""

    def lookup(self, receipt_code: str) -> LedgerTransaction | None: ...

    def candidates(self, start: datetime, end: datetime) -> list[LedgerTransaction]: ...
