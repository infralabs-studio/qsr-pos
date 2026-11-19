"""Payment state machine: pure data, no Django imports, trivial to test.

States are Payment.Status values. Events are things that happen to a payment. A (state, event)
pair missing from TRANSITIONS is illegal unless the event was already applied (an idempotent
replay), in which case it is a no-op.
"""

from __future__ import annotations

from enum import StrEnum


class PaymentEvent(StrEnum):
    SUCCEEDED = "succeeded"  # provider says the money arrived
    FAILED = "failed"  # provider says it definitively failed
    TIMED_OUT = "timed_out"  # local timeout, no answer; the prompt may still succeed
    CANCELLED = "cancelled"  # customer or cashier cancelled
    VERIFIED_EXACT = "verified_exact"  # reconciliation found an exact match
    FLAGGED_FOR_REVIEW = "flagged_for_review"  # near match, no match, or duplicate
    MANAGER_CONFIRMED = "manager_confirmed"
    MANAGER_REJECTED = "manager_rejected"
    MANAGER_DEFERRED = "manager_deferred"  # "leave pending"


TRANSITIONS: dict[tuple[str, str], str] = {
    # Live STK prompt
    ("pending", "succeeded"): "confirmed",
    ("pending", "failed"): "failed",
    ("pending", "timed_out"): "timed_out",
    ("pending", "cancelled"): "cancelled",
    # A timeout or a local cancel does not stop the prompt: money can still arrive later.
    ("timed_out", "succeeded"): "confirmed",
    ("timed_out", "failed"): "failed",
    ("timed_out", "cancelled"): "cancelled",
    ("cancelled", "succeeded"): "confirmed",
    # The provider said "failed" and then "succeeded": contradictory, never auto-confirm.
    ("failed", "succeeded"): "manager_review",
    # Manual M-PESA (created directly as pending_verification) and reconciliation
    ("pending_verification", "verified_exact"): "confirmed",
    ("pending_verification", "flagged_for_review"): "manager_review",
    ("manager_review", "manager_confirmed"): "confirmed",
    ("manager_review", "manager_rejected"): "failed",
    ("manager_review", "manager_deferred"): "pending_verification",
    # "confirmed" is terminal: refunds and voids are a separate manager flow.
}

# For each event, the states that mean "this event has already been applied".
ALREADY_APPLIED: dict[str, frozenset[str]] = {
    "succeeded": frozenset({"confirmed", "manager_review"}),
    "failed": frozenset({"failed"}),
    "timed_out": frozenset({"timed_out"}),
    "cancelled": frozenset({"cancelled"}),
    "verified_exact": frozenset({"confirmed"}),
    "flagged_for_review": frozenset({"manager_review"}),
    "manager_confirmed": frozenset({"confirmed"}),
    "manager_rejected": frozenset({"failed"}),
    "manager_deferred": frozenset({"pending_verification"}),
}


def target_status(current: str, event: PaymentEvent) -> str | None:
    return TRANSITIONS.get((current, event.value))


def is_already_applied(current: str, event: PaymentEvent) -> bool:
    return current in ALREADY_APPLIED.get(event.value, frozenset())
