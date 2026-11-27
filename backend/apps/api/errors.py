from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.pos.providers.base import ProviderUnavailable


class NoOpenShift(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Open a shift before taking orders."
    default_code = "no_open_shift"


def _error(code: str, messages: list[str], http_status: int) -> Response:
    return Response({"error": {"code": code, "messages": messages}}, status=http_status)


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """One error shape for the whole API: {"error": {"code", "messages" | "fields"}}."""
    if isinstance(exc, DjangoValidationError):
        return _error("validation_error", [str(m) for m in exc.messages], 400)
    if isinstance(exc, DjangoPermissionDenied):
        return _error("forbidden", [str(exc) or "You do not have permission to do this."], 403)
    if isinstance(exc, ProviderUnavailable):
        return _error("provider_unavailable", [str(exc)], 503)

    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, DRFValidationError):
        response.data = {"error": {"code": "validation_error", "fields": response.data}}
        return response
    detail = response.data.get("detail") if isinstance(response.data, dict) else None
    code = getattr(detail, "code", "error")
    response.data = {"error": {"code": str(code), "messages": [str(detail or "Request failed.")]}}
    return response
