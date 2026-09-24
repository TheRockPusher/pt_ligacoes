"""Deterministic PostgreSQL-only tests; DATABASE_URL is mandatory."""

import os

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-only-key-not-for-deployment-pt-ligacoes-000000000"  # noqa: S105 - Isolated tests.
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
DATABASES = {"default": postgres_database(os.environ.get("DATABASE_URL", ""))}  # noqa: F405
DATABASES["default"]["CONN_MAX_AGE"] = 0
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SECURE_SSL_REDIRECT = False
