from django.contrib import admin
from django.http import HttpRequest

from . import models

admin.site.register(
    [
        models.MenuCategory,
        models.MenuItem,
        models.ModifierGroup,
        models.ModifierOption,
        models.StockItem,
        models.Shift,
        models.Order,
        models.OrderItem,
        models.OrderItemModifier,
        models.Payment,
        models.PaymentAttempt,
        models.PrintJob,
        models.Outbox,
    ]
)


@admin.register(models.AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "event_type", "actor", "target_type", "target_id")
    list_filter = ("event_type",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False
