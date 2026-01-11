"""
Django settings for CAPSTONE project.
"""

from pathlib import Path
import os

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
SECRET_KEY = 'django-insecure-9rd#f)siuc6tgn0bdtimr#9umk_lk*e66avo6qtykmz93n2nqw'
DEBUG = True
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '192.168.100.111']
SITE_BASE_URL = "http://192.168.100.111:8000"
# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # project apps
    'core',
    'teacher',
    'student',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',   # <-- must be active
]

ROOT_URLCONF = 'CAPSTONE.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # Your global template dir (keep app templates via APP_DIRS=True)
        'DIRS': [BASE_DIR / 'core' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                # default
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # helpful in templates when dealing with static/media
                'django.template.context_processors.media',
                'django.template.context_processors.static',
                # 🔹 our branding context processor (for brand_name & brand_logo_url)
                'core.context_processors.branding',
                'core.context_processors.pusher_keys',
            ],
            'libraries': {
                # custom template tags
                'custom_filters': 'teacher.templatetags.custom_filters',
            },
        },
    },
]

LOGIN_URL = '/'
AUTH_USER_MODEL = 'core.CustomUser'
WSGI_APPLICATION = 'CAPSTONE.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'mssql',
        'NAME': 'SmartLab',
        'USER': 'sa',
        'PASSWORD': '2580',
        'HOST': 'SEAN\\MSSQLSERVER01',
        'PORT': '',
        'OPTIONS': {
            'driver': 'ODBC Driver 17 for SQL Server',
        },
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
# (optional but recommended for collectstatic in prod)
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media (uploaded files like the sidebar logo)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'seanzt08@gmail.com'
EMAIL_HOST_PASSWORD = 'psix qxrs ghru zleo'  # Gmail App Password
DEFAULT_FROM_EMAIL = 'SMARTLAB <seanzt08@gmail.com>'


# ========= SSRS (ReportServer) =========
SSRS_BASE_URL = "http://sean/ReportServer"
SSRS_UTILIZATION_REPORT_PATH = "/UtilizationSlip"
SSRS_ATTENDANCE_REPORT_PATH  = "/AttendanceSheet"

SSRS_UTIL_PARAM_NAME   = "UtilizationId"
SSRS_ATTEND_PARAM_NAME = "UtilizationId"

SSRS_AUTH_MODE = "NTLM"

# Option A – raw string
SSRS_NTLM_USER = r"SEAN\reportuser"

# or Option B – normal string
SSRS_NTLM_USER = "SEAN\\reportuser"

SSRS_NTLM_PASS = "StrongPassword123!"
SSRS_VERIFY_TLS = False


PUSHER_APP_ID = "2080588"
PUSHER_KEY = "d6725d162a2e3ab1624b"
PUSHER_SECRET = "34a230eb5a01251e795f"
PUSHER_CLUSTER = "ap1"
