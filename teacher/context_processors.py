from django.db import connection

def teacher_notification_context(request):
    user_id = request.session.get('user_id')
    role = request.session.get('role')

    context = {
        "teacher_name": "Teacher",
        "notifications": [],
    }

    if not user_id or role != "teacher":
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
            context["teacher_name"] = full_name

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
