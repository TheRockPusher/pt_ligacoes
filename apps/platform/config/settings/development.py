"""Explicit local development settings, never selected by default."""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import postgres_database

DEBUG = True
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be set to a generated local development secret.")
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
ENABLE_ADMIN = True
DATABASES = {
    "default": postgres_database(
        os.environ.get(
            "DATABASE_URL", "postgresql://pt_ligacoes:pt_ligacoes@localhost:5432/pt_ligacoes"
        )
    )
}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True
SECURE_SSL_REDIRECT = False
