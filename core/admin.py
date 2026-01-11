# core/admin.py
from django.contrib import admin
from django.utils.html import format_html
from django.core.cache import cache
from .models import SiteSetting

@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    list_display = ("brand_name", "logo_thumb", "updated_at")
    readonly_fields = ("logo_preview", "updated_at")

    fieldsets = (
        ("Branding", {
            "fields": ("brand_name", "brand_logo", "logo_preview"),
            "description": "Edit sidebar title and upload logo used across Admin & Teacher."
        }),
        ("Meta", {"fields": ("updated_at",), "classes": ("collapse",)}),
    )

    def logo_preview(self, obj):
        if obj.brand_logo:
            return format_html('<img src="{}" style="max-height:60px;">', obj.brand_logo.url)
        return "—"

    def logo_thumb(self, obj):
        if obj.brand_logo:
            return format_html('<img src="{}" style="height:24px;">', obj.brand_logo.url)
        return "—"

    # Clear cached branding so sidebars update immediately after saving in Admin
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        cache.delete("branding_ctx")
