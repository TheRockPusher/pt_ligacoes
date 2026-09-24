"""Fail-closed settings for the single trusted Railway ingress."""

import os
import re
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured(
        "SECRET_KEY must be a strong, unique production secret of at least 50 characters."
    )
ALLOWED_HOSTS = [
    host.strip().lower() for host in os.environ.get("ALLOWED_HOSTS", "").split(",") if host.strip()
]
if not ALLOWED_HOSTS or any(
    not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", host)
    or host in {"localhost", "0.0.0.0", "127.0.0.1"}  # noqa: S104 - Reject bind addresses as hosts.
    for host in ALLOWED_HOSTS
):
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS requires explicit public hostnames, without wildcards, schemes or ports."
    )
DATABASES = {"default": postgres_database(os.environ.get("DATABASE_URL", ""))}  # noqa: F405
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
for origin in CSRF_TRUSTED_ORIGINS:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or "*" in origin
    ):
        raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS must contain exact HTTPS origins.")
ENABLE_ADMIN = os.environ.get("ENABLE_ADMIN", "false").lower() == "true"
REQUIRE_VITE_MANIFEST = True
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
# Railway terminates TLS and replaces this header. Do not expose Gunicorn directly.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
