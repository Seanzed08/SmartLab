from django.urls import path
from . import views
from teacher import views as teacher_views
from student import views as student_views
from .views import branding_settings

urlpatterns = [
    # Login / Profile / Logout
    path('', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.admin_profile, name='admin_profile'),
    path('branding/settings/', views.branding_settings, name='branding_settings'),

    # Dashboard
    path('dashboard/', views.dashboard, name='dashboard'),    # optional
    path('print/utilization/<int:utilization_id>/', views.print_utilization_slip, name='print_utilization_slip'),
    path('print/attendance/<int:utilization_id>/', views.print_attendance_sheet, name='print_attendance_sheet'),
    path('dashboard/print-queue/', views.print_queue, name='print_queue'),
    path('dashboard/print-queue-single/', views.print_queue_single, name='print_queue_single'),
    path('dashboard/export-merged-pdf/', views.export_merged_pdf, name='export_merged_pdf'),
    path('dashboard/export-pdfs/', views.export_pdfs, name='export_pdfs'),
    path('api/attendance-preview/', views.attendance_preview_api, name='attendance_preview_api'),


    # Courses
    # Manage Courses
    path('manage-courses/', views.manage_courses, name='manage_courses'),

    # Courses
    path('add-course/', views.add_course, name='add_course'),
    path('edit-course/<int:course_id>/', views.edit_course, name='edit_course'),
    path('restore-course/<int:course_id>/', views.restore_course, name='restore_course'),
    path('hard-delete-course/<int:course_id>/', views.hard_delete_course, name='hard_delete_course'),

    # Semesters
    path('add-semester/', views.add_semester, name='add_semester'),
    path('edit-semester/<int:semester_id>/', views.edit_semester, name='edit_semester'),
    path('restore-semester/<int:semester_id>/', views.restore_semester, name='restore_semester'),
    path('hard-delete-semester/<int:semester_id>/', views.hard_delete_semester, name='hard_delete_semester'),

    # Programs
    path('dashboard/programs/add/', views.add_program, name='add_program'),
    path('dashboard/programs/<int:program_id>/edit/', views.edit_program, name='edit_program'),
    path('dashboard/programs/<int:program_id>/restore/', views.restore_program, name='restore_program'),
    path('dashboard/programs/<int:program_id>/hard-delete/', views.hard_delete_program, name='hard_delete_program'),




    # Students
    path('dashboard/students/', views.manage_students, name='manage_students'),
    path('dashboard/students/add/', views.add_student, name='add_student'),
    path('dashboard/students/<int:student_id>/json/', views.get_student_json, name='get_student_json'),
    path('dashboard/students/<int:student_id>/save/', views.save_student, name='save_student'),
    path('dashboard/students/<int:student_id>/delete/', views.delete_student, name='delete_student'),
    path('dashboard/students/<int:student_id>/restore/', views.restore_student, name='restore_student'),
    path('dashboard/students/<int:student_id>/hard-delete/', views.hard_delete_student, name='hard_delete_student'),
    path('dashboard/students/assign-rfid/', views.assign_rfid, name='assign_rfid'),
    path('dashboard/students/upload-excel/', views.upload_excel, name='upload_excel'),

    # Faculty (all in core.views)
    path('dashboard/manage-faculty/', views.manage_faculty, name='manage_faculty'),
    path('dashboard/manage-faculty/add/', views.add_teacher, name='add_teacher'),
    # soft delete (archive)
    path('dashboard/manage-faculty/delete/<int:teacher_id>/', views.delete_teacher, name='delete_teacher'),
    # hard delete (permanent)
    path('dashboard/manage-faculty/hard-delete/<int:faculty_id>/', views.hard_delete_teacher, name='hard_delete_teacher'),
    # restore
    path('restore-teacher/<int:faculty_id>/', views.restore_teacher, name='restore_teacher'),

    # Modal edit JSON
    path('dashboard/faculty/get/<int:faculty_id>/', views.get_teacher_json, name='get_teacher_json'),
    path('dashboard/faculty/save/<int:faculty_id>/', views.save_teacher_json, name='save_teacher_json'),

    # Assign course
    path('dashboard/assign-course/options/<int:faculty_id>/', views.assign_course_options, name='assign_course_options'),
    path('dashboard/assign-course/save/<int:faculty_id>/', views.assign_course_save, name='assign_course_save'),
    path('unassign-course/<int:assigned_teacher_id>/', views.unassign_course, name='unassign_course'),
    path('dashboard/manage-faculty/upload-excel/', views.upload_teacher_excel, name='upload_teacher_excel'),


    # Teacher RFID
    path('assign-teacher-rfid/', views.assign_teacher_rfid, name='assign_teacher_rfid'),

    # Laboratories
    path('admin/laboratories/', views.manage_laboratories, name='manage_laboratories'),
    path('admin/laboratories/add', views.add_laboratory, name='add_laboratory'),

    # optional legacy edit page (kept; modal is preferred)
    path('manage-laboratories/laboratories/', views.manage_laboratories, name='manage_laboratories'),
    path('manage-laboratories/laboratories/add', views.add_laboratory, name='add_laboratory'),
    path('manage-laboratories/laboratories/<int:lab_id>/archive',  views.archive_laboratory, name='archive_laboratory'),
    path('manage-laboratories/laboratories/<int:lab_id>/restore',  views.restore_laboratory, name='restore_laboratory'),
    path('manage-laboratories/laboratories/<int:lab_id>/hard-delete', views.hard_delete_laboratory, name='hard_delete_laboratory'),
    path('manage-laboratories/labs/<int:lab_id>/json',       views.get_laboratory_json, name='get_laboratory_json'),
    path('manage-laboratories/labs/<int:lab_id>/save-json',  views.save_laboratory_json, name='save_laboratory_json'),
    # Schedule
     path('manage-schedule/', views.manage_schedule, name='manage_schedule'),
    path('admin-week-data/', views.admin_week_data, name='admin_week_data'),
    path('get-active-sessions/', views.get_active_sessions, name='get_active_sessions'),
    path('admin-create-schedule/', views.admin_create_schedule, name='admin_create_schedule'),
# admin urls.py
    path("approve_reservation/<int:slip_id>/", views.approve_reservation, name="admin_approve_reservation"),
    path("reject_reservation/<int:slip_id>/", views.reject_reservation, name="admin_reject_reservation"),

    path('admin-pending-requests/', views.admin_pending_requests, name='admin_pending_requests'),

    # Teacher & Student dashboards
    path('teacher/dashboard/', teacher_views.teacher_dashboard, name='teacher_dashboard'),
    path('student/dashboard/', student_views.student_dashboard, name='student_dashboard'),

    # Misc
    path('test-db/', views.test_db_connection, name='test_db'),
    path('mark-notifications-read/', views.mark_notifications_read, name='admin_mark_notifications_read'),
    path('mark-notifications-read/', views.mark_notifications_read, name='admin_mark_notifications_read'),
    path('notifications/<int:notification_id>/', views.admin_notification_detail, name='admin_notification_detail'),
    path('notifications/load_more/', views.admin_notifications_load_more, name='admin_notifications_load_more'),
    path('rfid-scan/', views.rfid_scan, name='rfid_scan'),
    path('scan-page/', views.rfid_scan_page, name='scan_rfid_page'),
    path('rfid/check-in/', views.rfid_check_in, name='rfid_check_in'),
    path('rfid-listen/', views.rfid_test_page, name='rfid_listen'),
    path('rfid/test/', views.rfid_test_page, name='rfid_test_page'),

    # Operating Time
    path('dashboard/operating-time/', views.manage_operating_time, name='manage_operating_time'),
]
