from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils.html import strip_tags
def send_account_email_simple(*, to_email: str, full_name: str, role_label: str,
                              username_label: str, username_value: str,
                              raw_password: str):
    # Base URL for SmartLab
    base_url = getattr(settings, "SITE_BASE_URL", "http://192.168.100.111:8000").rstrip("/")

    # Use the named URL "login" so it always matches urls.py
    login_path = reverse("login")          # this will be "/" in your case
    login_url = f"{base_url}{login_path}"  # -> "http://192.168.100.111:8000/"

    subject = f"{role_label} Account Created — NEUST SmartLab"
    html = f"""
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">
        <p>Hi <strong>{full_name}</strong>,</p>
        <p>Your {role_label.lower()} account has been created in <strong>NEUST SmartLab</strong>.</p>

        <p><strong>Account details:</strong></p>
        <ul>
          <li><strong>{username_label}:</strong> {username_value}</li>
          <li><strong>Temporary Password:</strong> {raw_password}</li>
        </ul>

        <p>Please sign in and change your password immediately.</p>

        <div style="margin-top:20px;">
          <p>You can access NEUST SmartLab here:</p>
          <a href="{login_url}" 
             style="display:inline-block;padding:10px 16px;background:#0056b3;color:white;
                    text-decoration:none;border-radius:6px;">
            Open SmartLab
          </a>
          <p style="margin-top:8px;font-size:12px;">
            If the button doesn’t work, copy this link:<br>
            <a href="{login_url}">{login_url}</a>
          </p>
        </div>

        <p style="margin-top:16px;">— NEUST SmartLab</p>
      </div>
    """
    text = strip_tags(html)

    from_email = (
        getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or getattr(settings, "EMAIL_HOST_USER", None)
    )

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=from_email,
        to=[to_email],
    )
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=False)
def send_student_rfid_account_email(*, to_email: str, full_name: str, stud_num: str):
    """
    Simple student email:
    - Confirms account creation
    - Shows full name + student number
    - Instructs them to go to the admin to have RFID assigned
    """
    subject = "Student Account Created — NEUST SmartLab"
    html = f"""
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">
        <p>Hi <strong>{full_name}</strong>,</p>
        <p>Your <strong>student account</strong> has been created in <strong>NEUST SmartLab</strong>.</p>

        <p><strong>Account details:</strong></p>
        <ul>
          <li><strong>Full Name:</strong> {full_name}</li>
          <li><strong>Student Number:</strong> {stud_num}</li>
        </ul>

        <p>
          You currently <strong>do not have an RFID card assigned</strong>.
          Please visit the <strong>SmartLab Admin</strong> to have your RFID Card
           and linked to your account before using the laboratory facilities.
        </p>

        <p style="margin-top:16px;">— NEUST SmartLab</p>
      </div>
    """
    text = strip_tags(html)

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=from_email,
        to=[to_email],
    )
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=False)   # set True later if you want it quiet