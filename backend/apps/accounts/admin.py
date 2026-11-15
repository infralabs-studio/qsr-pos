from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class QsrUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("POS", {"fields": ("role",)}),)  # type: ignore[operator]
    list_display = ("username", "role", "is_active")
