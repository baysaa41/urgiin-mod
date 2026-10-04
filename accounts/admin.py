from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Invite, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email", "full_name", "role", "is_active", "is_staff", "date_joined")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("email", "full_name")
    ordering = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Хувийн мэдээлэл", {"fields": ("full_name", "role")}),
        ("Эрх", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Огноо", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "full_name", "role", "password1", "password2"),
        }),
    )


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    list_display = ("email", "role", "invited_by", "created_at", "expires_at", "accepted_at")
    list_filter = ("role",)
    search_fields = ("email",)
    readonly_fields = ("token", "created_at", "accepted_at", "accepted_by")
