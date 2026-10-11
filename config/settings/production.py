# config/settings/production.py
"""
Production settings — extends base.py.
Use: DJANGO_SETTINGS_MODULE=config.settings.production
"""

import os

from .base import *  # noqa: F401, F403

# ─────────────────────────────────────────────────────────────────────────────
# Core
# ─────────────────────────────────────────────────────────────────────────────

DEBUG = False

ALLOWED_HOSTS = [
    "panel.retano.ir",
    "www.retano.ir",
    "retano.ir",
    "194.59.170.105",
    "127.0.0.1",
    "localhost",
    "185.81.96.206",
]

# ─────────────────────────────────────────────────────────────────────────────
# Security — full hardening
# ─────────────────────────────────────────────────────────────────────────────

SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_SECURE = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

CSRF_COOKIE_SECURE = False
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

CSRF_TRUSTED_ORIGINS = [
    "https://retano.ir",
    "https://retano.ir",
    "https://panel.retano.ir",
    "http://194.59.170.105",
]
# ─────────────────────────────────────────────────────────────────────────────
# CORS — whitelist only the React frontend origin
# ─────────────────────────────────────────────────────────────────────────────

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = False

# ─────────────────────────────────────────────────────────────────────────────
# DRF — JSON only, no browsable API in production
# ─────────────────────────────────────────────────────────────────────────────

REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [  # noqa: F405
    "rest_framework.renderers.JSONRenderer",
]

# ─────────────────────────────────────────────────────────────────────────────
# Email — real SMTP in production
# ─────────────────────────────────────────────────────────────────────────────

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@retano.ir")

# ─────────────────────────────────────────────────────────────────────────────
# Logging — structured file + console logging
# ─────────────────────────────────────────────────────────────────────────────

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{levelname}] {asctime} {name} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "json": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[{server_time}] {message}",
            "style": "{",
        },
    },
    "filters": {
        "require_debug_false": {
            "()": "django.utils.log.RequireDebugFalse",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "/var/log/retano/django.log",
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 10,
            "formatter": "verbose",
        },
        "mail_admins": {
            "level": "ERROR",
            "class": "django.utils.log.AdminEmailHandler",
            "filters": ["require_debug_false"],
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["file", "mail_admins"],
            "level": "ERROR",
            "propagate": False,
        },
        "retano": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
