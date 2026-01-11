from django.urls import path
from . import views

urlpatterns = [
    


    path('dashboard/', views.teacher_dashboard, name='teacher_dashboard'),
    path('dashboard-data/', views.teacher_dashboard_data, name='teacher_dashboard_data'),

    path('profile/', views.teacher_profile, name='teacher_profile'),


    # Attendance Records
     path("attendance-records/", views.attendance_records, name="attendance_records"),
    path("attendance-records/<int:utilization_id>/preview/", views.attendance_record_preview, name="attendance_record_preview"),
    path("attendance-records/<int:utilization_id>/print/", views.print_combined_slip, name="print_combined_slip"),

    # Toolbar (bulk)
    path("attendance-records/print-queue/", views.teacher_print_queue, name="teacher_print_queue"),
    path("attendance-records/export-merged/", views.teacher_export_merged_pdf, name="teacher_export_merged_pdf"),
    path("attendance-records/export-zip/", views.teacher_export_zip, name="teacher_export_zip"),

    # NEW: single-session SSRS PDFs for teacher (used by modal)
    path("attendance-records/<int:utilization_id>/ssrs/slip/", views.teacher_print_utilization_slip, name="teacher_print_utilization_slip"),
    path("attendance-records/<int:utilization_id>/ssrs/attendance/", views.teacher_print_attendance_sheet, name="teacher_print_attendance_sheet"),
    path(
    "attendance-records/<int:utilization_id>/ssrs/both/",
    views.teacher_print_both_ssrs,
    name="teacher_print_both_ssrs"
    ),

    # Lab Availability
    path('lab-availability/', views.view_lab_availability, name='view_lab_availability'),
    path("teacher/submit-utilization-request/", views.submit_utilization_request, name="submit_utilization_request"),
    path('get-active-sessions/', views.get_active_sessions, name='get_active_sessions'),
     path('teacher/approve_reservation/<int:res_id>/', views.approve_reservation, name='approve_reservation'),
     path('teacher/reject_reservation/<int:res_id>/', views.reject_reservation, name='reject_reservation'),
     path('update-schedule-section/', views.update_schedule_section, name='update_schedule_section'),


    
    # Schedule
    path('schedule/', views.teacher_schedule, name='teacher_schedule'),
    path('create-schedule/', views.create_schedule, name='create_schedule'),
    path('schedule/week-data/', views.lab_week_data, name='lab_week_data'),
    path('schedule/update-section', views.update_schedule_section, name='update_schedule_section'),
    path('profile/', views.teacher_profile, name='teacher_profile'),

     path('mark-notifications-read/', views.mark_teacher_notifications_read, name='mark_notifications_read'),

    # NEW:
    path('notifications/load_more', views.notifications_load_more, name='notifications_load_more'),
    path('notifications/<int:notif_id>/', views.notification_detail, name='notification_detail'),

     # urls.py
     path('assignments/', views.teacher_assignments, name='teacher_assignments'),
     path('assignments/create/', views.create_assignment, name='create_assignment'),
     # urls.py
     path('assignments/unassign/<int:assigned_teacher_id>/', views.unassign_assignment, name='unassign_assignment'),

    path("logout/", views.logout_view, name="logout")

]
