"""Which STK provider the API uses. Only the mock exists until the Daraja provider (QSR-201)."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .base import PaymentProvider
from .mock_mpesa import MockMpesaProvider

_mock: MockMpesaProvider | None = None


def get_mock_provider() -> MockMpesaProvider:
    """A per-process singleton, so a prompt started in one request can be resolved in another."""
    global _mock
    if _mock is None:
        _mock = MockMpesaProvider()
    return _mock


def reset_mock_provider() -> None:
    global _mock
    _mock = None


def get_stk_provider() -> PaymentProvider:
    if settings.MPESA_PROVIDER == "mock":
        return get_mock_provider()
    raise ImproperlyConfigured(
        f"MPESA_PROVIDER={settings.MPESA_PROVIDER!r}: only 'mock' exists until QSR-201."
    )
