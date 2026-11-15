from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user from day one: swapping AUTH_USER_MODEL after the first migration is painful."""

    class Role(models.TextChoices):
        CASHIER = "cashier", "Cashier"
        MANAGER = "manager", "Manager"

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.CASHIER)

    @property
    def is_manager(self) -> bool:
        return self.role == self.Role.MANAGER
