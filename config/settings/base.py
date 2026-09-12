# config/settings/base.py
"""
Base settings shared across all environments.
Do NOT use this file directly — use development.py or production.py.
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ─────────────────────────────────────────────────────────────────────────────
# Security
# ─────────────────────────────────────────────────────────────────────────────

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY environment variable is not set.")

# ─────────────────────────────────────────────────────────────────────────────
# Application definition
# ─────────────────────────────────────────────────────────────────────────────

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
]


LOCAL_APPS = [
    "core.apps.CoreConfig",
    "users",
    "notifications",
    "billing.apps.BillingConfig",
    "consultations.apps.ConsultationsConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ─────────────────────────────────────────────────────────────────────────────
# Middleware
# ─────────────────────────────────────────────────────────────────────────────

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",  # must be before CommonMiddleware
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# ─────────────────────────────────────────────────────────────────────────────
# URL / WSGI
# ─────────────────────────────────────────────────────────────────────────────

ROOT_URLCONF = "Retano.urls"
WSGI_APPLICATION = "Retano.wsgi.application"

# ─────────────────────────────────────────────────────────────────────────────
# Templates (kept for Django admin)
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# Database — PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_DB_VARS = ["DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT"]
if not all(os.getenv(var) for var in REQUIRED_DB_VARS):
    raise RuntimeError(
        f"Database environment variables not fully set. "
        f"Required: {', '.join(REQUIRED_DB_VARS)}"
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME"),
        "USER": os.environ.get("DB_USER"),
        "PASSWORD": os.environ.get("DB_PASSWORD"),
        "HOST": os.environ.get("DB_HOST"),
        "PORT": os.environ.get("DB_PORT"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {
            "sslmode": os.environ.get("DB_SSLMODE", "require"),
        },
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Cache — Redis (used for OTP storage and dashboard caching)
# ─────────────────────────────────────────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/1")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
            "IGNORE_EXCEPTIONS": True,  
        },
        "KEY_PREFIX": "retano",
        "TIMEOUT": 300,  # 5 minutes default
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

AUTH_USER_MODEL = "users.CustomUser"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard-view"
LOGOUT_REDIRECT_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation"
            ".UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─────────────────────────────────────────────────────────────────────────────
# Django REST Framework
# ─────────────────────────────────────────────────────────────────────────────

REST_FRAMEWORK = {
    # Authentication
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    # Permissions — every endpoint requires a logged-in user by default
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Pagination — all list endpoints are paginated by default
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardResultsPagination",
    "PAGE_SIZE": 20,
    # Filtering
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    # Renderer — JSON only in production; BrowsableAPI added in development.py
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    # Parser
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    # Exception handling
    "EXCEPTION_HANDLER": "core.exceptions.custom_exception_handler",
    # Schema
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Throttling
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/hour",
        "user": "1000/hour",
        "otp_request": "5/hour",  # used on the OTP endpoint specifically
        "otp_verify": "100/hour",  # per phone; avoids shared-IP lockouts
        "free_consult_create": "5/hour",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Simple JWT
# ─────────────────────────────────────────────────────────────────────────────

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(os.environ.get("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", 60))
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=int(os.environ.get("JWT_REFRESH_TOKEN_LIFETIME_DAYS", 7))
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_OBTAIN_SERIALIZER": (
        "rest_framework_simplejwt.serializers.TokenObtainPairSerializer"
    ),
    "TOKEN_REFRESH_SERIALIZER": (
        "rest_framework_simplejwt.serializers.TokenRefreshSerializer"
    ),
    "TOKEN_BLACKLIST_SERIALIZER": (
        "rest_framework_simplejwt.serializers.TokenBlacklistSerializer"
    ),
}

# ─────────────────────────────────────────────────────────────────────────────
# drf-spectacular (OpenAPI / Swagger)
# ─────────────────────────────────────────────────────────────────────────────

SPECTACULAR_SETTINGS = {
    "TITLE": "Retano API",
    "DESCRIPTION": (
        "Retano360 — Customer Retention & Campaign Management Platform. "
        "All endpoints require JWT Bearer authentication unless stated otherwise."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]",
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": True,
        "displayOperationId": True,
    },
    "PREPROCESSING_HOOKS": [
        "drf_spectacular.hooks.preprocess_exclude_path_format",
    ],
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# Internationalization
# ─────────────────────────────────────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ─────────────────────────────────────────────────────────────────────────────
# Static & Media files
# ─────────────────────────────────────────────────────────────────────────────

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_DIRS = [os.path.join(BASE_DIR, "Retano", "static")]

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

# ─────────────────────────────────────────────────────────────────────────────
# Default primary key
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─────────────────────────────────────────────────────────────────────────────
# OTP configuration
# ─────────────────────────────────────────────────────────────────────────────

OTP_TTL_SECONDS = int(os.environ.get("OTP_TTL_SECONDS", 120))  # 2 minutes
OTP_LENGTH = 4

# ─────────────────────────────────────────────────────────────────────────────
# Kavenegar
# ─────────────────────────────────────────────────────────────────────────────

KAVENEGAR_API_KEY = os.environ.get("KAVENEGAR_API_KEY", "")
SMSIR_API_KEY = os.environ.get("SMSIR_API_KEY", "")
SMSIR_OTP_TEMPLATE_ID = os.environ.get("SMSIR_OTP_TEMPLATE_ID", "")

OTP_PROVIDER = os.environ.get("OTP_PROVIDER", "sms_ir")


# Analytics responses remain available as stale snapshots for one day. Once a
# snapshot is older than its shorter freshness window, it is returned
# immediately and refreshed by Celery instead of making page navigation wait.
ANALYTICS_CACHE_TTL_SECONDS = int(
    os.environ.get("ANALYTICS_CACHE_TTL_SECONDS", 24 * 60 * 60)
)
ANALYTICS_REPORT_FRESH_SECONDS = int(
    os.environ.get("ANALYTICS_REPORT_FRESH_SECONDS", 15 * 60)
)
DASHBOARD_CACHE_FRESH_SECONDS = int(
    os.environ.get("DASHBOARD_CACHE_FRESH_SECONDS", 15 * 60)
)
ANALYTICS_REFRESH_LOCK_SECONDS = int(
    os.environ.get("ANALYTICS_REFRESH_LOCK_SECONDS", 5 * 60)
)


# ── Celery ───────────────────────────────────────────────────────────────────
# Reuses the same Redis instance as CACHES above, on separate logical DBs
# (2 for broker, 3 for result backend) so cache/broker/results never collide.
_REDIS_BASE_URL = REDIS_URL.rsplit("/", 1)[0]  # strips the trailing /1 from REDIS_URL

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", f"{_REDIS_BASE_URL}/2")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", f"{_REDIS_BASE_URL}/3")

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE  # reuse the project's existing TIME_ZONE ("UTC")

# Upload processing can legitimately run for many minutes on a multi-million
# row file. Do not let Celery's own visibility/ack timeouts kill a task that
# is still making progress -- the UploadJob row is the actual progress
# signal, not the task's liveness from the broker's point of view.
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 60 * 60 * 3       # hard kill at 3 hours
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 60 * 2  # 2 hours: task can catch this and mark job failed gracefully

# Dedicated queue so upload processing never competes with other Celery
# workloads this project may add later (emails, SMS via Kavenegar, etc.)
CELERY_TASK_ROUTES = {
    "core.tasks.uploads.*": {"queue": "uploads"},
}

# ── Supabase Storage (used by core/services/storage.py) ─────────────────────
SUPABASE_URL = os.environ.get("API_EXTERNAL_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_UPLOAD_BUCKET = os.environ.get("SUPABASE_UPLOAD_BUCKET", "upload-staging")

# Chunk size for reading Excel rows and COPYing into staging.
UPLOAD_CHUNK_SIZE = int(os.environ.get("UPLOAD_CHUNK_SIZE", "50000"))

# Allocation + normalization for very large uploads is intentionally one
# atomic database phase.  This value is applied with transaction-local
# set_config() by the worker, so it really covers the following SQL calls.
UPLOAD_DB_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("UPLOAD_DB_STATEMENT_TIMEOUT_MS", str(2 * 60 * 60 * 1000))
)

# ─────────────────────────────────────────────────────────────────────────────
# Campaign SMS (send + delivery tracking) — Campaign Detail phase
# ─────────────────────────────────────────────────────────────────────────────
from config.settings.campaign_sms_beat import (  # noqa: E402
    SMSIR_CAMPAIGN_LINE_NUMBER,
    CAMPAIGN_SMS_BEAT_SCHEDULE,
)

CELERY_BEAT_SCHEDULE = dict(CAMPAIGN_SMS_BEAT_SCHEDULE)

CELERY_TASK_ROUTES["core.tasks.campaign_sms.*"] = {"queue": "campaign_sms"}
