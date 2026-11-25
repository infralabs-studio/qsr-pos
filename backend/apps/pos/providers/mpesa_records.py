"""Terminal-side access to the M-PESA records (Step 6). Implements TransactionSource.

Anything that is not a clean, well-formed answer raises ProviderUnavailable, so reconciliation
waits instead of drawing a conclusion from a broken connection.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .base import LedgerTransaction, ProviderUnavailable


def _malformed() -> ProviderUnavailable:
    return ProviderUnavailable("Malformed response from M-PESA records")


def _parse_transaction(item: Any) -> LedgerTransaction:
    try:
        occurred = parse_datetime(str(item["occurred_at"]))
        if occurred is None or timezone.is_naive(occurred):
            raise ValueError("occurred_at must include a timezone")
        return LedgerTransaction(
            receipt_code=str(item["receipt_code"]),
            amount=Decimal(str(item["amount"])),
            phone_number=str(item.get("phone_number", "")),
            occurred_at=occurred,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as exc:
        raise _malformed() from exc


class HttpTransactionSource:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("MPESA_RECORDS_URL must start with http:// or https://")
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderUnavailable(str(exc)) from exc

    def lookup(self, receipt_code: str) -> LedgerTransaction | None:
        data = self._get(self.base_url + urllib.parse.quote(receipt_code, safe="") + "/")
        if not isinstance(data, dict) or "found" not in data:
            raise _malformed()
        if data["found"] is False:
            return None
        if data["found"] is True:
            return _parse_transaction(data.get("transaction"))
        raise _malformed()

    def candidates(self, start: datetime, end: datetime) -> list[LedgerTransaction]:
        query = urllib.parse.urlencode({"start": start.isoformat(), "end": end.isoformat()})
        data = self._get(f"{self.base_url}?{query}")
        if not isinstance(data, dict) or not isinstance(data.get("transactions"), list):
            raise _malformed()
        return [_parse_transaction(item) for item in data["transactions"]]
