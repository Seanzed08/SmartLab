import base64
from gettext import translation
import io
import json
import os
from collections import defaultdict
from datetime import date, time, timedelta, datetime as dt
import zipfile

from PyPDF2 import PdfMerger
from django.conf import settings
from django.contrib import messages  # NOTE: correct messages import
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.core.mail import send_mail
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_GET
from django.db import transaction
import requests

from .realtime import realtime


# ---------------------------
# Header / notifications
# ---------------------------
def get_teacher_header_context(user_id):
    context = {
        "teacher_name": "Teacher",
        "teacher_profile_image": None,
        "notifications": [],
        "unread_count": 0,
        "MEDIA_URL": settings.MEDIA_URL,

        "PUSHER_KEY": getattr(settings, "PUSHER_KEY", ""),
        "PUSHER_CLUSTER": getattr(settings, "PUSHER_CLUSTER", ""),
    }

    try:
        with connection.cursor() as cursor:
            # Name + photo
            cursor.execute("""
                SELECT first_name, middle_name, last_name, profile_image
                FROM FACULTY
                WHERE faculty_id = %s AND is_archived = 0
            """, [user_id])
            row = cursor.fetchone()
            if row:
                first, middle, last, profile_image = row
                middle_part = f" {middle[0]}." if middle else ""
                context["teacher_name"] = f"{first}{middle_part} {last}".strip().title()
                context["teacher_profile_image"] = profile_image

            # NOTIFICATIONS (SQL Server): use notification_id, alias as id
            cursor.execute("""
                SELECT TOP 10
                       notification_id AS id,
                       message,
                       status,
                       created_at
                FROM NOTIFICATIONS
                WHERE receiver_teacher_id = %s
                ORDER BY notification_id DESC
            """, [user_id])
            rows = cursor.fetchall()
            context["notifications"] = [
                {"id": r[0], "message": r[1], "status": r[2], "created_at": r[3]}
                for r in rows
            ]

            # Unread count
            cursor.execute("""
                SELECT COUNT(*)
                FROM NOTIFICATIONS
                WHERE receiver_teacher_id = %s AND status = 'Unread'
            """, [user_id])
            context["unread_count"] = cursor.fetchone()[0]

    except Exception as e:
        print(f"❌ Error in get_teacher_header_context({user_id}):", e)

    return context


# ---------------------------
# Attendance + printing
# ---------------------------
def _none_if_empty(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s == "" or s.lower() == "none" else v

def _int_or_none(v):
    v = _none_if_empty(v)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def _date_or_none_strict(v):
    v = _none_if_empty(v)
    if v is None:
        return None
    try:
        return dt.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None

def _fmt_hhmm(tobj):
    return tobj.strftime('%H:%M') if tobj else "—"




# =========================
# SSRS helpers (same as admin pattern)
# =========================

def _make_auth_headers_and_authobj():
    mode = getattr(settings, "SSRS_AUTH_MODE", "NONE").upper()
    if mode == "NTLM":
        try:
            from requests_ntlm import HttpNtlmAuth
        except ImportError:
            raise RuntimeError("requests-ntlm is not installed. Run: pip install requests-ntlm")
        user = getattr(settings, "SSRS_NTLM_USER", "")
        pwd  = getattr(settings, "SSRS_NTLM_PASS", "")
        return ({}, HttpNtlmAuth(user, pwd))
    elif mode == "BASIC":
        user = getattr(settings, "SSRS_BASIC_USER", "")
        pwd  = getattr(settings, "SSRS_BASIC_PASS", "")
        token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        return ({"Authorization": f"Basic {token}"}, None)
    else:
        return ({}, None)

def _ssrs_pdf(report_path, params, timeout=60):
    base = settings.SSRS_BASE_URL.rstrip('/')
    qp = [("rs:Command", "Render"), ("rs:Format", "PDF")]
    for k, v in params.items():
        if v is not None:
            qp.append((k, str(v)))
    query = "&".join(
        f"{requests.utils.quote(k, safe=':')}={requests.utils.quote(v)}"
        for k, v in qp
    )
    url = f"{base}?{requests.utils.quote(report_path, safe='/')}&{query}"

    headers, auth = _make_auth_headers_and_authobj()
    last = None
    for _ in (1, 2):  # one retry on 5xx
        try:
            r = requests.get(
                url, auth=auth, headers=headers,
                verify=getattr(settings, "SSRS_VERIFY_TLS", False),
                timeout=timeout
            )
        except requests.RequestException as ex:
            raise requests.HTTPError(f"SSRS request failed for {url}\n{ex}") from ex

        if r.status_code == 200:
            return r.content

        last = r
        print("SSRS status:", r.status_code, r.reason)
        print("SSRS body:", r.text[:1500])
        if r.status_code < 500:
            break

    raise requests.HTTPError(f"{last.status_code} {last.reason} for {url}\n\n{last.text[:4000]}")


# =========================
# Query helpers
# =========================

def _filtered_utilization_ids_for_teacher(teacher_id, date_from=None, date_to=None, lab_id=None):
    """
    Return list of utilization_id that match:
      - status Completed
      - requested_by = teacher_id
      - optional date_from/date_to
      - optional lab_id
    """
    with connection.cursor() as cursor:
        q = [
            "SELECT u.utilization_id",
            "FROM UTILIZATION_SLIP u",
            "WHERE u.status = 'Completed' AND u.requested_by = %s"
        ]
        p = [teacher_id]
        if date_from:
            q.append("AND u.date >= %s"); p.append(date_from)
        if date_to:
            q.append("AND u.date <= %s"); p.append(date_to)
        if lab_id is not None:
            q.append("AND u.lab_id = %s"); p.append(lab_id)
        q.append("ORDER BY u.date DESC, u.utilization_id DESC")
        cursor.execute(" ".join(q), p)
        return [row[0] for row in cursor.fetchall()]


# =========================
# VIEWS
# =========================

def attendance_records(request):
    """
    Completed attendance list (teacher-only) with filters:
      - date_from (optional) and date_to (optional)
      - lab (optional)
    """
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    context = get_teacher_header_context(teacher_id)

    # Filters
    date_from_str = (request.GET.get("date_from") or "").strip()
    date_to_str   = (request.GET.get("date_to") or "").strip()
    lab_id_str    = (request.GET.get("lab_id") or "").strip()

    date_from_val = _date_or_none_strict(date_from_str)
    date_to_val   = _date_or_none_strict(date_to_str)
    lab_id_val    = _int_or_none(lab_id_str)

    where = ["u.status = 'Completed'", "u.requested_by = %s"]
    params = [teacher_id]

    if date_from_val:
        where.append("u.date >= %s"); params.append(date_from_val)
    if date_to_val:
        where.append("u.date <= %s"); params.append(date_to_val)
    if lab_id_val is not None:
        where.append("u.lab_id = %s"); params.append(lab_id_val)

    where_sql = " AND ".join(where)

    with connection.cursor() as cursor:
        # teacher's assigned lab (optional header info)
        cursor.execute("SELECT lab_id FROM LABORATORIES WHERE faculty_id = %s", [teacher_id])
        row = cursor.fetchone()
        assigned_lab = row[0] if row else None

        # Labs dropdown respecting same filters
        cursor.execute(f"""
            SELECT DISTINCT l.lab_id, l.lab_num
            FROM UTILIZATION_SLIP u
            JOIN LABORATORIES l  ON u.lab_id = l.lab_id
            JOIN LAB_SCHEDULE ls ON u.schedule_id = ls.schedule_id
            JOIN ASSIGNED_TEACHER at ON ls.assigned_teacher_id = at.assigned_teacher_id
            WHERE {where_sql}
            ORDER BY l.lab_num
        """, params)
        labs = [{"id": r[0], "num": r[1]} for r in cursor.fetchall()]

        # Records
        cursor.execute(f"""
            SELECT 
                u.utilization_id,
                u.date,
                l.lab_num,
                c.course_code,
                u.student_year_and_section,
                u.start_time,
                u.end_time,
                (SELECT COUNT(*) FROM COMPUTER_LAB_ATTENDANCE a WHERE a.utilization_id = u.utilization_id) AS student_count,
                s.term,
                s.school_year
            FROM UTILIZATION_SLIP u
            JOIN LABORATORIES l       ON u.lab_id = l.lab_id
            JOIN LAB_SCHEDULE ls      ON u.schedule_id = ls.schedule_id
            JOIN ASSIGNED_TEACHER at  ON ls.assigned_teacher_id = at.assigned_teacher_id
            JOIN COURSE c             ON at.course_id = c.course_id
            LEFT JOIN SEMESTER s      ON at.semester_id = s.semester_id
            WHERE {where_sql}
            ORDER BY u.date DESC, u.utilization_id DESC
        """, params)
        rows = cursor.fetchall()

    records = []
    for (util_id, date_val, lab_num, course_code, section, start_time, end_time, scount, term, sy) in rows:
        records.append({
            "utilization_id": util_id,
            "date": date_val,
            "lab": lab_num,
            "course": course_code,
            "section": section,
            "time_duration": f"{_fmt_hhmm(start_time)} - {_fmt_hhmm(end_time)}",
            "student_count": scount,
            "semester_text": f"{term}, A.Y. {sy}" if term and sy else "—",
        })

    context.update({
        "admin_name": context.get("teacher_name"),
        "assigned_lab": assigned_lab,
        "current_page": "Attendance",
        "attendance_records": records,
        # Echo filters back to template
        "f_date_from": date_from_str if date_from_val else "",
        "f_date_to": date_to_str if date_to_val else "",
        "f_lab_id": str(lab_id_val) if lab_id_val is not None else "",
        "labs": labs,
    })
    return render(request, "teacher/attendance_records.html", context)


def attendance_record_preview(request, utilization_id):
    """JSON for preview modal—teacher can only view their own records."""
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return JsonResponse({"error": "Unauthorized"}, status=401)

    teacher_id = request.session.get("user_id")

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 
                u.utilization_id,
                u.date, 
                u.start_time,
                u.end_time,
                l.lab_num,
                CONCAT(f.first_name, ' ', f.last_name) AS teacher_name,
                c.course_code,
                u.student_year_and_section,
                COALESCE(u.remarks,'') AS remarks,
                u.processed_by,
                u.created_at,
                s.term,
                s.school_year
            FROM UTILIZATION_SLIP u
            JOIN LABORATORIES l       ON u.lab_id = l.lab_id
            JOIN FACULTY f            ON u.requested_by = f.faculty_id AND f.is_archived = 0
            JOIN LAB_SCHEDULE ls      ON u.schedule_id = ls.schedule_id
            JOIN ASSIGNED_TEACHER at  ON ls.assigned_teacher_id = at.assigned_teacher_id
            JOIN COURSE c             ON at.course_id = c.course_id
            LEFT JOIN SEMESTER s      ON at.semester_id = s.semester_id
            WHERE u.utilization_id = %s AND u.requested_by = %s
        """, [utilization_id, teacher_id])
        row = cursor.fetchone()
        if not row:
            return JsonResponse({"error": "Not found or unauthorized."}, status=404)

        (util_id, date_val, start_time, end_time, lab_num, teacher_name, 
         course_code, section, remarks, processed_by_id, created_at, term, sy) = row

        cursor.execute("""
            SELECT CONCAT(s.first_name, ' ', COALESCE(s.middle_name,''), ' ', s.last_name)
            FROM COMPUTER_LAB_ATTENDANCE a
            JOIN STUDENTS s ON a.student_id = s.student_id AND s.is_archived = 0
            WHERE a.utilization_id = %s
            ORDER BY s.last_name, s.first_name
        """, [utilization_id])
        students = [n.strip().replace("  ", " ") for (n,) in cursor.fetchall()]
        student_count = len(students)

        processed_by_name = "N/A"
        if processed_by_id:
            cursor.execute("""
                SELECT CONCAT(first_name, ' ', last_name)
                FROM FACULTY WHERE faculty_id = %s AND is_archived = 0
            """, [processed_by_id])
            pr = cursor.fetchone()
            if pr:
                processed_by_name = pr[0]

    payload = {
        "utilization_id": util_id,
        "date": date_val.strftime("%Y-%m-%d"),
        "time": f"{_fmt_hhmm(start_time)} - {_fmt_hhmm(end_time)}",
        "lab_num": lab_num,
        "teacher_name": teacher_name,
        "course_code": course_code,
        "section": section,
        "remarks": remarks or "N/A",
        "processed_by": processed_by_name,
        "processed_date": created_at.strftime("%Y-%m-%d") if created_at else "—",
        "semester_ay": f"{term}, A.Y. {sy}" if term and sy else "—",
        "student_count": student_count,
        "students": students,
        "print_url": request.build_absolute_uri(
        reverse("teacher_print_both_ssrs", args=[utilization_id])
    ),
    }
    return JsonResponse(payload, status=200)


def print_combined_slip(request, utilization_id):
    """Printable page (teacher-locked) combining details + student list."""
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 
                u.date, 
                u.start_time,
                u.end_time,
                l.lab_num,
                CONCAT(f.first_name, ' ', f.last_name) AS teacher_name, 
                c.course_code, 
                u.student_year_and_section,
                COALESCE(u.remarks,'') AS remarks, 
                u.processed_by, 
                u.created_at,
                s.term, 
                s.school_year
            FROM UTILIZATION_SLIP u
            JOIN LABORATORIES l       ON u.lab_id = l.lab_id
            JOIN FACULTY f            ON u.requested_by = f.faculty_id AND f.is_archived = 0
            JOIN LAB_SCHEDULE ls      ON u.schedule_id = ls.schedule_id
            JOIN ASSIGNED_TEACHER at  ON ls.assigned_teacher_id = at.assigned_teacher_id
            JOIN COURSE c             ON at.course_id = c.course_id
            LEFT JOIN SEMESTER s      ON at.semester_id = s.semester_id
            WHERE u.utilization_id = %s AND u.requested_by = %s
        """, [utilization_id, teacher_id])
        row = cursor.fetchone()
        if not row:
            return HttpResponse("Unauthorized or not found.", status=403)

        (date_val, start_time, end_time, lab_num, teacher_name, course_code, section,
         remarks, processed_by_id, created_at, term, sy) = row

        cursor.execute("SELECT COUNT(*) FROM COMPUTER_LAB_ATTENDANCE WHERE utilization_id = %s", [utilization_id])
        student_count = cursor.fetchone()[0]

        processed_by_name = "N/A"
        if processed_by_id:
            cursor.execute("""
                SELECT CONCAT(first_name, ' ', last_name)
                FROM FACULTY WHERE faculty_id = %s AND is_archived = 0
            """, [processed_by_id])
            name_row = cursor.fetchone()
            if name_row:
                processed_by_name = name_row[0]

        cursor.execute("""
            SELECT CONCAT(s.first_name, ' ', COALESCE(s.middle_name,''), ' ', s.last_name)
            FROM COMPUTER_LAB_ATTENDANCE a
            JOIN STUDENTS s ON a.student_id = s.student_id AND s.is_archived = 0
            WHERE a.utilization_id = %s
            ORDER BY s.last_name, s.first_name
        """, [utilization_id])
        students = [r[0].strip().replace("  ", " ") for r in cursor.fetchall()]

    time_duration = f"{_fmt_hhmm(start_time)} - {_fmt_hhmm(end_time)}"
    remaining = max(0, 30 - len(students))
    semester_ay = f"{term}, A.Y. {sy}" if term and sy else "—"

    context = {
        "utilization_id": utilization_id,
        "date": date_val,
        "time_duration": time_duration,
        "lab_num": lab_num,
        "teacher_name": teacher_name,
        "course_code": course_code,
        "course_name": course_code,
        "section": section,
        "year_section": section,
        "students_present": student_count,
        "remarks": remarks or "N/A",
        "processed_by": processed_by_name,
        "processed_date": created_at.strftime("%Y-%m-%d") if created_at else "—",
        "students": students,
        "semester_ay": semester_ay,
        "request_date": date_val.strftime("%B %d, %Y"),
        "instructor_name": teacher_name,
        "lab_assistant_name": processed_by_name,
        "room_number": lab_num,
        "date_time": f"{date_val.strftime('%B %d, %Y')} / {time_duration}",
        "range": range(1, 31),
        "blank_rows": range(1, remaining + 1),
    }
    return render(request, "teacher/print_combined_slip.html", context)


# =========================
# Bulk print/export for teacher (SSRS-backed)
# =========================

def teacher_print_queue(request):
    """Merged single PDF for this teacher’s filtered records."""
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    date_from   = _date_or_none_strict(request.GET.get("date_from"))
    date_to     = _date_or_none_strict(request.GET.get("date_to"))
    lab_id      = _int_or_none(request.GET.get("lab_id"))
    t           = (request.GET.get("type") or "both").lower()  # both | utilization | attendance

    ids = _filtered_utilization_ids_for_teacher(teacher_id, date_from, date_to, lab_id)
    if not ids:
        return HttpResponse("No records to print for the selected filters.", status=404)

    merger = PdfMerger()
    try:
        for uid in ids:
            if t in ("both", "utilization"):
                merger.append(io.BytesIO(_ssrs_pdf(
                    settings.SSRS_UTILIZATION_REPORT_PATH,
                    {settings.SSRS_UTIL_PARAM_NAME: uid}
                )))
            if t in ("both", "attendance"):
                merger.append(io.BytesIO(_ssrs_pdf(
                    settings.SSRS_ATTENDANCE_REPORT_PATH,
                    {settings.SSRS_ATTEND_PARAM_NAME: uid}
                )))
    except requests.HTTPError as e:
        return HttpResponse(f"SSRS error: {e}", status=502)

    out = io.BytesIO(); merger.write(out); merger.close()
    resp = HttpResponse(out.getvalue(), content_type="application/pdf")
    resp['Content-Disposition'] = 'inline; filename=teacher_print_queue.pdf'
    return resp

def teacher_export_merged_pdf(request):
    """Download merged single PDF for this teacher’s filtered records."""
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    date_from  = _date_or_none_strict(request.GET.get("date_from"))
    date_to    = _date_or_none_strict(request.GET.get("date_to"))
    lab_id     = _int_or_none(request.GET.get("lab_id"))
    t          = (request.GET.get("type") or "both").lower()

    ids = _filtered_utilization_ids_for_teacher(teacher_id, date_from, date_to, lab_id)
    if not ids:
        return HttpResponse("No records to export for the selected filters.", status=404)

    merger = PdfMerger()
    try:
        for uid in ids:
            if t in ("both", "utilization"):
                merger.append(io.BytesIO(_ssrs_pdf(
                    settings.SSRS_UTILIZATION_REPORT_PATH,
                    {settings.SSRS_UTIL_PARAM_NAME: uid}
                )))
            if t in ("both", "attendance"):
                merger.append(io.BytesIO(_ssrs_pdf(
                    settings.SSRS_ATTENDANCE_REPORT_PATH,
                    {settings.SSRS_ATTEND_PARAM_NAME: uid}
                )))
    except requests.HTTPError as e:
        return HttpResponse(f"SSRS error: {e}", status=502)

    out = io.BytesIO()
    merger.write(out)
    merger.close()

    # dt is datetime class: from datetime import datetime as dt
    filename = f"attendance_export_{t}_{dt.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    resp = HttpResponse(out.getvalue(), content_type="application/pdf")
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp



def teacher_export_zip(request):
    """Download a ZIP of individual PDFs for this teacher’s filtered records."""
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    date_from   = _date_or_none_strict(request.GET.get("date_from"))
    date_to     = _date_or_none_strict(request.GET.get("date_to"))
    lab_id      = _int_or_none(request.GET.get("lab_id"))
    t           = (request.GET.get("type") or "both").lower()

    ids = _filtered_utilization_ids_for_teacher(teacher_id, date_from, date_to, lab_id)
    if not ids:
        return HttpResponse("No records to export for the selected filters.", status=404)

    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for uid in ids:
                if t in ("both", "utilization"):
                    zf.writestr(f"UtilizationSlip_{uid}.pdf",
                                _ssrs_pdf(settings.SSRS_UTILIZATION_REPORT_PATH,
                                          {settings.SSRS_UTIL_PARAM_NAME: uid}))
                if t in ("both", "attendance"):
                    zf.writestr(f"AttendanceSheet_{uid}.pdf",
                                _ssrs_pdf(settings.SSRS_ATTENDANCE_REPORT_PATH,
                                          {settings.SSRS_ATTEND_PARAM_NAME: uid}))
    except requests.HTTPError as e:
        return HttpResponse(f"SSRS error: {e}", status=502)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp['Content-Disposition'] = f'attachment; filename=teacher_exports_{t}_{ts}.zip'
    return resp


# =========================
# Teacher SSRS single-session prints with auth check
# =========================

def teacher_print_utilization_slip(request, utilization_id):
    """Direct SSRS Utilization Slip PDF for this teacher’s session."""
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")
    teacher_id = request.session.get("user_id")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM UTILIZATION_SLIP WHERE utilization_id=%s AND requested_by=%s",
            [utilization_id, teacher_id]
        )
        if not cursor.fetchone():
            return HttpResponse("Unauthorized or not found.", status=403)

    try:
        pdf = _ssrs_pdf(
            settings.SSRS_UTILIZATION_REPORT_PATH,
            {settings.SSRS_UTIL_PARAM_NAME: utilization_id}
        )
    except requests.HTTPError as e:
        return HttpResponse(f"SSRS error: {e}", status=502)

    resp = HttpResponse(pdf, content_type="application/pdf")
    resp['Content-Disposition'] = f'inline; filename=UtilizationSlip_{utilization_id}.pdf'
    return resp


def teacher_print_attendance_sheet(request, utilization_id):
    """Direct SSRS Attendance Sheet PDF for this teacher’s session."""
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")
    teacher_id = request.session.get("user_id")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM UTILIZATION_SLIP WHERE utilization_id=%s AND requested_by=%s",
            [utilization_id, teacher_id]
        )
        if not cursor.fetchone():
            return HttpResponse("Unauthorized or not found.", status=403)

    try:
        pdf = _ssrs_pdf(
            settings.SSRS_ATTENDANCE_REPORT_PATH,
            {settings.SSRS_ATTEND_PARAM_NAME: utilization_id}
        )
    except requests.HTTPError as e:
        return HttpResponse(f"SSRS error: {e}", status=502)

    resp = HttpResponse(pdf, content_type="application/pdf")
    resp['Content-Disposition'] = f'inline; filename=AttendanceSheet_{utilization_id}.pdf'
    return resp
def teacher_print_both_ssrs(request, utilization_id):
    """
    Single-session merged PDF (Utilization + Attendance) via SSRS,
    teacher-locked.
    """
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")

    # Make sure this utilization belongs to the logged-in teacher
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM UTILIZATION_SLIP WHERE utilization_id=%s AND requested_by=%s",
            [utilization_id, teacher_id]
        )
        if not cursor.fetchone():
            return HttpResponse("Unauthorized or not found.", status=403)

    merger = PdfMerger()
    try:
        # Utilization slip
        util_pdf = _ssrs_pdf(
            settings.SSRS_UTILIZATION_REPORT_PATH,
            {settings.SSRS_UTIL_PARAM_NAME: utilization_id}
        )
        merger.append(io.BytesIO(util_pdf))

        # Attendance sheet
        att_pdf = _ssrs_pdf(
            settings.SSRS_ATTENDANCE_REPORT_PATH,
            {settings.SSRS_ATTEND_PARAM_NAME: utilization_id}
        )
        merger.append(io.BytesIO(att_pdf))

    except requests.HTTPError as e:
        return HttpResponse(f"SSRS error: {e}", status=502)

    out = io.BytesIO()
    merger.write(out)
    merger.close()

    resp = HttpResponse(out.getvalue(), content_type="application/pdf")
    resp['Content-Disposition'] = f'inline; filename=Session_{utilization_id}_both.pdf'
    return resp

# ---------------------------
# Dashboard
# ---------------------------

def teacher_dashboard(request):
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    now = timezone.localtime()
    today = now.date()
    current_time = now.time()

    # Header context (profile, etc.)
    context = get_teacher_header_context(teacher_id)

    with connection.cursor() as cursor:
        # Teacher full name + assigned lab (via LABORATORIES.faculty_id)
        cursor.execute("""
            SELECT f.first_name, f.middle_name, f.last_name, l.lab_id
            FROM FACULTY f
            LEFT JOIN LABORATORIES l ON l.faculty_id = f.faculty_id
            WHERE f.faculty_id = %s AND f.is_archived = 0
        """, [teacher_id])
        res = cursor.fetchone()
        first_name, middle_name, last_name, assigned_lab = (res or (None, None, None, None))
        middle_part = f" {middle_name[0]}." if middle_name else ""
        full_name = f"{first_name}{middle_part} {last_name}".title() if first_name else "Teacher"

        # Any active slip NOW for this teacher
        cursor.execute("""
            SELECT u.utilization_id, l.lab_num, u.start_time, u.end_time
            FROM UTILIZATION_SLIP u
            JOIN LABORATORIES l ON u.lab_id = l.lab_id
            WHERE u.requested_by = %s
              AND u.status = 'Active'
              AND u.date = %s
              AND u.start_time <= %s
              AND u.end_time >= %s
            ORDER BY u.start_time DESC
        """, [teacher_id, today, current_time, current_time])
        active_session = cursor.fetchone()

    context.update({
        "admin_name": full_name,
        "assigned_lab": assigned_lab,
        "active_session": ({
            "id": active_session[0],
            "lab": active_session[1],
            "start": active_session[2].strftime("%H:%M") if active_session and active_session[2] else "—",
            "end": active_session[3].strftime("%H:%M") if active_session and active_session[3] else "—",
        } if active_session else None),
        "current_page": "Dashboard",
    })
    return render(request, "teacher/teacher_dashboard.html", context)


def teacher_dashboard_data(request):
    teacher_id = request.session.get("user_id")
    if not teacher_id:
        return JsonResponse({"active_session": None, "students": []})

    now = timezone.localtime()
    today = now.date()
    current_time = now.time()

    data = {"active_session": None, "students": []}

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 
                u.utilization_id,
                l.lab_num,
                u.start_time,
                u.end_time,
                f.first_name,
                f.middle_name,
                f.last_name,
                u.student_year_and_section,
                CAST(COALESCE(u.remarks,'') AS NVARCHAR(4000)) AS remarks,
                c.course_name,
                CONCAT(sem.term, ' SY ', sem.school_year) AS term_label
            FROM UTILIZATION_SLIP u
            JOIN LABORATORIES l ON u.lab_id = l.lab_id
            JOIN FACULTY f ON u.requested_by = f.faculty_id AND f.is_archived = 0
            LEFT JOIN ASSIGNED_TEACHER at ON at.assigned_teacher_id = u.assigned_teacher_id
            LEFT JOIN COURSE c ON c.course_id = at.course_id
            LEFT JOIN SEMESTER sem ON sem.semester_id = at.semester_id
            WHERE u.requested_by = %s
              AND u.status = 'Active'
              AND u.date = %s
              AND u.start_time <= %s
              AND u.end_time >= %s
            ORDER BY u.start_time DESC
        """, [teacher_id, today, current_time, current_time])

        session = cursor.fetchone()

        if session:
            (
                utilization_id, lab_num, start, end,
                fname, mname, lname, year_sec,
                remarks, course_name, term_label
            ) = session

            name_mid = f" {mname[0]}." if mname else ""
            full_name = f"{fname}{name_mid} {lname}".title()

            data["active_session"] = {
                "lab": f"Lab {lab_num}",
                "start": start.strftime("%H:%M") if start else "—",
                "end": end.strftime("%H:%M") if end else "—",
                "teacher": full_name,
                "course": course_name or "—",
                "section": year_sec or "—",
                "remarks": remarks or "",
                "semester": term_label or "—"
            }

            cursor.execute("""
                SELECT s.first_name, s.last_name, a.time_in, a.time_out
                FROM COMPUTER_LAB_ATTENDANCE a
                JOIN STUDENTS s ON a.student_id = s.student_id AND s.is_archived = 0
                WHERE a.utilization_id = %s
                ORDER BY a.time_in, a.attendance_sheet_id
            """, [utilization_id])

            for row in cursor.fetchall():
                data["students"].append({
                    "name": f"{row[0]} {row[1]}",
                    "time_in": row[2].strftime("%H:%M:%S") if row[2] else "—",
                    "time_out": row[3].strftime("%H:%M:%S") if row[3] else "—"
                })

    return JsonResponse(data)


# ---------------------------
# Schedule helpers
# ---------------------------
def _monday_of(date_obj: date) -> date:
    return date_obj - timedelta(days=date_obj.weekday())  # Monday

def _sunday_of(date_obj: date) -> date:
    return _monday_of(date_obj) + timedelta(days=6)

def _db_day_of_week(py_weekday: int) -> int:
    """
    OPERATING_TIME.day_of_week is 0=Sunday..6=Saturday
    Python weekday() is 0=Monday..6=Sunday
    Convert py_weekday -> db value
    """
    return (py_weekday + 1) % 7


# -------------------------------------------------
# Page context (UNCHANGED)
# -------------------------------------------------
def _build_schedule_context(teacher_id: int, base_date: date, selected_lab_id: str | None = None):
    start_of_week = _monday_of(base_date)
    end_of_week = _sunday_of(base_date)
    week_range_display = f"{start_of_week.strftime('%b %d')} - {end_of_week.strftime('%d, %Y')}"
    prev_week = (start_of_week - timedelta(days=7)).isoformat()
    next_week = (start_of_week + timedelta(days=7)).isoformat()

    full_name, assigned_lab, is_assigned = "Teacher", None, False
    courses, labs = [], []
    time_slots, schedule_map = [], {}

    with connection.cursor() as cursor:
        # Teacher info
        cursor.execute("""
            SELECT f.first_name, f.last_name, l.lab_id
            FROM FACULTY f
            LEFT JOIN LABORATORIES l ON l.faculty_id = f.faculty_id
            WHERE f.faculty_id = %s AND ISNULL(f.is_archived,0) = 0
        """, [teacher_id])
        row = cursor.fetchone()
        if row:
            full_name = f"{(row[0] or '').title()} {(row[1] or '').title()}".strip() or "Teacher"
            assigned_lab = row[2]
            is_assigned = bool(assigned_lab)

        # COURSES for the displayed week
        cursor.execute("""
            SELECT DISTINCT c.course_id, c.course_code
            FROM ASSIGNED_TEACHER at
            JOIN COURSE   c ON at.course_id   = c.course_id
            JOIN SEMESTER s ON at.semester_id = s.semester_id
            WHERE at.faculty_id = %s
              AND ISNULL(at.is_active, 1) = 1
              AND s.start_date <= %s AND s.end_date >= %s
            ORDER BY c.course_code
        """, [teacher_id, end_of_week, start_of_week])
        courses = cursor.fetchall()

        # All labs
        cursor.execute("""
            SELECT l.lab_id, l.lab_num
            FROM LABORATORIES l
            ORDER BY l.lab_num
        """)
        labs = cursor.fetchall()

        # Teacher's week snapshot
        cursor.execute("""
            SELECT s.date, s.start_time, s.end_time,
                   c.course_code,
                   COALESCE(s.student_year_and_section, '') AS section,
                   COALESCE(f.first_name, ''), COALESCE(f.last_name, '')
            FROM LAB_SCHEDULE s
            JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
            JOIN COURSE c ON at.course_id = c.course_id
            JOIN FACULTY f ON at.faculty_id = f.faculty_id AND ISNULL(f.is_archived,0) = 0
            WHERE at.faculty_id = %s
              AND s.date BETWEEN %s AND %s
              AND UPPER(LTRIM(RTRIM(s.status))) NOT IN ('CANCELLED','REJECTED')
            ORDER BY s.date, s.start_time
        """, [teacher_id, start_of_week, end_of_week])
        rows = cursor.fetchall()

    schedule_map = defaultdict(lambda: defaultdict(dict))
    time_set = set()
    for d, st, et, course_code, section, fn, ln in rows:
        weekday = d.strftime("%A")
        time_label = f"{st.strftime('%H:%M')} - {et.strftime('%H:%M')}"
        teacher_name = f"{(fn or '').strip()} {(ln or '').strip()}".strip()
        schedule_map[weekday][time_label] = {
            "teacher": teacher_name,
            "course": course_code,
            "section": section or "",
            "time": time_label,
        }
        time_set.add(time_label)

    time_slots = sorted(time_set)

    # Default selected lab
    labs_list = list(labs)
    selected_lab_id = str(selected_lab_id) if selected_lab_id not in (None, "") else None
    if not selected_lab_id:
        if assigned_lab:
            selected_lab_id = str(assigned_lab)
        elif labs_list:
            selected_lab_id = str(labs_list[0][0])

    # Prefill modal's lab if only one
    form_vals = {}
    if len(labs_list) == 1:
        form_vals["lab_id"] = str(labs_list[0][0])

    return {
        "is_assigned": is_assigned,
        "assigned_lab": assigned_lab,
        "courses": courses,
        "labs": labs,
        "selected_lab_id": selected_lab_id or "",
        "schedule_map": dict(schedule_map),
        "time_slots": time_slots,
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "week_range_display": week_range_display,
        "prev_week": prev_week,
        "next_week": next_week,
        "base_week": start_of_week.isoformat(),
        "teacher_name": full_name,
        "form_vals": form_vals,
    }


# -------------------------------------------------
# Page (UNCHANGED)
# -------------------------------------------------
def teacher_schedule(request):
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    week_str = request.GET.get("week")
    try:
        base_date = dt.strptime(week_str, "%Y-%m-%d").date() if week_str else date.today()
    except ValueError:
        base_date = date.today()

    selected_lab_id = request.GET.get("lab_id") or None

    try:
        from .views import get_teacher_header_context  # optional
        context = get_teacher_header_context(teacher_id)
    except Exception:
        context = {}

    context["current_page"] = "Schedule"
    context.update(_build_schedule_context(teacher_id, base_date, selected_lab_id))

    if request.GET.get("error"):
        context["error"] = request.GET.get("error")
        context["open_modal"] = True
    if request.GET.get("success"):
        context["success"] = request.GET.get("success")

    return render(request, "teacher/teacher_schedule.html", context)



# -------------------------------------------------
# Create schedule
# -------------------------------------------------
def create_schedule(request):
    """
    AJAX-first endpoint.
    - If XHR: returns JSON (no redirect). On error, nothing is cleared; frontend shows toast.
    - If non-XHR (fallback): same validations, redirects with ?error= or ?success=.
    """
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)
        return redirect("login")

    teacher_id = request.session.get("user_id")

    if request.method != "POST":
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "POST required"}, status=405)
        return redirect("teacher_schedule")

    # capture form values
    lab_id = request.POST.get("lab_id")
    course_id = request.POST.get("course_id")
    selected_days = request.POST.getlist("days")
    start_time_str = request.POST.get("start_time")
    end_time_str = request.POST.get("end_time")
    date_start = request.POST.get("start_date")
    date_end = request.POST.get("end_date")
    section = request.POST.get("student_year_and_section")

    def _fmt(d: date) -> str:
        return d.strftime("%Y-%m-%d")

    # ---------- basic validations ----------
    def err(msg, status=400):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": msg}, status=status)
        return redirect(f"{reverse('teacher_schedule')}?error={msg}&modal=1")

    if not (lab_id and course_id and selected_days and start_time_str and end_time_str and date_start and date_end and section):
        return err("Please fill out all required fields")

    try:
        start_date = dt.strptime(date_start, "%Y-%m-%d").date()
        end_date = dt.strptime(date_end, "%Y-%m-%d").date()
    except ValueError:
        return err("Invalid date format")

    if start_date > end_date:
        return err("Start date must be on or before End date")

    day_map = {'Monday':0,'Tuesday':1,'Wednesday':2,'Thursday':3,'Friday':4,'Saturday':5,'Sunday':6}
    selected_idx = {day_map[d] for d in selected_days if d in day_map}
    if not selected_idx:
        return err("Please select at least one day")

    try:
        t_start = dt.strptime(start_time_str, "%H:%M").time()
        t_end   = dt.strptime(end_time_str, "%H:%M").time()
    except ValueError:
        return err("Invalid time format")

    if t_start >= t_end:
        return err("Start Time must be before End Time")

    # ✅ enforce 3–5 hour duration
    dur_minutes = (dt.combine(date.today(), t_end) - dt.combine(date.today(), t_start)).seconds // 60
    if dur_minutes < 180 or dur_minutes > 300:
        return err("Duration must be between 3 and 5 hours (inclusive)")

    # ---------- create loop ----------
    created_count = 0
    closed_days = []
    out_of_hours_days = []
    no_assignment_days = []
    lab_conflict_days = []
    teacher_conflict_days = []

    with connection.cursor() as cursor:
        current_date = start_date
        while current_date <= end_date:
            if current_date.weekday() not in selected_idx:
                current_date += timedelta(days=1)
                continue

            # 1) Operating time lookup (and FK)
            db_dow = _db_day_of_week(current_date.weekday())
            cursor.execute("""
                SELECT operating_time_id, is_open, start_time, end_time
                FROM OPERATING_TIME
                WHERE day_of_week = %s
            """, [db_dow])
            ot_row = cursor.fetchone()
            if not ot_row:
                closed_days.append(current_date)
                current_date += timedelta(days=1)
                continue

            operating_time_id, is_open, ot_start, ot_end = ot_row[0], bool(ot_row[1]), ot_row[2], ot_row[3]
            if not is_open or not (ot_start and ot_end):
                closed_days.append(current_date)
                current_date += timedelta(days=1)
                continue

            if not (ot_start <= t_start and t_end <= ot_end):
                out_of_hours_days.append(
                    f"{current_date.strftime('%Y-%m-%d')} "
                    f"(Lab open {ot_start.strftime('%I:%M %p')}–{ot_end.strftime('%I:%M %p')})"
                )
                current_date += timedelta(days=1)
                continue

            # 2) Assignment validity (semester window)
            cursor.execute("""
                SELECT TOP 1 at.assigned_teacher_id
                FROM ASSIGNED_TEACHER at
                JOIN SEMESTER s ON s.semester_id = at.semester_id
                JOIN FACULTY f  ON f.faculty_id = at.faculty_id AND ISNULL(f.is_archived,0) = 0
                WHERE at.faculty_id = %s
                  AND at.course_id  = %s
                  AND %s BETWEEN s.start_date AND s.end_date
                ORDER BY s.start_date DESC
            """, [teacher_id, course_id, current_date])
            row = cursor.fetchone()
            if not row:
                no_assignment_days.append(current_date)
                current_date += timedelta(days=1)
                continue
            assigned_teacher_id = row[0]

            # 3) Conflicts
            # ✅ LAB CONFLICT: ignore Cancelled/Rejected schedules
            cursor.execute("""
                SELECT COUNT(*)
                FROM LAB_SCHEDULE
                WHERE lab_id = %s
                  AND date = %s
                  AND UPPER(LTRIM(RTRIM(ISNULL(status, '')))) NOT IN ('CANCELLED','REJECTED')
                  AND NOT (end_time <= %s OR start_time >= %s)
            """, [lab_id, current_date, start_time_str, end_time_str])
            (lab_conflict_count,) = cursor.fetchone() or (0,)
            if lab_conflict_count > 0:
                lab_conflict_days.append(current_date)
                current_date += timedelta(days=1)
                continue

            # ✅ TEACHER CONFLICT: ignore Cancelled/Rejected schedules
            cursor.execute("""
                SELECT COUNT(*)
                FROM LAB_SCHEDULE s
                JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
                WHERE at.faculty_id = %s
                  AND s.date = %s
                  AND UPPER(LTRIM(RTRIM(ISNULL(s.status, '')))) NOT IN ('CANCELLED','REJECTED')
                  AND NOT (s.end_time <= %s OR s.start_time >= %s)
            """, [teacher_id, current_date, start_time_str, end_time_str])
            (t_conflict_count,) = cursor.fetchone() or (0,)
            if t_conflict_count > 0:
                teacher_conflict_days.append(current_date)
                current_date += timedelta(days=1)
                continue

            # 4) Insert (with operating_time_id FK)
            cursor.execute("""
                INSERT INTO LAB_SCHEDULE (
                    lab_id, assigned_teacher_id, reserved_to, date,
                    start_time, end_time, student_year_and_section, status,
                    operating_time_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'Scheduled', %s)
            """, [
                lab_id, assigned_teacher_id, teacher_id, current_date,
                start_time_str, end_time_str, section,
                operating_time_id
            ])
            created_count += 1
            current_date += timedelta(days=1)

    # ---------- finalize ----------
    if created_count == 0:
        parts = []
        if closed_days:
            parts.append("Closed days: " + ", ".join(_fmt(d) for d in closed_days))
        if out_of_hours_days:
            parts.append("Outside hours: " + " | ".join(out_of_hours_days))
        if no_assignment_days:
            parts.append("No assignment: " + ", ".join(_fmt(d) for d in no_assignment_days))
        if lab_conflict_days:
            parts.append("Lab conflicts: " + ", ".join(_fmt(d) for d in lab_conflict_days))
        if teacher_conflict_days:
            parts.append("Teacher conflicts: " + ", ".join(_fmt(d) for d in teacher_conflict_days))
        detail = " | ".join(parts) if parts else "Check Operating Time, overlaps, semester assignment, and selected days."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": detail}, status=400)
        return redirect(f"{reverse('teacher_schedule')}?error={detail}&modal=1")

    skipped = (
        len(closed_days) + len(out_of_hours_days) + len(no_assignment_days) +
        len(lab_conflict_days) + len(teacher_conflict_days)
    )
    msg = f"{created_count} slot(s) created"
    if skipped:
        msg += f"; {skipped} day(s) skipped"

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "ok": True,
            "message": msg,
            "created": created_count,
            "skipped": skipped
        })

    return redirect(f"{reverse('teacher_schedule')}?success={msg}")



# -------------------------------------------------
# Week data (AJAX) — now returns schedule_id as "id"
# -------------------------------------------------
@require_POST
def update_schedule_section(request):
    user_id = request.session.get("user_id")
    role = request.session.get("role")

    if not user_id or role != "teacher":
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=401)

    action = (request.POST.get("action") or "update").strip().lower()
    new_section = (request.POST.get("student_year_and_section") or "").strip()
    course_id_raw = (request.POST.get("course_id") or "").strip()

    if action != "cancel" and not new_section:
        return JsonResponse({"success": False, "error": "Section cannot be empty"}, status=400)

    lab_id = (request.POST.get("lab_id") or "").strip()
    date_str = (request.POST.get("date") or "").strip()
    start_time = (request.POST.get("start_time") or "").strip()
    end_time = (request.POST.get("end_time") or "").strip()

    schedule_id = None
    schedule_id_raw = (request.POST.get("schedule_id") or "").strip()
    if schedule_id_raw and schedule_id_raw.lower() != "null":
        try:
            schedule_id = int(schedule_id_raw)
        except (TypeError, ValueError):
            schedule_id = None

    with connection.cursor() as cur, transaction.atomic():
        # 1) Find schedule_id if missing, and be sure it belongs to this teacher
        if schedule_id is None:
            if not (lab_id and date_str and start_time and end_time):
                return JsonResponse({"success": False, "error": "Missing schedule locator"}, status=400)

            cur.execute(
                """
                SELECT TOP 1 s.schedule_id
                FROM LAB_SCHEDULE s
                JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
                WHERE s.lab_id = %s
                  AND s.date = %s
                  AND s.start_time = %s
                  AND s.end_time = %s
                  AND at.faculty_id = %s
                  AND UPPER(LTRIM(RTRIM(ISNULL(s.status,'')))) NOT IN ('CANCELLED','REJECTED')
                ORDER BY s.schedule_id
                """,
                [lab_id, date_str, start_time, end_time, user_id],
            )
            row = cur.fetchone()
            if not row:
                return JsonResponse({"success": False, "error": "Schedule not found for locator"}, status=404)
            schedule_id = int(row[0])

        # 2) Make sure the schedule really belongs to this teacher
        cur.execute(
            """
            SELECT at.faculty_id, s.date, s.status, ISNULL(s.student_year_and_section,'')
            FROM LAB_SCHEDULE s
            JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
            WHERE s.schedule_id = %s
            """,
            [schedule_id],
        )
        row = cur.fetchone()
        if not row:
            return JsonResponse({"success": False, "error": "Schedule not found"}, status=404)

        faculty_id, sched_date, before_status, before_section = row
        if int(faculty_id) != int(user_id):
            return JsonResponse({"success": False, "error": "You can only modify your own schedules."}, status=403)

        # 3) CANCEL
        if action == "cancel":
            cur.execute(
                "UPDATE LAB_SCHEDULE SET status = 'Cancelled' WHERE schedule_id = %s",
                [schedule_id],
            )
            rows_affected = cur.rowcount

            cur.execute(
                "SELECT status, ISNULL(student_year_and_section,'') FROM LAB_SCHEDULE WHERE schedule_id = %s",
                [schedule_id],
            )
            row2 = cur.fetchone()
            after_status, after_section = row2 if row2 else (None, None)

            return JsonResponse(
                {
                    "success": rows_affected > 0,
                    "schedule_id": schedule_id,
                    "action": "cancel",
                    "rows_affected": rows_affected,
                    "before_status": before_status,
                    "after_status": after_status,
                    "before_section": before_section,
                    "after_section": after_section,
                    "message": "Schedule cancelled." if rows_affected > 0 else "Nothing was cancelled.",
                }
            )

        # 4) UPDATE (change section + optional course)
        new_assigned_teacher_id = None
        if course_id_raw:
            try:
                new_course_id = int(course_id_raw)
            except (TypeError, ValueError):
                return JsonResponse({"success": False, "error": "Invalid course id"}, status=400)

            cur.execute(
                """
                SELECT TOP 1 at.assigned_teacher_id
                FROM ASSIGNED_TEACHER at
                JOIN SEMESTER s ON s.semester_id = at.semester_id
                WHERE at.faculty_id = %s
                  AND at.course_id  = %s
                  AND %s BETWEEN s.start_date AND s.end_date
                ORDER BY s.start_date DESC
                """,
                [faculty_id, new_course_id, sched_date],
            )
            row2 = cur.fetchone()
            if not row2:
                return JsonResponse(
                    {"success": False, "error": "No valid assignment found for this course on the schedule date"},
                    status=400,
                )
            new_assigned_teacher_id = int(row2[0])

        if new_assigned_teacher_id is not None:
            cur.execute(
                """
                UPDATE LAB_SCHEDULE
                SET student_year_and_section = %s,
                    assigned_teacher_id = %s
                WHERE schedule_id = %s
                """,
                [new_section, new_assigned_teacher_id, schedule_id],
            )
        else:
            cur.execute(
                """
                UPDATE LAB_SCHEDULE
                SET student_year_and_section = %s
                WHERE schedule_id = %s
                """,
                [new_section, schedule_id],
            )

        rows_affected = cur.rowcount

        cur.execute(
            "SELECT status, ISNULL(student_year_and_section,'') FROM LAB_SCHEDULE WHERE schedule_id = %s",
            [schedule_id],
        )
        row3 = cur.fetchone()
        after_status, after_section = row3 if row3 else (None, None)

    return JsonResponse(
        {
            "success": rows_affected > 0,
            "schedule_id": schedule_id,
            "action": "update",
            "rows_affected": rows_affected,
            "before_status": before_status,
            "after_status": after_status,
            "before_section": before_section,
            "after_section": after_section,
            "section": after_section,
            "message": "Schedule updated." if rows_affected > 0 else "Nothing was changed.",
        }
    )
# ---------------------------
# Active sessions feed
# ---------------------------




# ---------------------------
# Availability page
# -------------------------
# ================== Helpers ==================
# --- helpers ---
def _db_day_of_week(py_weekday: int) -> int:
    """OPERATING_TIME.day_of_week is 0=Sun..6=Sat, Python weekday() is 0=Mon..6=Sun"""
    return (py_weekday + 1) % 7


def _fmt_am_pm(t: time) -> str:
    """Format time as 'h:mm AM/PM' without leading zero; safe for None."""
    if t is None:
        return "—"
    return t.strftime("%I:%M %p").lstrip("0")


def _fmt_hhmm(tobj):
    return tobj.strftime('%H:%M') if tobj else "—"


# Shared redirect that preserves form fields + week + toast
def _back_with_error(request, msg: str, lab_id=None, week=None):
    from urllib.parse import quote_plus
    qs = [("open", "request")]
    if msg:
        qs.append(("error", quote_plus(str(msg))))

    fields = {
        "lab_id": lab_id or request.POST.get("lab_id") or request.GET.get("lab_id"),
        "course_id": request.POST.get("course_id") or request.GET.get("course_id"),
        "student_year_and_section": request.POST.get("student_year_and_section") or request.GET.get("student_year_and_section"),
        "date": request.POST.get("date") or request.GET.get("date"),
        "start_time": request.POST.get("start_time") or request.GET.get("start_time"),
        "end_time": request.POST.get("end_time") or request.GET.get("end_time"),
        "remarks": request.POST.get("remarks") or request.GET.get("remarks"),
    }
    for k, v in fields.items():
        if v not in (None, ""):
            qs.append((k, quote_plus(str(v))))

    if week:
        qs.append(("week", week))
    elif request.GET.get("week"):
        qs.append(("week", request.GET.get("week")))

    query = "&".join([f"{k}={v}" for k, v in qs])
    return redirect(f"{reverse('view_lab_availability')}?{query}")


# ================== MAIN PAGE ==================
def view_lab_availability(request):
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    try:
        context = get_teacher_header_context(teacher_id)
    except NameError:
        context = {}

    schedule_map = defaultdict(lambda: defaultdict(list))
    class_index = {}
    class_any = set()  # (date, start_time, end_time) seen in LAB_SCHEDULE
    time_set = set()

    # Pending modal (for selected lab)
    pending_requests = []
    show_requests = False
    pending_count = 0
    pending_lab_id = None
    pending_lab_num = None
    user_is_incharge_of_pending_lab = False

    # Right-side "Your Requests"
    own_requests = []
    own_requests_map = defaultdict(lambda: defaultdict(list))
    request_time_set = set()

    teacher_courses = []
    today = date.today()

    # ---- Compact names
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT first_name, middle_name, last_name
            FROM FACULTY
            WHERE faculty_id = %s AND ISNULL(is_archived,0)=0
        """, [teacher_id])
        row = cursor.fetchone()

    if row:
        first, middle, last = row
        mid_part = f" {middle[0]}." if middle else ""
        teacher_fullname = f"{(first or '').strip()}{mid_part} {(last or '').strip()}".strip().title()
        user_shortname = f"{(first or '')[:1].upper()}. {(last or '').title()}".strip()
    else:
        teacher_fullname = "You"
        user_shortname = "You"

    with connection.cursor() as cursor:
        # Lab you manage
        cursor.execute("SELECT lab_id FROM LABORATORIES WHERE faculty_id = %s", [teacher_id])
        row = cursor.fetchone()
        assigned_lab = row[0] if row else None

        # All labs
        cursor.execute("SELECT lab_id, lab_num FROM LABORATORIES ORDER BY lab_num")
        labs = cursor.fetchall()

        # Selected lab (defaults to your assigned, else first)
        selected_lab_id = request.GET.get("lab_id")
        if not selected_lab_id:
            if assigned_lab is not None:
                selected_lab_id = str(assigned_lab)
            elif labs:
                selected_lab_id = str(labs[0][0])

        # Selected lab number
        selected_lab_num = None
        if selected_lab_id:
            cursor.execute("SELECT lab_num FROM LABORATORIES WHERE lab_id = %s", [selected_lab_id])
            row = cursor.fetchone()
            if row:
                selected_lab_num = row[0]

        # Your active courses (current semester)
        cursor.execute("""
            SELECT 
                c.course_id,
                CONCAT(c.course_code, ' — ', c.course_name) AS label
            FROM ASSIGNED_TEACHER at
            JOIN COURSE   c ON c.course_id = at.course_id
            JOIN SEMESTER s ON s.semester_id = at.semester_id
            WHERE at.faculty_id = %s
              AND CAST(GETDATE() AS DATE) BETWEEN s.start_date AND s.end_date
              AND ISNULL(at.is_active,1) = 1
            ORDER BY c.course_code
        """, [teacher_id])
        teacher_courses = cursor.fetchall()

        # Week window
        week_str = request.GET.get("week")
        try:
            base_date = dt.strptime(week_str, "%Y-%m-%d").date() if week_str else today
        except ValueError:
            base_date = today

        start_of_week = base_date - timedelta(days=base_date.weekday())  # Monday
        end_of_week = start_of_week + timedelta(days=6)                  # Sunday
        week_range_display = f"{start_of_week.strftime('%B %d')} - {end_of_week.strftime('%d, %Y')}"
        prev_week = (start_of_week - timedelta(days=7)).isoformat()
        next_week = (start_of_week + timedelta(days=7)).isoformat()

        # Active sessions (slips)
        active_sessions = set()
        if selected_lab_id:
            cursor.execute("""
                SELECT date, start_time, end_time, requested_by
                FROM UTILIZATION_SLIP
                WHERE lab_id = %s AND status = 'Active'
            """, [selected_lab_id])
            active_sessions = {
                (r[0], (r[1] or time(0, 0)).replace(microsecond=0), (r[2] or time(0, 0)).replace(microsecond=0), r[3])
                for r in cursor.fetchall()
            }

        # LAB_SCHEDULE → grid (hide Cancelled/Rejected)
        if selected_lab_id:
            cursor.execute("""
            SELECT
                s.schedule_id,
                s.date, s.start_time, s.end_time,
                COALESCE(f.first_name,''), COALESCE(f.last_name,''), f.faculty_id,
                COALESCE(c.course_code,''), COALESCE(s.student_year_and_section,'')
            FROM LAB_SCHEDULE s
                LEFT JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
                LEFT JOIN COURSE c            ON at.course_id = c.course_id
                LEFT JOIN FACULTY f           ON at.faculty_id = f.faculty_id AND f.is_archived = 0
                WHERE s.lab_id = %s
                  AND s.date BETWEEN %s AND %s
                  AND UPPER(LTRIM(RTRIM(ISNULL(s.status,'')))) NOT IN ('CANCELLED','REJECTED')
                ORDER BY s.date, s.start_time
            """, [selected_lab_id, start_of_week, end_of_week])

            seen_keys = set()
            for (sched_id, d, st, et, fn, ln, fid, course_code, year_sec) in cursor.fetchall():
                weekday = d.strftime("%A")
                time_key_24 = f"{st.strftime('%H:%M')} - {et.strftime('%H:%M')}"
                time_label_12 = f"{_fmt_am_pm(st)} - {_fmt_am_pm(et)}"
                dedup_key = ("class", d, st, et, fid)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                # remember this slot exists as a schedule (for later de-dupe)
                class_any.add((d, st, et))

                is_active = (d, st.replace(microsecond=0), et.replace(microsecond=0), fid) in active_sessions

                entry = {
                    "schedule_id": sched_id,   # ✅ keep schedule_id
                    "kind": "class",
                    "faculty": f"{fn} {ln}".strip(),
                    "course": course_code or "",
                    "year_section": year_sec,
                    "time": time_key_24,
                    "time_display": time_label_12,
                    "active": is_active,
                    "status": "",
                    "status_class": "",
                    "date": d.isoformat(),
                    "start_time": st.strftime("%H:%M:%S"),
                    "end_time": et.strftime("%H:%M:%S"),
                    "faculty_id": fid,
                    "remarks": "",
                }
                schedule_map[weekday][time_key_24].append(entry)
                class_index[(d, st, et, fid)] = schedule_map[weekday][time_key_24][-1]
                time_set.add(time_key_24)

        # UTILIZATION_SLIP Approved/Active → merge to grid (only ORPHAN slips, no schedule link)
        if selected_lab_id:
            cursor.execute("""
                SELECT
                    u.date, u.start_time, u.end_time,
                    COALESCE(f.first_name,''), COALESCE(f.last_name,''), u.requested_by,
                    COALESCE(u.student_year_and_section,''),
                    CAST(COALESCE(u.remarks,'') AS NVARCHAR(4000)),
                    COALESCE(u.status,''),
                    COALESCE(c.course_code,''),
                    u.schedule_id
                FROM UTILIZATION_SLIP u
                LEFT JOIN FACULTY f ON u.requested_by = f.faculty_id AND f.is_archived = 0
                LEFT JOIN ASSIGNED_TEACHER at ON u.assigned_teacher_id = at.assigned_teacher_id
                LEFT JOIN COURSE  c ON at.course_id = c.course_id
                WHERE u.lab_id = %s
                  AND u.date BETWEEN %s AND %s
                  AND u.status IN ('Approved','Active')
                  AND u.schedule_id IS NULL
                ORDER BY u.date, u.start_time
            """, [selected_lab_id, start_of_week, end_of_week])

            seen_keys = set()
            for (d, st, et, fn, ln, req_by, year_sec, remarks, status, course_code, sched_id) in cursor.fetchall():
                if (d, st, et) in class_any:
                    continue

                weekday = d.strftime("%A")
                time_key_24 = f"{st.strftime('%H:%M')} - {et.strftime('%H:%M')}"
                time_label_12 = f"{_fmt_am_pm(st)} - {_fmt_am_pm(et)}"
                dedup_key = ("slip", d, st, et, req_by)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                class_key = (d, st, et, req_by)
                is_active = (d, st.replace(microsecond=0), et.replace(microsecond=0), req_by) in active_sessions

                if (remarks or "").strip() == "Auto RFID Check-in":
                    if class_key in class_index:
                        class_index[class_key]["active"] = True
                    continue

                if class_key in class_index:
                    ref = class_index[class_key]
                    ref["active"] = ref["active"] or is_active
                    ref["status"] = status or ref.get("status", "")
                    ref["status_class"] = (status or ref.get("status", "")).strip().lower()
                    continue

                entry = {
                    "kind": "class",
                    "faculty": f"{fn} {ln}".strip(),
                    "course": course_code or "",
                    "year_section": year_sec,
                    "time": time_key_24,
                    "time_display": time_label_12,
                    "active": is_active,
                    "status": status or "",
                    "status_class": (status or "").strip().lower(),
                    "date": d.isoformat(),
                    "start_time": st.strftime("%H:%M:%S"),
                    "end_time": et.strftime("%H:%M:%S"),
                    "faculty_id": req_by,
                    "remarks": "",
                }
                schedule_map[weekday][time_key_24].append(entry)
                time_set.add(time_key_24)

        # UTILIZATION_SLIP Pending → grid “Requests” section (includes YOUR OWN pending)
        if selected_lab_id:
            cursor.execute("""
                SELECT
                    u.date, u.start_time, u.end_time,
                    COALESCE(f.first_name,''), COALESCE(f.last_name,''), u.requested_by,
                    COALESCE(u.student_year_and_section,'') AS section,
                    CAST(COALESCE(u.remarks,'') AS NVARCHAR(4000)) AS remarks,
                    COALESCE(u.status,'') AS status,
                    COALESCE(c1.course_code, c2.course_code, '') AS course_code
                FROM UTILIZATION_SLIP u
                LEFT JOIN FACULTY f ON u.requested_by = f.faculty_id AND f.is_archived = 0
                LEFT JOIN ASSIGNED_TEACHER at1 ON u.assigned_teacher_id = at1.assigned_teacher_id
                LEFT JOIN COURSE c1 ON at1.course_id = c1.course_id
                LEFT JOIN ASSIGNED_TEACHER at2 ON at2.faculty_id = u.requested_by
                LEFT JOIN SEMESTER s2 ON s2.semester_id = at2.semester_id AND u.date BETWEEN s2.start_date AND s2.end_date
                LEFT JOIN COURSE c2 ON at2.course_id = c2.course_id
                WHERE u.lab_id = %s
                  AND u.date BETWEEN %s AND %s
                  AND u.status = 'Pending'
                ORDER BY u.date, u.start_time
            """, [selected_lab_id, start_of_week, end_of_week])

            seen_pending = set()
            for (d, st, et, fn, ln, req_by, section, remarks, status, course_code) in cursor.fetchall():
                dedup_key = (d, st, et, req_by, (course_code or ''), (section or ''))
                if dedup_key in seen_pending:
                    continue
                seen_pending.add(dedup_key)

                weekday = d.strftime("%A")
                time_key_24 = f"{st.strftime('%H:%M')} - {et.strftime('%H:%M')}"
                time_label_12 = f"{_fmt_am_pm(st)} - {_fmt_am_pm(et)}"
                short_name = f"{(fn or '')[:1].upper()}. {(ln or '').title()}".strip()

                entry = {
                    "kind": "request",
                    "requested_by": f"{fn} {ln}".strip(),
                    "requested_by_short": short_name,
                    "course": course_code or "",
                    "section": section or "",
                    "time": time_key_24,
                    "time_display": time_label_12,
                    "active": False,
                    "status": status or "Pending",
                    "status_class": "pending",
                    "date": d.isoformat(),
                    "start_time": st.strftime("%H:%M:%S"),
                    "end_time": et.strftime("%H:%M:%S"),
                    "faculty_id": req_by,
                    "remarks": remarks or "",
                }
                schedule_map[weekday][time_key_24].append(entry)
                time_set.add(time_key_24)

        # ========== Pending modal: EXCLUDES your own ==========
        pending_lab_id = None
        if selected_lab_id:
            pending_lab_id = int(selected_lab_id)
        elif assigned_lab:
            pending_lab_id = int(assigned_lab)

        if pending_lab_id:
            # Label + in-charge status
            cursor.execute("SELECT lab_num, faculty_id FROM LABORATORIES WHERE lab_id = %s", [pending_lab_id])
            _lab_row = cursor.fetchone()
            if _lab_row:
                pending_lab_num, pending_incharge_id = _lab_row[0], _lab_row[1]
                user_is_incharge_of_pending_lab = (pending_incharge_id == teacher_id)

            # Pull pending slips (latest first) excluding your own
            cursor.execute("""
                SELECT u.utilization_id, u.date, u.start_time, u.end_time,
                       f.first_name, f.last_name, u.student_year_and_section,
                       CAST(COALESCE(u.remarks,'') AS NVARCHAR(4000)) AS remarks,
                       u.status, COALESCE(c.course_code,'') AS course_code,
                       u.created_at, u.lab_id, l.lab_num, u.requested_by
                FROM UTILIZATION_SLIP u
                JOIN FACULTY f ON u.requested_by = f.faculty_id AND f.is_archived = 0
                LEFT JOIN ASSIGNED_TEACHER at ON u.assigned_teacher_id = at.assigned_teacher_id
                LEFT JOIN COURSE c ON at.course_id = c.course_id
                LEFT JOIN LABORATORIES l ON u.lab_id = l.lab_id
                WHERE u.lab_id = %s
                  AND UPPER(LTRIM(RTRIM(u.status))) IN ('PENDING','FOR APPROVAL')
                  AND u.requested_by <> %s
                ORDER BY u.created_at DESC, u.date DESC, u.start_time DESC, u.utilization_id DESC
            """, [pending_lab_id, teacher_id])

            seen_cards = set()
            for row in cursor.fetchall():
                (util_id, d, st, et, fn, ln, section, remarks, status, course_code,
                 created_at, lab_id_val, lab_num_val, req_by) = row
                card_key = (d, st, et, fn, ln, section, course_code, status)
                if card_key in seen_cards:
                    continue
                seen_cards.add(card_key)

                # Authority: lab-in-charge, OR non-incharge but approving a slot that overlaps their OWN schedule
                can_approve = user_is_incharge_of_pending_lab
                if not can_approve and (st and et):
                    cursor.execute("""
                        SELECT TOP 1 1
                        FROM LAB_SCHEDULE s
                        LEFT JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
                        WHERE s.lab_id = %s
                          AND s.date = %s
                          AND NOT (s.end_time <= %s OR s.start_time >= %s)
                          AND at.faculty_id = %s
                    """, [lab_id_val, d, st, et, teacher_id])
                    if cursor.fetchone():
                        can_approve = True

                pending_requests.append({
                    "id": util_id,
                    "date": d,
                    "start_time": st,
                    "end_time": et,
                    "faculty": f"{fn} {ln}",
                    "section": section,
                    "remarks": remarks,
                    "status": status,
                    "course": course_code,
                    "can_approve": can_approve,
                    "lab_num": lab_num_val,
                    "lab_id": lab_id_val,
                })

            pending_count = len(pending_requests)
            show_requests = pending_count > 0

        # ===== Your own requests (right pane) — show ALL statuses this week
        cursor.execute("""
            SELECT
                   u.date, u.start_time, u.end_time,
                   l.lab_num, u.student_year_and_section,
                   CAST(COALESCE(u.remarks,'') AS NVARCHAR(4000)) AS remarks,
                   u.status, COALESCE(c.course_code,'') AS course_code
            FROM UTILIZATION_SLIP u
            LEFT JOIN LABORATORIES l ON u.lab_id = l.lab_id
            LEFT JOIN ASSIGNED_TEACHER at ON u.assigned_teacher_id = at.assigned_teacher_id
            LEFT JOIN COURSE c ON at.course_id = c.course_id
            WHERE u.requested_by = %s
              AND u.date BETWEEN %s AND %s
            ORDER BY u.date ASC, u.start_time ASC
        """, [teacher_id, start_of_week, end_of_week])

        seen_req = set()
        for (d, st, et, lab_num, section, remarks, status, course_code) in cursor.fetchall():
            key = (d, st, et, lab_num, section, status, course_code)
            if key in seen_req:
                continue
            seen_req.add(key)

            time_key_24 = f"{st.strftime('%H:%M')} - {et.strftime('%H:%M')}"
            entry = {
                "lab_num": lab_num,
                "course": course_code,
                "date": d,
                "start_time": st,
                "end_time": et,
                "time": time_key_24,
                "section": section,
                "remarks": remarks,
                "status": status,
                "status_class": (status or "").strip().lower(),
                "requested_by": teacher_fullname,
            }
            own_requests.append(entry)
            own_requests_map[d.strftime("%A")][time_key_24].append(entry)
            request_time_set.add(time_key_24)

        time_slots = sorted(time_set)
        own_request_time_slots = sorted(request_time_set)

    # Overlap guard: ignore REJECTED so it won't block clicks
    own_request_ranges_json = json.dumps([
        {
            "date": e["date"].strftime("%Y-%m-%d"),
            "start": e["start_time"].strftime("%H:%M:%S"),
            "end": e["end_time"].strftime("%H:%M:%S"),
        }
        for e in own_requests
        if (e.get("status") or "").strip().lower() not in ("rejected",)
    ])

    context.update({
        "assigned_lab": assigned_lab,
        "labs": labs,
        "teacher_courses": teacher_courses,
        "selected_lab_id": int(selected_lab_id) if selected_lab_id else None,
        "selected_lab_num": selected_lab_num,
        "schedule_map": dict(schedule_map),
        "time_slots": time_slots,
        "own_requests_map": dict(own_requests_map),
        "own_request_time_slots": own_request_time_slots,
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "current_page": "Availability",
        "can_request_lab": True,
        "week_range_display": week_range_display,
        "prev_week": prev_week,
        "next_week": next_week,
        "base_week": start_of_week.isoformat(),

        # Pending context
        "pending_requests": pending_requests,
        "show_requests": show_requests,
        "pending_count": pending_count,
        "pending_lab_id": pending_lab_id,
        "pending_lab_num": pending_lab_num,
        "user_is_incharge_of_pending_lab": user_is_incharge_of_pending_lab,

        "own_requests": own_requests,
        "user_fullname": teacher_fullname,
        "user_shortname": user_shortname,
        "today": today,
        "own_request_ranges_json": own_request_ranges_json,
    })
    return render(request, "teacher/view_lab_availability.html", context)

# ---------- AJAX: weekly preview (uses AM/PM) ----------
def lab_week_data(request):
    if request.method != "GET":
        return JsonResponse({"error": "GET only"}, status=405)

    lab_id = request.GET.get("lab_id")
    week_str = request.GET.get("week")
    include_requests = (request.GET.get("include_requests") == "1")
    mine_param = request.GET.get("mine")  # "1" when called from My Schedule

    if not lab_id:
        return JsonResponse({"error": "lab_id required"}, status=400)

    try:
        base_date = dt.strptime(week_str, "%Y-%m-%d").date() if week_str else date.today()
    except ValueError:
        base_date = date.today()

    start_of_week = base_date - timedelta(days=base_date.weekday())  # Monday
    end_of_week   = start_of_week + timedelta(days=6)                # Sunday
    week_range_display = f"{start_of_week.strftime('%b %d')} - {end_of_week.strftime('%d, %Y')}"

    role = request.session.get("role")
    teacher_id = request.session.get("user_id") if role == "teacher" else None
    mine = (mine_param == "1" and role == "teacher")

    entries = defaultdict(lambda: defaultdict(list))
    req_entries = defaultdict(lambda: defaultdict(list))
    time_set = set()

    # ---------- SCHEDULES ----------
    with connection.cursor() as cur:
        if mine and teacher_id:
            # ✅ My Schedule → only this teacher’s rows
            cur.execute("""
                SELECT s.schedule_id, s.date, s.start_time, s.end_time,
                       COALESCE(f.first_name,''), COALESCE(f.last_name,''),
                       c.course_code,
                       COALESCE(s.student_year_and_section,''),
                       c.course_id,
                       COALESCE(s.status,'')
                FROM LAB_SCHEDULE s
                JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
                JOIN COURSE c ON at.course_id = c.course_id
                JOIN FACULTY f ON at.faculty_id = f.faculty_id AND ISNULL(f.is_archived,0) = 0
                WHERE s.lab_id = %s
                  AND at.faculty_id = %s
                  AND s.date BETWEEN %s AND %s
                  AND UPPER(LTRIM(RTRIM(ISNULL(s.status,'')))) NOT IN ('CANCELLED','REJECTED')
                ORDER BY s.date, s.start_time
            """, [lab_id, teacher_id, start_of_week, end_of_week])
        else:
            # Lab Availability / admin → whole lab schedule
            cur.execute("""
                SELECT s.schedule_id, s.date, s.start_time, s.end_time,
                       COALESCE(f.first_name,''), COALESCE(f.last_name,''),
                       c.course_code,
                       COALESCE(s.student_year_and_section,''),
                       c.course_id,
                       COALESCE(s.status,'')
                FROM LAB_SCHEDULE s
                JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
                JOIN COURSE c ON at.course_id = c.course_id
                JOIN FACULTY f ON at.faculty_id = f.faculty_id AND ISNULL(f.is_archived,0) = 0
                WHERE s.lab_id = %s
                  AND s.date BETWEEN %s AND %s
                  AND UPPER(LTRIM(RTRIM(ISNULL(s.status,'')))) NOT IN ('CANCELLED','REJECTED')
                ORDER BY s.date, s.start_time
            """, [lab_id, start_of_week, end_of_week])

        rows = cur.fetchall()

    for schedule_id, d, st, et, fn, ln, course_code, section, course_id, status in rows:
        dayname = d.strftime("%A")
        time_key_24    = f"{st.strftime('%H:%M')} - {et.strftime('%H:%M')}"
        time_display12 = f"{st.strftime('%I:%M %p')} - {et.strftime('%I:%M %p')}"

        teacher_full   = f"{(fn or '').title()} {(ln or '').title()}".strip()
        tshort = (f"{(fn or '')[:1].upper()}. {(ln or '').title()}".strip()) if (fn or ln) else ""

        try:
            sid = int(schedule_id)
        except (TypeError, ValueError):
            sid = None

        try:
            cid = int(course_id) if course_id is not None else None
        except (TypeError, ValueError):
            cid = None

        entries[dayname][time_key_24].append({
            "id": sid,
            "schedule_id": sid,
            "day": dayname,
            "time_24": time_key_24,
            "teacher": teacher_full,
            "teacher_short": tshort,
            "course": course_code or "",
            "course_id": cid,
            "section": section or "",
            "time": time_key_24,
            "time_display": time_display12,
            "status": (status or "").strip(),
        })
        time_set.add(time_key_24)

    # ---------- PENDING REQUESTS (only when include_requests=1) ----------
    if include_requests:
        with connection.cursor() as cur:
            cur.execute("""
                SELECT
                    u.date, u.start_time, u.end_time,
                    LTRIM(RTRIM(u.status)) AS status,
                    COALESCE(fr.first_name,''), COALESCE(fr.last_name,''),
                    u.student_year_and_section,
                    c.course_code
                FROM UTILIZATION_SLIP u
                LEFT JOIN FACULTY fr ON u.requested_by = fr.faculty_id
                LEFT JOIN ASSIGNED_TEACHER at ON u.assigned_teacher_id = at.assigned_teacher_id
                LEFT JOIN COURSE c ON at.course_id = c.course_id
                WHERE u.lab_id = %s
                  AND u.date BETWEEN %s AND %s
                  AND UPPER(LTRIM(RTRIM(u.status))) IN ('PENDING','FOR APPROVAL')
                ORDER BY u.date, u.start_time
            """, [lab_id, start_of_week, end_of_week])

            for d, st, et, status, rfn, rln, section, course_code in cur.fetchall():
                dayname = d.strftime("%A")
                if st and et:
                    key_24 = f"{st.strftime('%H:%M')} - {et.strftime('%H:%M')}"
                    key_12 = f"{st.strftime('%I:%M %p')} - {et.strftime('%I:%M %p')}"
                    time_set.add(key_24)
                else:
                    key_24 = "—"
                    key_12 = "—"

                req_entries[dayname][key_24].append({
                    "requested_by": f"{(rfn or '').title()} {(rln or '').title()}".strip(),
                    "requested_by_short": ((rfn or '')[:1].upper() + (". " + (rln or '').title() if rln else "")).strip(),
                    "status": status or "Pending",
                    "section": section or "",
                    "course": course_code or "",
                    "time": key_24,
                    "time_display": key_12
                })

    time_slots = sorted(time_set)

    return JsonResponse({
        "week_range": week_range_display,
        "entries": {d: dict(times) for d, times in entries.items()},
        "req_entries": {d: dict(times) for d, times in req_entries.items()} if include_requests else {},
        "time_slots": time_slots
    })

# ---------- POLLING: active sessions for highlighting ----------
# ---------- POLLING: active sessions for highlighting ----------
def get_active_sessions(request):
    lab_id = request.GET.get("lab_id")
    now = timezone.localtime()
    today = now.date()
    now_t = now.time()

    if not lab_id:
        return JsonResponse({"active_sessions": []})

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT u.date, u.start_time, u.end_time, u.requested_by, u.schedule_id
            FROM UTILIZATION_SLIP u
            WHERE u.lab_id = %s
              AND u.status = 'Active'
        """, [lab_id])
        rows = cursor.fetchall()

    active = []
    for d, st, et, faculty_id, schedule_id in rows:
        active.append({
            "date": d.isoformat(),
            "start_time": st.strftime("%H:%M:%S") if st else None,
            "end_time": et.strftime("%H:%M:%S") if et else None,
            "faculty_id": faculty_id,
            "schedule_id": schedule_id,
        })

    return JsonResponse({"active_sessions": active})




# ---------- SUBMIT UTILIZATION ----------
@csrf_exempt 
def submit_utilization_request(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid request method.")
    
    teacher_id = request.session.get("user_id")
    if not teacher_id or request.session.get("role") != "teacher":
        return redirect("login")
    
    lab_id = request.POST.get("lab_id")
    course_id = request.POST.get("course_id")
    year_section = request.POST.get("student_year_and_section")
    remarks = request.POST.get("remarks")
    date_str = request.POST.get("date")
    start_time_str = request.POST.get("start_time")
    end_time_str = request.POST.get("end_time")
    
    if not all([lab_id, course_id, year_section, date_str, start_time_str, end_time_str]):
        return _back_with_error(request, "Please fill out all required fields.", lab_id=lab_id)
    
    # Parse datetime
    try:
        date_value = dt.strptime(date_str, "%Y-%m-%d").date()
        start_dt = dt.strptime(start_time_str, "%H:%M")
        end_dt = dt.strptime(end_time_str, "%H:%M")
        duration = (end_dt - start_dt)
        if duration.total_seconds() <= 0:
            return _back_with_error(request, "End time must be after Start time.", lab_id=lab_id)
        
        # Enforcing duration check (3 to 5 hours)
        duration_hours = duration.total_seconds() / 3600
        if duration_hours < 3:
            return _back_with_error(request, "The minimum duration is 3 hours.", lab_id=lab_id)
        elif duration_hours > 5:
            return _back_with_error(request, "The maximum duration is 5 hours.", lab_id=lab_id)
        
        time_duration = (dt.min + duration).time()
    except Exception:
        return _back_with_error(request, "Invalid date or time format.", lab_id=lab_id)
    
    pretty_range = f"{_fmt_am_pm(start_dt.time())}–{_fmt_am_pm(end_dt.time())}"
    
    # Past date guards
    now_local = timezone.localtime()
    if date_value < now_local.date():
        return _back_with_error(request, "You cannot request a past date.", lab_id=lab_id)
    if date_value == now_local.date() and start_dt.time() <= now_local.time():
        return _back_with_error(request, "You cannot request a time that has already started or passed.", lab_id=lab_id)
    
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Idempotent same slot check
            cursor.execute("""
                SELECT TOP 1 status FROM UTILIZATION_SLIP 
                WHERE requested_by = %s AND lab_id = %s AND date = %s 
                  AND start_time = %s AND end_time = %s 
                  AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL','APPROVED','ACTIVE')
                ORDER BY utilization_id DESC
            """, [teacher_id, lab_id, date_value, start_dt.time(), end_dt.time()])
            if cursor.fetchone():
                return redirect(
                    f"{reverse('view_lab_availability')}?lab_id={lab_id}"
                    f"&success=Your request for {date_value.isoformat()} {pretty_range} already exists"
                )
            
            # Course assignment (semester check)
            cursor.execute("""
                SELECT TOP 1 at.assigned_teacher_id 
                FROM ASSIGNED_TEACHER at 
                JOIN SEMESTER s ON s.semester_id = at.semester_id 
                WHERE at.faculty_id = %s AND at.course_id = %s 
                  AND %s BETWEEN s.start_date AND s.end_date 
                  AND ISNULL(at.is_active,1) = 1 
                ORDER BY s.start_date DESC
            """, [teacher_id, course_id, date_value])
            row = cursor.fetchone()
            if not row:
                return _back_with_error(
                    request,
                    "You are not assigned to this course for the selected date/semester.",
                    lab_id=lab_id
                )
            assigned_teacher_id = row[0]
            
            # Operating Time (FK)
            db_dow = _db_day_of_week(date_value.weekday())
            cursor.execute("""
                SELECT TOP 1 operating_time_id, is_open, start_time, end_time 
                FROM OPERATING_TIME 
                WHERE day_of_week = %s
            """, [db_dow])
            ot = cursor.fetchone()
            if not ot:
                return _back_with_error(
                    request,
                    "Operating hours not configured for the selected day.",
                    lab_id=lab_id
                )
            op_id, is_open, ot_start, ot_end = ot[0], ot[1], ot[2], ot[3]
            if not is_open:
                return _back_with_error(request, "The lab is closed on the selected day.", lab_id=lab_id)
            if not (ot_start and ot_end):
                return _back_with_error(
                    request,
                    "Operating hours not configured for the selected day.",
                    lab_id=lab_id
                )
            if not (ot_start <= start_dt.time() and end_dt.time() <= ot_end):
                return _back_with_error(
                    request,
                    f"Time must be within operating hours ({_fmt_am_pm(ot_start)}–{_fmt_am_pm(ot_end)}).",
                    lab_id=lab_id
                )
            
            # ========== CONFLICT DETECTION ==========

            # 1. Overlapping APPROVED/ACTIVE utilization requests (BLOCKS)
            cursor.execute("""
                SELECT COUNT(*) 
                FROM UTILIZATION_SLIP 
                WHERE lab_id = %s 
                  AND date = %s 
                  AND NOT (end_time <= %s OR start_time >= %s)
                  AND UPPER(LTRIM(RTRIM(status))) IN ('APPROVED','ACTIVE')
            """, [lab_id, date_value, start_dt.time(), end_dt.time()])
            (approved_conflict_count,) = cursor.fetchone() or (0,)
            if approved_conflict_count > 0:
                return _back_with_error(
                    request,
                    "This slot is already taken by an approved utilization request.",
                    lab_id=lab_id
                )
            
            # 2. Overlapping PENDING/FOR APPROVAL utilization requests (BLOCKS)
            cursor.execute("""
                SELECT TOP 1 utilization_id 
                FROM UTILIZATION_SLIP 
                WHERE lab_id = %s 
                  AND date = %s 
                  AND NOT (end_time <= %s OR start_time >= %s)
                  AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
            """, [lab_id, date_value, start_dt.time(), end_dt.time()])
            overlapping_req = cursor.fetchone()
            if overlapping_req:
                return _back_with_error(
                    request,
                    "This slot already has a pending request. Please choose a different time.",
                    lab_id=lab_id
                )
            
            # 3. LAB_SCHEDULE conflicts
            #    ✅ IGNORE cancelled / rejected schedules – they should NOT block
            cursor.execute("""
                SELECT COUNT(*) 
                FROM LAB_SCHEDULE 
                WHERE lab_id = %s 
                  AND date   = %s 
                  AND UPPER(LTRIM(RTRIM(ISNULL(status, '')))) NOT IN ('CANCELLED','REJECTED')
                  AND NOT (end_time <= %s OR start_time >= %s)
            """, [lab_id, date_value, start_dt.time(), end_dt.time()])
            (class_conflict_count,) = cursor.fetchone() or (0,)
            # ========== END CONFLICT DETECTION ==========
            
            # ---- helpers for notifications/emails ----
            def _get_admins():
                cursor.execute("""
                    SELECT faculty_id, email 
                    FROM FACULTY 
                    WHERE role = 'admin' AND is_archived = 0
                """)
                rows = cursor.fetchall()
                return [r[0] for r in rows], [r[1] for r in rows if r[1]]
            
            def _get_in_charge(lab_id_):
                cursor.execute(
                    "SELECT lab_num, faculty_id FROM LABORATORIES WHERE lab_id = %s",
                    [lab_id_]
                )
                r = cursor.fetchone()
                return (r[0], r[1]) if r else (None, None)
            
            def _get_email(fid):
                cursor.execute(
                    "SELECT email FROM FACULTY WHERE faculty_id = %s AND is_archived = 0",
                    [fid]
                )
                r = cursor.fetchone()
                return (r[0] if r else None)
            
            # ========== DECISION: Auto-approve or Pending ==========
            # Auto-approve ONLY if no class conflicts AND no utilization conflicts
            if class_conflict_count == 0 and approved_conflict_count == 0:
                # No conflicts, so schedule the lab automatically
                cursor.execute("""
                    INSERT INTO LAB_SCHEDULE (
                        lab_id, assigned_teacher_id, reserved_to, date, start_time, end_time, 
                        student_year_and_section, status, operating_time_id
                    ) OUTPUT INSERTED.schedule_id 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'Scheduled', %s)
                """, [
                    lab_id, assigned_teacher_id, teacher_id, date_value, 
                    start_dt.time(), end_dt.time(), year_section, op_id
                ])
                schedule_id = cursor.fetchone()[0]
                
                # Convert pending if present, else insert Approved (with op FK)
                cursor.execute("""
                    SELECT TOP 1 utilization_id 
                    FROM UTILIZATION_SLIP 
                    WHERE requested_by = %s 
                      AND lab_id       = %s 
                      AND date         = %s 
                      AND start_time   = %s 
                      AND end_time     = %s 
                      AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
                    ORDER BY created_at DESC
                """, [teacher_id, lab_id, date_value, start_dt.time(), end_dt.time()])
                existing_pending = cursor.fetchone()
                
                if existing_pending:
                    cursor.execute("""
                        UPDATE UTILIZATION_SLIP 
                        SET schedule_id = %s,
                            time_duration = %s, 
                            remarks = CASE WHEN COALESCE(remarks,'')='' THEN %s ELSE remarks END,
                            processed_by = %s, 
                            status = 'Approved', 
                            assigned_teacher_id = COALESCE(assigned_teacher_id, %s), 
                            operating_time_id = %s 
                        WHERE utilization_id = %s
                    """, [
                        schedule_id, time_duration, remarks or '', teacher_id, 
                        assigned_teacher_id, op_id, existing_pending[0]
                    ])
                else:
                    cursor.execute("""
                        INSERT INTO UTILIZATION_SLIP (
                            schedule_id, date, time_duration, start_time, end_time, 
                            requested_by, student_year_and_section, remarks, processed_by, 
                            status, created_at, lab_id, assigned_teacher_id, operating_time_id
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            'Approved', GETDATE(), %s, %s, %s
                        )
                    """, [
                        schedule_id, date_value, time_duration, start_dt.time(), end_dt.time(),
                        teacher_id, year_section, remarks, teacher_id,
                        lab_id, assigned_teacher_id, op_id
                    ])
                
                # ---------- Notifications & Emails (Auto-scheduled) ----------
                lab_num, lab_in_charge = _get_in_charge(lab_id)
                if lab_num is not None:
                    lab_label = f"Lab {lab_num}"
                    notif_message = (
                        f"A slot was auto-scheduled for {lab_label} "
                        f"on {date_value.isoformat()} at {pretty_range}."
                    )
                    admin_ids, admin_emails = _get_admins()
                    
                    # In-app for admins
                    for admin_id in admin_ids:
                        cursor.execute("""
                            INSERT INTO NOTIFICATIONS (
                                message, status, receiver_teacher_id, sender_teacher_id, created_at
                            )
                            VALUES (%s, 'Unread', %s, %s, GETDATE())
                        """, [notif_message, admin_id, teacher_id])
                        _push_unread_to_channel(admin_id)
                    
                    # Email admins
                    if admin_emails:
                        try:
                            send_mail(
                                "Lab Auto-Scheduled",
                                notif_message,
                                settings.DEFAULT_FROM_EMAIL,
                                admin_emails,
                                fail_silently=True
                            )
                        except Exception:
                            pass
                    
                    # In-app + email for lab-in-charge (if not requester/admin)
                    if lab_in_charge and lab_in_charge != teacher_id and lab_in_charge not in admin_ids:
                        cursor.execute("""
                            INSERT INTO NOTIFICATIONS (
                                message, status, receiver_teacher_id, sender_teacher_id, created_at
                            )
                            VALUES (%s, 'Unread', %s, %s, GETDATE())
                        """, [notif_message, lab_in_charge, teacher_id])
                        _push_unread_to_channel(lab_in_charge)

                        incharge_email = _get_email(lab_in_charge)
                        if incharge_email:
                            try:
                                send_mail(
                                    "Lab Auto-Scheduled",
                                    notif_message,
                                    settings.DEFAULT_FROM_EMAIL,
                                    [incharge_email],
                                    fail_silently=True
                                )
                            except Exception:
                                pass
                    
                    # Email the requester too
                    req_email = _get_email(teacher_id)
                    if req_email:
                        try:
                            send_mail(
                                "Lab Auto-Scheduled",
                                notif_message,
                                settings.DEFAULT_FROM_EMAIL,
                                [req_email],
                                fail_silently=True
                            )
                        except Exception:
                            pass
                
                return redirect(
                    f"{reverse('view_lab_availability')}?lab_id={lab_id}"
                    f"&success=Scheduled for {date_value.isoformat()} {pretty_range}"
                )
            
            # ========== PENDING REQUEST PATH (class_conflict_count > 0) ==========
            cursor.execute("""
                SELECT COUNT(*) 
                FROM UTILIZATION_SLIP 
                WHERE requested_by = %s 
                  AND lab_id       = %s 
                  AND date         = %s 
                  AND start_time   = %s 
                  AND end_time     = %s 
                  AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
            """, [teacher_id, lab_id, date_value, start_dt.time(), end_dt.time()])
            (already_pending,) = cursor.fetchone() or (0,)
            
            if already_pending == 0:
                cursor.execute("""
                    INSERT INTO UTILIZATION_SLIP (
                        schedule_id, date, time_duration, start_time, end_time, 
                        requested_by, student_year_and_section, remarks, processed_by, 
                        status, created_at, lab_id, assigned_teacher_id, operating_time_id
                    ) VALUES (
                        NULL, %s, %s, %s, %s,
                        %s, %s, %s, NULL,
                        'Pending', GETDATE(), %s, %s, %s
                    )
                """, [
                    date_value, time_duration, start_dt.time(), end_dt.time(),
                    teacher_id, year_section, remarks,
                    lab_id, assigned_teacher_id, op_id
                ])
            
            # ---------- Notifications & Emails (New Pending) ----------
            lab_num, lab_in_charge = _get_in_charge(lab_id)
            if lab_num is not None:
                lab_label = f"Lab {lab_num}"
                notif_message = (
                    f"New lab request for {lab_label} "
                    f"on {date_value.isoformat()} at {pretty_range}."
                )
                admin_ids, admin_emails = _get_admins()
                
                # In-app for admins
                for admin_id in admin_ids:
                    cursor.execute("""
                        INSERT INTO NOTIFICATIONS (
                            message, status, receiver_teacher_id, sender_teacher_id, created_at
                        )
                        VALUES (%s, 'Unread', %s, %s, GETDATE())
                    """, [notif_message, admin_id, teacher_id])
                    _push_unread_to_channel(admin_id)
                
                # Email admins
                if admin_emails:
                    try:
                        send_mail(
                            "New Lab Reservation Request",
                            notif_message,
                            settings.DEFAULT_FROM_EMAIL,
                            admin_emails,
                            fail_silently=True
                        )
                    except Exception:
                        pass
                
                # In-app + email for lab-in-charge (if not requester/admin)
                if lab_in_charge and lab_in_charge != teacher_id and lab_in_charge not in admin_ids:
                    cursor.execute("""
                        INSERT INTO NOTIFICATIONS (
                            message, status, receiver_teacher_id, sender_teacher_id, created_at
                        )
                        VALUES (%s, 'Unread', %s, %s, GETDATE())
                    """, [notif_message, lab_in_charge, teacher_id])
                    _push_unread_to_channel(lab_in_charge)

                    incharge_email = _get_email(lab_in_charge)
                    if incharge_email:
                        try:
                            send_mail(
                                "New Lab Reservation Request",
                                notif_message,
                                settings.DEFAULT_FROM_EMAIL,
                                [incharge_email],
                                fail_silently=True
                            )
                        except Exception:
                            pass
                
                # Email the requester too
                req_email = _get_email(teacher_id)
                if req_email:
                    try:
                        send_mail(
                            "New Lab Reservation Request",
                            notif_message,
                            settings.DEFAULT_FROM_EMAIL,
                            [req_email],
                            fail_silently=True
                        )
                    except Exception:
                        pass
            
            if already_pending == 0:
                return redirect(
                    f"{reverse('view_lab_availability')}?lab_id={lab_id}"
                    f"&success=Your request for {date_value.isoformat()} {pretty_range} was submitted as Pending"
                )
            else:
                return redirect(
                    f"{reverse('view_lab_availability')}?lab_id={lab_id}"
                    f"&success=Your request for {date_value.isoformat()} {pretty_range} is already Pending"
                )


# ---------------------------
# Approve / reject flows
# ---------------------------

def _get_assigned_teacher_id_for_date(faculty_id, on_date):
    """
    Find an ASSIGNED_TEACHER.assigned_teacher_id for the given faculty and date
    within an active semester window. Prefers most recent start_date.
    """
    with connection.cursor() as cur:
        cur.execute("""
            SELECT TOP 1 at.assigned_teacher_id
            FROM ASSIGNED_TEACHER at
            JOIN SEMESTER s ON s.semester_id = at.semester_id
            JOIN FACULTY f  ON f.faculty_id = at.faculty_id AND ISNULL(f.is_archived,0) = 0
            WHERE at.faculty_id = %s
              AND %s BETWEEN s.start_date AND s.end_date
            ORDER BY s.start_date DESC
        """, [faculty_id, on_date])
        row = cur.fetchone()
        return row[0] if row else None


def _notify_faculty(receiver_faculty_id, sender_faculty_id, message, subject):
    with connection.cursor() as cur:
        cur.execute("""
            INSERT INTO NOTIFICATIONS (message, status, receiver_teacher_id, sender_teacher_id, created_at)
            VALUES (%s, 'Unread', %s, %s, GETDATE())
        """, [message, receiver_faculty_id, sender_faculty_id])

        # 🔔 Fire Pusher so their header updates
        _push_unread_to_channel(receiver_faculty_id)

        cur.execute("SELECT email FROM FACULTY WHERE faculty_id = %s AND is_archived = 0", [receiver_faculty_id])
        email_row = cur.fetchone()
        if email_row and email_row[0]:
            try:
                send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email_row[0]], fail_silently=True)
            except Exception:
                pass


def _redirect_pending(lab_id, kind="success", msg=""):
    key = "success" if kind == "success" else ("error" if kind == "error" else "info")
    return redirect(f"{reverse('view_lab_availability')}?lab_id={lab_id}&open=pending&{key}={msg}")


@require_POST
@transaction.atomic
def approve_reservation(request, res_id):
    teacher_id = request.session.get("user_id")
    if not teacher_id or request.session.get("role") != "teacher":
        return redirect("login")

    with connection.cursor() as cursor:
        # Load slip
        cursor.execute("""
            SELECT 
                u.utilization_id, u.lab_id, u.date, u.start_time, u.end_time,
                u.student_year_and_section, u.requested_by,
                u.status, u.assigned_teacher_id, CAST(COALESCE(u.remarks,'') AS NVARCHAR(4000))
            FROM UTILIZATION_SLIP u
            WHERE u.utilization_id = %s
        """, [res_id])
        r = cursor.fetchone()
        if not r:
            return _redirect_pending(request.GET.get("lab_id", ""), "error", "Request not found.")

        (util_id, lab_id, date_val, start_time_obj, end_time_obj,
         year_section, requested_by, cur_status, req_assigned_teacher_id, _remarks) = r

        # 0) time sanity checks
        if not (start_time_obj and end_time_obj):
            return _redirect_pending(lab_id, "error", "Cannot approve: request is missing start or end time.")
        if end_time_obj <= start_time_obj:
            return _redirect_pending(lab_id, "error", "Cannot approve: end time must be after start time.")
        now_local = timezone.localtime()
        if date_val < now_local.date() or (date_val == now_local.date() and start_time_obj <= now_local.time()):
            pretty_range = f"{_fmt_hhmm(start_time_obj)}–{_fmt_hhmm(end_time_obj)}"
            return _redirect_pending(lab_id, "error", f"Cannot approve: {date_val:%Y-%m-%d} {pretty_range} has already started or passed.")

        norm = (cur_status or "").strip().upper()
        if norm in ("APPROVED", "ACTIVE"):
            return _redirect_pending(lab_id, "info", "This request is already Approved.")
        if norm == "REJECTED":
            return _redirect_pending(lab_id, "info", "This request was already Rejected.")
        if norm not in ("PENDING", "FOR APPROVAL"):
            return _redirect_pending(lab_id, "error", f"Cannot approve: invalid status '{cur_status}'.")

        # Who manages this lab?
        cursor.execute("SELECT lab_id FROM LABORATORIES WHERE faculty_id = %s", [teacher_id])
        lab_row = cursor.fetchone()
        managed_lab = lab_row[0] if lab_row else None
        is_lab_in_charge = (managed_lab == lab_id)

        # Ensure requester has an assignment for that date
        if not req_assigned_teacher_id:
            req_assigned_teacher_id = _get_assigned_teacher_id_for_date(requested_by, date_val)
        if not req_assigned_teacher_id:
            return _redirect_pending(lab_id, "error", "Requester has no active course assignment for that date/semester.")

        # Labels
        cursor.execute("SELECT lab_num FROM LABORATORIES WHERE lab_id = %s", [lab_id])
        lab_num_row = cursor.fetchone()
        lab_label = f"Lab {lab_num_row[0]}" if lab_num_row else f"Lab #{lab_id}"
        st_txt = _fmt_hhmm(start_time_obj)
        et_txt = _fmt_hhmm(end_time_obj)

        # 1) EXACT MATCH schedule?
        cursor.execute("""
            SELECT s.schedule_id, at.faculty_id AS owner_faculty_id
            FROM LAB_SCHEDULE s
            LEFT JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
            WHERE s.lab_id = %s AND s.date = %s AND s.start_time = %s AND s.end_time = %s
        """, [lab_id, date_val, start_time_obj, end_time_obj])
        exact = cursor.fetchone()

        if exact:
            schedule_id, owner_faculty_id = exact

            # If requester already owns that exact slot -> link & approve
            if owner_faculty_id == requested_by:
                if not (is_lab_in_charge or teacher_id == requested_by):
                    return _redirect_pending(lab_id, "error", "Unauthorized: only the lab-in-charge or the requester can approve this.")

                cursor.execute("""
                    UPDATE UTILIZATION_SLIP
                       SET status='Approved',
                           processed_by=%s,
                           schedule_id=%s,
                           assigned_teacher_id = COALESCE(assigned_teacher_id, %s)
                     WHERE utilization_id=%s
                       AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
                """, [teacher_id, schedule_id, req_assigned_teacher_id, util_id])

                if cursor.rowcount == 0:
                    return _redirect_pending(lab_id, "info", "Status changed by someone else. Refreshed list shown.")

                _notify_faculty(
                    requested_by, teacher_id,
                    f"Your lab request for {lab_label} on {date_val:%Y-%m-%d} ({st_txt}–{et_txt}) has been approved.",
                    "Lab Request Approved"
                )
                return _redirect_pending(lab_id, "success", "Request approved.")

            # If approver owns the exact slot OR is in-charge -> reassign to requester
            if owner_faculty_id == teacher_id or is_lab_in_charge:
                cursor.execute("""
                    UPDATE LAB_SCHEDULE
                       SET assigned_teacher_id = %s,
                           reserved_to = %s,
                           student_year_and_section = %s
                     WHERE schedule_id = %s
                """, [req_assigned_teacher_id, requested_by, year_section, schedule_id])

                cursor.execute("""
                    UPDATE UTILIZATION_SLIP
                       SET status='Approved',
                           processed_by=%s,
                           schedule_id=%s,
                           assigned_teacher_id = COALESCE(assigned_teacher_id, %s)
                     WHERE utilization_id=%s
                       AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
                """, [teacher_id, schedule_id, req_assigned_teacher_id, util_id])

                if cursor.rowcount == 0:
                    return _redirect_pending(lab_id, "info", "Status changed by someone else. Refreshed list shown.")

                if owner_faculty_id and owner_faculty_id not in (requested_by, teacher_id):
                    _notify_faculty(
                        owner_faculty_id, teacher_id,
                        f"Your {lab_label} schedule on {date_val:%Y-%m-%d} ({st_txt}–{et_txt}) was reassigned due to an approved request.",
                        "Lab Schedule Reassigned"
                    )
                    cursor.execute("""
                        INSERT INTO ARCHIVE_LOG (user_type, user_id, action, performed_by)
                        VALUES ('faculty', %s, 'reassign', %s)
                    """, [owner_faculty_id, teacher_id])

                _notify_faculty(
                    requested_by, teacher_id,
                    f"Your lab request for {lab_label} on {date_val:%Y-%m-%d} ({st_txt}–{et_txt}) has been approved.",
                    "Lab Request Approved"
                )
                return _redirect_pending(lab_id, "success", "Request approved.")

            return _redirect_pending(lab_id, "error", "Unauthorized: only the lab-in-charge can reassign this slot.")

        # 2) FLEX OVERLAP: find an overlapping schedule (not exact)
        cursor.execute("""
            SELECT TOP 1 s.schedule_id,
                         s.start_time, s.end_time,
                         at.faculty_id AS owner_faculty_id
            FROM LAB_SCHEDULE s
            LEFT JOIN ASSIGNED_TEACHER at ON s.assigned_teacher_id = at.assigned_teacher_id
            WHERE s.lab_id = %s
              AND s.date = %s
              AND NOT (s.end_time <= %s OR s.start_time >= %s)
            ORDER BY
              CASE WHEN s.start_time = %s AND s.end_time = %s THEN 0 ELSE 1 END,
              ABS(DATEDIFF(SECOND, s.start_time, %s)) + ABS(DATEDIFF(SECOND, s.end_time, %s))
        """, [lab_id, date_val, start_time_obj, end_time_obj,
              start_time_obj, end_time_obj, start_time_obj, end_time_obj])
        overlap = cursor.fetchone()

        if overlap:
            ov_schedule_id, ov_st, ov_et, ov_owner = overlap

            # If the approver owns the overlapping slot or is in-charge → reassign & retime to request window
            if ov_owner == teacher_id or is_lab_in_charge:
                cursor.execute("""
                    UPDATE LAB_SCHEDULE
                       SET start_time = %s,
                           end_time   = %s,
                           assigned_teacher_id = %s,
                           reserved_to = %s,
                           student_year_and_section = %s
                     WHERE schedule_id = %s
                """, [start_time_obj, end_time_obj, req_assigned_teacher_id, requested_by, year_section, ov_schedule_id])

                cursor.execute("""
                    UPDATE UTILIZATION_SLIP
                       SET status='Approved',
                           processed_by=%s,
                           schedule_id=%s,
                           assigned_teacher_id = COALESCE(assigned_teacher_id, %s)
                     WHERE utilization_id=%s
                       AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
                """, [teacher_id, ov_schedule_id, req_assigned_teacher_id, util_id])

                if cursor.rowcount == 0:
                    return _redirect_pending(lab_id, "info", "Status changed by someone else. Refreshed list shown.")

                if ov_owner and ov_owner not in (requested_by, teacher_id):
                    _notify_faculty(
                        ov_owner, teacher_id,
                        f"Your {lab_label} schedule on {date_val:%Y-%m-%d} was reassigned (time changed to {st_txt}–{et_txt}) due to an approved request.",
                        "Lab Schedule Reassigned"
                    )
                    cursor.execute("""
                        INSERT INTO ARCHIVE_LOG (user_type, user_id, action, performed_by)
                        VALUES ('faculty', %s, 'reassign', %s)
                    """, [ov_owner, teacher_id])

                _notify_faculty(
                    requested_by, teacher_id,
                    f"Your lab request for {lab_label} on {date_val:%Y-%m-%d} ({st_txt}–{et_txt}) has been approved.",
                    "Lab Request Approved"
                )
                return _redirect_pending(lab_id, "success", "Request approved.")

            return _redirect_pending(lab_id, "error", "Conflict: another schedule overlaps this time in the same lab.")

        # 3) No exact and no overlap → only lab-in-charge may create
        if not is_lab_in_charge:
            return _redirect_pending(lab_id, "error", "Unauthorized: only the lab-in-charge can create a schedule when approving.")

        # Safety guard (should be clear by now)
        cursor.execute("""
            SELECT COUNT(*) FROM LAB_SCHEDULE
            WHERE lab_id = %s AND date = %s
              AND NOT (end_time <= %s OR start_time >= %s)
        """, [lab_id, date_val, start_time_obj, end_time_obj])
        (conflict_count,) = cursor.fetchone() or (0,)
        if conflict_count > 0:
            return _redirect_pending(lab_id, "error", "Conflict: another schedule overlaps this time in the same lab.")

        # Create schedule + approve slip
        cursor.execute("""
            INSERT INTO LAB_SCHEDULE (
                lab_id, assigned_teacher_id, reserved_to, date, start_time, end_time,
                student_year_and_section, status
            ) OUTPUT INSERTED.schedule_id
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'Scheduled')
        """, [lab_id, req_assigned_teacher_id, requested_by, date_val, start_time_obj, end_time_obj, year_section])
        schedule_id = cursor.fetchone()[0]

        cursor.execute("""
            UPDATE UTILIZATION_SLIP
               SET status='Approved',
                   processed_by=%s,
                   schedule_id=%s,
                   assigned_teacher_id = COALESCE(assigned_teacher_id, %s)
             WHERE utilization_id=%s
               AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
        """, [teacher_id, schedule_id, req_assigned_teacher_id, util_id])

        if cursor.rowcount == 0:
            return _redirect_pending(lab_id, "info", "Status changed by someone else. Refreshed list shown.")

        _notify_faculty(
            requested_by, teacher_id,
            f"Your lab request for {lab_label} on {date_val:%Y-%m-%d} ({st_txt}–{et_txt}) has been approved.",
            "Lab Request Approved"
        )
        return _redirect_pending(lab_id, "success", "Request approved.")


@require_POST
@transaction.atomic
def reject_reservation(request, res_id):
    teacher_id = request.session.get("user_id")
    if not teacher_id or request.session.get("role") != "teacher":
        return redirect("login")

    with connection.cursor() as cursor:
        # authority: lab-in-charge or owner (pending self)
        cursor.execute("SELECT lab_id FROM LABORATORIES WHERE faculty_id = %s", [teacher_id])
        lab_row = cursor.fetchone()
        managed_lab = lab_row[0] if lab_row else None

        cursor.execute("""
            SELECT lab_id, requested_by, date, start_time, end_time, status
            FROM UTILIZATION_SLIP
            WHERE utilization_id = %s
        """, [res_id])
        row = cursor.fetchone()
        if not row:
            return _redirect_pending(request.GET.get("lab_id", ""), "error", "Request not found.")

        lab_id, requested_by, date_val, start_time_obj, end_time_obj, cur_status = row
        norm = (cur_status or "").strip().upper()

        is_lab_in_charge = (managed_lab == lab_id)
        is_owner_pending = (requested_by == teacher_id and norm in ("PENDING", "FOR APPROVAL"))

        if not (is_lab_in_charge or is_owner_pending):
            return _redirect_pending(lab_id, "error", "Unauthorized: you cannot reject this request.")

        # Idempotent outcomes
        if norm == "REJECTED":
            return _redirect_pending(lab_id, "info", "This request is already Rejected.")
        if norm in ("APPROVED", "ACTIVE"):
            return _redirect_pending(lab_id, "info", "Cannot reject: request is already Approved/Active.")
        if norm not in ("PENDING", "FOR APPROVAL"):
            return _redirect_pending(lab_id, "error", f"Cannot reject: invalid status '{cur_status}'.")

        # Reject (concurrency-safe)
        cursor.execute("""
            UPDATE UTILIZATION_SLIP
               SET status='Rejected', processed_by=%s
             WHERE utilization_id=%s
               AND UPPER(LTRIM(RTRIM(status))) IN ('PENDING','FOR APPROVAL')
        """, [teacher_id, res_id])

        if cursor.rowcount == 0:
            return _redirect_pending(lab_id, "info", "Status changed by someone else. Refreshed list shown.")

        # Notify requester
        cursor.execute("SELECT lab_num FROM LABORATORIES WHERE lab_id = %s", [lab_id])
        lab_num_row = cursor.fetchone()
        lab_label = f"Lab {lab_num_row[0]}" if lab_num_row else f"Lab #{lab_id}"

        st_txt = _fmt_hhmm(start_time_obj)
        et_txt = _fmt_hhmm(end_time_obj)

        _notify_faculty(
            receiver_faculty_id=requested_by,
            sender_faculty_id=teacher_id,
            message=f"Your lab request for {lab_label} on {date_val:%Y-%m-%d} from {st_txt} to {et_txt} has been rejected.",
            subject="Lab Request Rejected"
        )

    return _redirect_pending(lab_id, "success", "Request rejected.")

# ---------------------------
# Notifications & Profile
# ---------------------------

@csrf_exempt
def mark_teacher_notifications_read(request):
    if request.method == "POST":
        teacher_id = request.session.get("user_id")
        if not teacher_id or request.session.get("role") != "teacher":
            return JsonResponse({"error": "Unauthorized"}, status=403)

        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE NOTIFICATIONS
                SET status = 'Read'
                WHERE receiver_teacher_id = %s AND status = 'Unread'
            """, [teacher_id])

        return JsonResponse({"message": "Notifications marked as read"})

    return JsonResponse({"error": "Invalid method"}, status=405)


@require_GET
def notifications_load_more(request):
    teacher_id = request.session.get("user_id")
    if not teacher_id or request.session.get("role") != "teacher":
        return JsonResponse({"error": "Unauthorized"}, status=403)

    last_id = request.GET.get("last_id")
    page_size = 10

    with connection.cursor() as cursor:
        if last_id and str(last_id).isdigit():
            cursor.execute("""
                SELECT TOP 10
                       notification_id AS id,
                       message,
                       status,
                       created_at
                FROM NOTIFICATIONS
                WHERE receiver_teacher_id = %s
                  AND notification_id < %s
                ORDER BY notification_id DESC
            """, [teacher_id, int(last_id)])
        else:
            cursor.execute("""
                SELECT TOP 10
                       notification_id AS id,
                       message,
                       status,
                       created_at
                FROM NOTIFICATIONS
                WHERE receiver_teacher_id = %s
                ORDER BY notification_id DESC
            """, [teacher_id])

        rows = cursor.fetchall()

    notifications = [{
        "id": r[0],
        "message": r[1] or "(No message)",
        "status": r[2],
        "created_at": r[3].strftime("%b %d, %Y %H:%M") if r[3] else "",
    } for r in rows]

    return JsonResponse({"notifications": notifications, "no_more": len(notifications) < page_size})


@require_GET
def notification_detail(request, notif_id: int):
    teacher_id = request.session.get("user_id")
    if not teacher_id or request.session.get("role") != "teacher":
        return JsonResponse({"error": "Unauthorized"}, status=403)

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT message
            FROM NOTIFICATIONS
            WHERE notification_id = %s
              AND receiver_teacher_id = %s
        """, [notif_id, teacher_id])
        row = cursor.fetchone()

    if not row:
        return JsonResponse({"error": "Not found"}, status=404)

    return JsonResponse({"full_message": row[0] or "(No message)"})


def teacher_profile(request):
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session["user_id"]
    context = get_teacher_header_context(teacher_id)

    # find the lab this teacher manages (if any)
    with connection.cursor() as cursor:
        cursor.execute("SELECT lab_id FROM LABORATORIES WHERE faculty_id = %s", [teacher_id])
        lab_row = cursor.fetchone()
        assigned_lab = lab_row[0] if lab_row else None

        cursor.execute("""
            SELECT first_name, middle_name, last_name, email, password, profile_image
            FROM FACULTY
            WHERE faculty_id = %s AND is_archived = 0
        """, [teacher_id])
        row = cursor.fetchone()

    if not row:
        return HttpResponse("Teacher not found or is archived.")

    if request.method == "POST":
        first_name = request.POST.get("first_name")
        middle_name = request.POST.get("middle_name")
        last_name = request.POST.get("last_name")
        email = request.POST.get("email")
        password_input = request.POST.get("password", "").strip()

        profile_image_path = row[5]  # existing image
        uploaded_file = request.FILES.get("profile_image")

        if uploaded_file:
            upload_dir = os.path.join(settings.MEDIA_ROOT, "faculty_images")
            os.makedirs(upload_dir, exist_ok=True)
            filename = f"{teacher_id}_{uploaded_file.name}"
            filepath = os.path.join(upload_dir, filename)
            with open(filepath, "wb+") as dest:
                for chunk in uploaded_file.chunks():
                    dest.write(chunk)
            profile_image_path = f"faculty_images/{filename}"

        # only hash password if changed
        new_password = row[4]
        if password_input:
            new_password = make_password(password_input)

        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE FACULTY
                SET first_name = %s,
                    middle_name = %s,
                    last_name = %s,
                    email = %s,
                    password = %s,
                    profile_image = %s
                WHERE faculty_id = %s
            """, [
                first_name, middle_name, last_name,
                email, new_password,
                profile_image_path, teacher_id
            ])

        return redirect("teacher_profile")

    # sidebar + form values
    context.update({
        "assigned_lab": assigned_lab,
        "first_name": row[0],
        "middle_name": row[1],
        "last_name": row[2],
        "email": row[3],
        "password": "",
        "teacher_profile_image": row[5],
        "current_page": "My Profile",
    })

    return render(request, "teacher/teacher_profile.html", context)


# ---------------------------
# Assignments
# ---------------------------

def _ensure_is_active_column():
    with connection.cursor() as c:
        c.execute("""
        IF COL_LENGTH('ASSIGNED_TEACHER','is_active') IS NULL
           ALTER TABLE ASSIGNED_TEACHER ADD is_active BIT NOT NULL DEFAULT 1
        """)

def _has_active_session_now(assigned_teacher_id: int) -> bool:
    with connection.cursor() as c:
        c.execute("""
            SELECT COUNT(*)
            FROM LAB_SCHEDULE
            WHERE assigned_teacher_id = %s
              AND date = CAST(GETDATE() AS DATE)
              AND status NOT IN ('Cancelled','Rejected')
              AND start_time IS NOT NULL AND end_time IS NOT NULL
              AND CONVERT(TIME, GETDATE()) BETWEEN start_time AND end_time
        """, [assigned_teacher_id])
        (ls_now,) = c.fetchone() or (0,)
        if ls_now > 0:
            return True

        c.execute("""
            SELECT COUNT(*)
            FROM UTILIZATION_SLIP us
            JOIN LAB_SCHEDULE ls ON ls.schedule_id = us.schedule_id
            WHERE ls.assigned_teacher_id = %s
              AND us.date = CAST(GETDATE() AS DATE)
              AND us.status NOT IN ('Cancelled','Rejected')
              AND us.start_time IS NOT NULL AND us.end_time IS NOT NULL
              AND CONVERT(TIME, GETDATE()) BETWEEN us.start_time AND us.end_time
        """, [assigned_teacher_id])
        (us_now,) = c.fetchone() or (0,)
        return us_now > 0

def _cancel_upcoming_rows(assigned_teacher_id: int):
    with connection.cursor() as c:
        # 1) LAB_SCHEDULE: cancel upcoming, skip Cancelled/Rejected/Completed
        c.execute("""
            UPDATE LAB_SCHEDULE
               SET status = 'Cancelled'
             WHERE assigned_teacher_id = %s
               AND (
                    date > CAST(GETDATE() AS DATE)
                 OR (date = CAST(GETDATE() AS DATE) AND start_time >= CONVERT(TIME, GETDATE()))
               )
               AND ISNULL(status,'') NOT IN ('Cancelled','Rejected','Completed')
        """, [assigned_teacher_id])

        # 2) UTILIZATION_SLIP (linked)
        c.execute("""
            UPDATE us
               SET us.status = 'Cancelled'
              FROM UTILIZATION_SLIP us
              JOIN LAB_SCHEDULE ls ON ls.schedule_id = us.schedule_id
             WHERE ls.assigned_teacher_id = %s
               AND (
                    us.date > CAST(GETDATE() AS DATE)
                 OR (us.date = CAST(GETDATE() AS DATE) AND us.start_time >= CONVERT(TIME, GETDATE()))
               )
               AND ISNULL(us.status,'') NOT IN ('Cancelled','Rejected','Completed')
        """, [assigned_teacher_id])

        # 3) UTILIZATION_SLIP orphans
        c.execute("""
            UPDATE UTILIZATION_SLIP
               SET status = 'Cancelled'
             WHERE schedule_id IS NULL
               AND assigned_teacher_id = %s
               AND (
                    date > CAST(GETDATE() AS DATE)
                 OR (date = CAST(GETDATE() AS DATE) AND start_time >= CONVERT(TIME, GETDATE()))
               )
               AND ISNULL(status,'') NOT IN ('Cancelled','Rejected','Completed')
        """, [assigned_teacher_id])

# --------------------- views ---------------------

@require_http_methods(["GET"])
def teacher_assignments(request):
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    context = get_teacher_header_context(teacher_id)
    today = timezone.localtime().date()

    with connection.cursor() as cursor:
        # Lab (if needed elsewhere)
        cursor.execute("SELECT lab_id FROM LABORATORIES WHERE faculty_id = %s", [teacher_id])
        row = cursor.fetchone()
        assigned_lab = row[0] if row else None

        # Current assignments (only active semester)
        cursor.execute("""
            SELECT 
                at.assigned_teacher_id,
                c.course_id, c.course_code, c.course_name,
                s.semester_id, s.term, s.school_year, s.start_date, s.end_date
            FROM ASSIGNED_TEACHER at
            JOIN COURSE   c ON c.course_id = at.course_id
            JOIN SEMESTER s ON s.semester_id = at.semester_id
            WHERE at.faculty_id = %s
              AND ISNULL(at.is_active, 1) = 1
              AND %s BETWEEN s.start_date AND s.end_date
            ORDER BY s.start_date DESC, c.course_code ASC
        """, [teacher_id, today])
        assignments = [{
            "assigned_teacher_id": r[0],
            "course_id": r[1],
            "course_code": r[2],
            "course_name": r[3],
            "semester_id": r[4],
            "term": r[5],
            "school_year": r[6],
            "start_date": r[7],
            "end_date": r[8],
        } for r in cursor.fetchall()]

        # Programs (active)
        cursor.execute("""
            SELECT program_id, program_code, program_name
            FROM PROGRAM
            WHERE is_active = 1
            ORDER BY program_code ASC
        """)
        programs = [{
            "program_id": r[0],
            "program_code": r[1],
            "program_name": r[2],
        } for r in cursor.fetchall()]

        # Courses (include program_id; full list)
        cursor.execute("""
            SELECT c.course_id, c.program_id, (c.course_code + ' — ' + c.course_name) AS label
            FROM COURSE c
            ORDER BY c.course_code ASC
        """)
        courses = [{
            "course_id": r[0],
            "program_id": r[1],
            "label": r[2],
        } for r in cursor.fetchall()]

        # Semesters (CURRENT ONLY)
        cursor.execute("""
            SELECT semester_id, term, school_year, start_date, end_date
            FROM SEMESTER
            WHERE CAST(GETDATE() AS DATE) BETWEEN start_date AND end_date
            ORDER BY start_date DESC
        """)
        semesters_rows = cursor.fetchall()
        semesters = [{
            "semester_id": r[0],
            "label": f"{r[1]} — A.Y. {r[2]}",
            "start_date": r[3],
            "end_date": r[4],
        } for r in semesters_rows]

        # Map of THIS teacher's active assignments per semester (for client-side exclusion)
        cursor.execute("""
            SELECT semester_id, course_id
            FROM ASSIGNED_TEACHER
            WHERE faculty_id = %s
              AND ISNULL(is_active, 1) = 1
        """, [teacher_id])
        rows = cursor.fetchall()
        assigned_map = {}
        for sid, cid in rows:
            if sid is not None and cid is not None:
                assigned_map.setdefault(str(int(sid)), []).append(int(cid))
        assigned_map_json = json.dumps(assigned_map)

    context.update({
        "current_page": "Assignments",
        "assignments": assignments,
        "programs": programs,
        "courses": courses,
        "semesters": semesters,
        "assigned_lab": assigned_lab,
        "assigned_map_json": assigned_map_json,
        # for JS convenience
        "semesters_count": len(semesters),
    })
    return render(request, "teacher/teacher_assignments.html", context)

@require_POST
def create_assignment(request):
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id  = request.session.get("user_id")
    program_id  = request.POST.get("program_id")
    course_id   = request.POST.get("course_id")
    semester_id = request.POST.get("semester_id")

    if not course_id or not semester_id:
        messages.error(request, "Please select both course and semester.")
        return redirect("teacher_assignments")

    _ensure_is_active_column()

    with connection.cursor() as cursor:
        # Validate semester (must be *current*)
        cursor.execute("""
            SELECT term, school_year, start_date, end_date
            FROM SEMESTER 
            WHERE semester_id = %s
              AND CAST(GETDATE() AS DATE) BETWEEN start_date AND end_date
        """, [semester_id])
        sem = cursor.fetchone()
        if not sem:
            messages.error(request, "Selected semester is not active.")
            return redirect("teacher_assignments")
        term, sy, sem_start, sem_end = sem

        # Source of truth: program on COURSE
        cursor.execute("SELECT program_id, course_code, course_name FROM COURSE WHERE course_id=%s", [course_id])
        c = cursor.fetchone()
        if not c:
            messages.error(request, "Selected course was not found.")
            return redirect("teacher_assignments")
        actual_program_id, course_code, course_name = c

        if program_id and str(actual_program_id or "") != str(program_id or ""):
            messages.error(request, "Selected course does not belong to the chosen program.")
            return redirect("teacher_assignments")

        # Duplicate guard (active only)
        cursor.execute("""
            SELECT COUNT(*) 
            FROM ASSIGNED_TEACHER
            WHERE faculty_id=%s AND course_id=%s AND semester_id=%s AND ISNULL(is_active,1)=1
        """, [teacher_id, course_id, semester_id])
        (dupe_count,) = cursor.fetchone() or (0,)
        if dupe_count > 0:
            messages.info(request, "You already have this course for the selected semester.")
            return redirect("teacher_assignments")

        cursor.execute("""
            INSERT INTO ASSIGNED_TEACHER (faculty_id, course_id, semester_id, assigned_by, is_active)
            VALUES (%s, %s, %s, %s, 1)
        """, [teacher_id, course_id, semester_id, teacher_id])

    messages.success(request, "Assignment added.")
    return redirect("teacher_assignments")

@require_POST
def unassign_assignment(request, assigned_teacher_id):
    if not request.session.get("user_id") or request.session.get("role") != "teacher":
        return redirect("login")

    teacher_id = request.session.get("user_id")
    _ensure_is_active_column()

    try:
        with transaction.atomic():
            with connection.cursor() as c:
                c.execute("""
                    SELECT COUNT(*) 
                    FROM ASSIGNED_TEACHER
                    WHERE assigned_teacher_id=%s AND faculty_id=%s
                """, [assigned_teacher_id, teacher_id])
                (cnt,) = c.fetchone() or (0,)
                if cnt == 0:
                    messages.error(request, "Unauthorized: assignment not found.")
                    return redirect("teacher_assignments")

            if _has_active_session_now(assigned_teacher_id):
                messages.error(request, "Cannot unassign: there is a session currently in progress for this assignment.")
                return redirect("teacher_assignments")

            _cancel_upcoming_rows(assigned_teacher_id)

            with connection.cursor() as c:
                c.execute("""
                    UPDATE ASSIGNED_TEACHER
                       SET is_active = 0
                     WHERE assigned_teacher_id=%s
                """, [assigned_teacher_id])

        messages.success(request, "Assignment inactivated. All future sessions and slips were cancelled.")
        return redirect("teacher_assignments")

    except Exception as e:
        messages.error(request, f"Could not inactivate assignment: {e}")
        return redirect("teacher_assignments")
    
def logout_view(request):
    request.session.flush()        # clears session (server + cookie)
    return redirect("login")

def _push_unread_to_channel(faculty_id: int):
    """
    After inserting a NOTIFICATIONS row for a faculty member, call this
    to fire a Pusher event so their header reloads & unread badge updates.
    """
    if not faculty_id:
        return

    with connection.cursor() as cur:
        # Get role (admin/teacher) so we know which channel prefix to use
        cur.execute("""
            SELECT role 
            FROM FACULTY 
            WHERE faculty_id = %s AND ISNULL(is_archived,0) = 0
        """, [faculty_id])
        row = cur.fetchone()
        if not row:
            return

        role = (row[0] or "").strip().lower()
        if role not in ("admin", "teacher"):
            return

        # Count unread notifications for this faculty
        cur.execute("""
            SELECT COUNT(*) 
            FROM NOTIFICATIONS
            WHERE receiver_teacher_id = %s AND status = 'Unread'
        """, [faculty_id])
        cnt_row = cur.fetchone()
        unread = cnt_row[0] if cnt_row else 0

    channel = f"{role}-{faculty_id}"  # matches pusher.js subscriptions

    try:
        realtime.trigger(channel, "notification", {
            "unread_count": unread,
        })
    except Exception as e:
        # Don't break the main flow if Pusher fails
        print("Pusher error while pushing unread count:", e)