from __future__ import annotations

from typing import TYPE_CHECKING

from apps.pos.models import Payment
from apps.pos.payment_states import PaymentEvent

from .base import InitiationResult, PaymentProvider, ProviderEvent

if TYPE_CHECKING:
    from apps.pos.models import PaymentAttempt


class CashProvider(PaymentProvider):
    """Cash is settled at the till: the money is already in the cashier's hand, so the outcome
    is known immediately and there is nothing to query. Tender and change checks stay in
    services.confirm_cash_payment."""

    method = Payment.Method.CASH.value

    def initiate(self, payment: Payment, *, phone_number: str = "") -> InitiationResult:
        return InitiationResult(event=ProviderEvent(event=PaymentEvent.SUCCEEDED))

    def query_status(self, attempt: PaymentAttempt) -> ProviderEvent | None:
        return None
