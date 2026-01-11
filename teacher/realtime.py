# teacher/realtime.py
import pusher
from django.conf import settings
from django.db import connection
from django.utils import timezone
from datetime import time


class RealtimeService:
    """
    Global Pusher wrapper.
    Supports:
        - teacher notifications
        - admin notifications
        - lab realtime (active sessions)
    """

    def __init__(self):
        self.client = pusher.Pusher(
            app_id=settings.PUSHER_APP_ID,
            key=settings.PUSHER_KEY,
            secret=settings.PUSHER_SECRET,
            cluster=settings.PUSHER_CLUSTER,
            ssl=True,
        )

    # -------------------------------------------------
    # BASIC EVENT TRIGGER
    # -------------------------------------------------
    def trigger(self, channel: str, event: str, data: dict):
        """Send ANY realtime event to ANY channel."""
        return self.client.trigger(channel, event, data)

    # -------------------------------------------------
    # TEACHER NOTIFICATION
    # -------------------------------------------------
    def push_teacher_notification(self, teacher_id: int, payload: dict):
        channel = f"teacher-{teacher_id}"
        return self.trigger(channel, "notification", payload)

    # -------------------------------------------------
    # ADMIN NOTIFICATION
    # -------------------------------------------------
    def push_admin_notification(self, admin_id: int, payload: dict):
        channel = f"admin-{admin_id}"
        return self.trigger(channel, "notification", payload)

    # -------------------------------------------------
    # LAB ACTIVE SESSIONS (both teacher + admin subscribe)
    # -------------------------------------------------
    def push_active_sessions(self, lab_id: int):
        today = timezone.localdate()

        with connection.cursor() as c:
            c.execute("""
                SELECT date, start_time, end_time, requested_by
                FROM UTILIZATION_SLIP
                WHERE lab_id = %s
                  AND status = 'Active'
                  AND date = %s
            """, [lab_id, today])
            rows = c.fetchall()

        payload = {
            "active_sessions": [
                {
                    "date": d.isoformat(),
                    "start_time": (st or time(0, 0)).strftime("%H:%M:%S"),
                    "end_time": (et or time(0, 0)).strftime("%H:%M:%S"),
                    "faculty_id": fid,
                }
                for d, st, et, fid in rows
            ]
        }

        channel = f"lab-{lab_id}"
        return self.trigger(channel, "active_sessions", payload)


# GLOBAL instance importable everywhere
realtime = RealtimeService()
