from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission


class IsManager(BasePermission):
    message = "A manager must do this."

    def has_permission(self, request: Any, view: Any) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and getattr(user, "is_manager", False))
