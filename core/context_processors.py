from django.conf import settings
from django.db import connection

def admin_notification_context(request):
    user_id = request.session.get('user_id')
    role = request.session.get('role')

    context = {
        "user_name": "Admin",
        "notifications": [],
    }

    if not user_id or role != "admin":
        return context

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT first_name, middle_name, last_name
            FROM FACULTY
            WHERE faculty_id = %s
        """, [user_id])
        row = cursor.fetchone()
        if row:
            first, middle, last = row
            middle_part = f" {middle[0]}." if middle else ""
            full_name = f"{first}{middle_part} {last}".title()
            context["user_name"] = full_name

        cursor.execute("""
            SELECT TOP 10 message, status
            FROM NOTIFICATIONS
            WHERE receiver_teacher_id = %s
            ORDER BY sent_time DESC
        """, [user_id])
        context["notifications"] = [
            {"message": r[0], "status": r[1]} for r in cursor.fetchall()
        ]

    return context
# core/context_processors.py
from django.core.cache import cache
from django.urls import reverse
from .models import SiteSetting

def branding(request):
    data = cache.get("branding_ctx")
    if data is None:
        try:
            s = SiteSetting.get_solo()
            logo = s.brand_logo.url if s.brand_logo else None
            edit_url = reverse("admin:core_sitesetting_change", args=[s.pk]) if s.pk \
                       else reverse("admin:core_sitesetting_add")
            data = {
                "brand_name": s.brand_name or "SMARTLAB",
                "brand_logo_url": logo,
                "brand_edit_url": edit_url,
            }
        except Exception:
            data = {
                "brand_name": "SMARTLAB",
                "brand_logo_url": None,
                "brand_edit_url": reverse("admin:core_sitesetting_changelist"),
            }
        cache.set("branding_ctx", data, 300)
    return data
def pusher_keys(request):
    return {
        "PUSHER_KEY": settings.PUSHER_KEY,
        "PUSHER_CLUSTER": settings.PUSHER_CLUSTER,
    }