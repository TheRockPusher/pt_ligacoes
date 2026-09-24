"""Shared settings. Each environment explicitly selects its security posture."""

from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

PLATFORM_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEBUG = False
ENABLE_ADMIN = False
REQUIRE_VITE_MANIFEST = False
SECRET_KEY = ""
ALLOWED_HOSTS = []


def postgres_database(url):
    if not url or not url.startswith(("postgres://", "postgresql://")):
        raise ImproperlyConfigured("DATABASE_URL must be an explicit PostgreSQL URL.")
    try:
        database = dj_database_url.parse(url, conn_max_age=60, conn_health_checks=True)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured("DATABASE_URL is not a valid PostgreSQL URL.") from exc
    if not database.get("NAME"):
        raise ImproperlyConfigured("DATABASE_URL requires a database name.")
    database.setdefault("OPTIONS", {}).update(
        options="-c statement_timeout=10000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=10000"
    )
    return database


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ligacoes.core.apps.CoreConfig",
    "ligacoes.public.apps.PublicConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "config.middleware.ResponseSecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PLATFORM_ROOT / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 14},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "pt-pt"
TIME_ZONE = "Europe/Lisbon"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = PLATFORM_ROOT / "staticfiles"
VITE_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
VITE_MANIFEST_PATH = VITE_DIST_DIR / ".vite" / "manifest.json"
STATICFILES_DIRS = [VITE_DIST_DIR] if VITE_DIST_DIR.is_dir() else []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_AUTOREFRESH = False
WHITENOISE_USE_FINDERS = False
WHITENOISE_INDEX_FILE = False
WHITENOISE_ROOT = None
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 262144
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200
FILE_UPLOAD_MAX_MEMORY_SIZE = 0
APPEND_SLASH = True
LOGIN_URL = "/admin/login/"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False}
    },
}
