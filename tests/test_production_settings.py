import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTING_NAMES = {
    "DJANGO_SETTINGS_MODULE",
    "SECRET_KEY",
    "ALLOWED_HOSTS",
    "DATABASE_URL",
    "CSRF_TRUSTED_ORIGINS",
    "ENABLE_ADMIN",
}
READ_SETTINGS = """
import json
from django.conf import settings
print(json.dumps({
    'debug': settings.DEBUG,
    'admin': settings.ENABLE_ADMIN,
    'hosts': settings.ALLOWED_HOSTS,
    'database_engine': settings.DATABASES['default']['ENGINE'],
    'ssl_redirect': settings.SECURE_SSL_REDIRECT,
    'session_secure': settings.SESSION_COOKIE_SECURE,
    'csrf_secure': settings.CSRF_COOKIE_SECURE,
    'session_httponly': settings.SESSION_COOKIE_HTTPONLY,
    'hsts_seconds': settings.SECURE_HSTS_SECONDS,
    'frame_options': settings.X_FRAME_OPTIONS,
}))
"""


def inspect_settings(overrides=None):
    environment = {key: value for key, value in os.environ.items() if key not in SETTING_NAMES}
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "apps" / "platform"),
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "SECRET_KEY": "test-only-production-validation-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "ALLOWED_HOSTS": "catalog.example.org",
            "DATABASE_URL": "postgresql://fixture:fixture@127.0.0.1:5432/config_test",
            "CSRF_TRUSTED_ORIGINS": "https://catalog.example.org",
        }
    )
    for key, value in (overrides or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    # Settings import only: no Django application startup, database connection,
    # or Vite manifest is needed to prove configuration fails closed.
    return subprocess.run(  # noqa: S603 - Fixed interpreter/code; overrides are test-controlled.
        [sys.executable, "-c", READ_SETTINGS],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_production_requires_secure_transport_and_disables_admin():
    result = inspect_settings()
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert settings["debug"] is False
    assert settings["admin"] is False
    assert settings["hosts"] == ["catalog.example.org"]
    assert settings["database_engine"] == "django.db.backends.postgresql"
    assert settings["ssl_redirect"] is True
    assert settings["session_secure"] is True
    assert settings["csrf_secure"] is True
    assert settings["session_httponly"] is True
    assert settings["hsts_seconds"] >= 31536000
    assert settings["frame_options"] == "DENY"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SECRET_KEY", None),
        ("SECRET_KEY", "short"),
        ("SECRET_KEY", "x" * 80),
        ("SECRET_KEY", "django-insecure-" + "abcdefghij" * 8),
        ("ALLOWED_HOSTS", None),
        ("ALLOWED_HOSTS", "*"),
        ("ALLOWED_HOSTS", "catalog.example.org,*"),
        ("ALLOWED_HOSTS", "localhost"),
        ("ALLOWED_HOSTS", "https://catalog.example.org"),
        ("DATABASE_URL", None),
        ("DATABASE_URL", "sqlite:///database.sqlite3"),
        ("CSRF_TRUSTED_ORIGINS", "http://catalog.example.org"),
        ("CSRF_TRUSTED_ORIGINS", "https://*.example.org"),
    ],
)
def test_invalid_production_configuration_refuses_startup(key, value):
    result = inspect_settings({key: value})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert key in result.stderr
