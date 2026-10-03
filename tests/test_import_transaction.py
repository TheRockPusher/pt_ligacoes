from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection

from ligacoes.core import models
from ligacoes.core.models import editorial_transaction, import_transaction

pytestmark = pytest.mark.django_db
SETTINGS = ("statement_timeout", "idle_in_transaction_session_timeout", "lock_timeout")


def timeouts() -> dict[str, str]:
    with connection.cursor() as cursor:
        values = {}
        for name in SETTINGS:
            cursor.execute(f"SHOW {name}")
            values[name] = cursor.fetchone()[0]
        return values


@pytest.mark.django_db(transaction=True)
def test_import_transaction_lifts_timeouts_only_for_its_transaction():
    strict = timeouts()
    with import_transaction():
        assert timeouts() == {
            "statement_timeout": "15min",
            "idle_in_transaction_session_timeout": "15min",
            "lock_timeout": "30min",
        }
    assert timeouts() == strict


def test_editorial_transaction_keeps_timeouts_and_import_nests_in_it():
    strict = timeouts()
    with editorial_transaction():
        assert timeouts() == strict
        with import_transaction():
            assert timeouts()["lock_timeout"] == "30min"


@pytest.mark.django_db(transaction=True)
def test_import_waits_for_editorial_lock_beyond_its_statement_timeout(monkeypatch):
    monkeypatch.setattr(models, "IMPORT_TIMEOUT", "100ms")
    monkeypatch.setattr(models, "IMPORT_LOCK_TIMEOUT", "5s")
    starting = Event()

    def importer():
        try:
            starting.set()
            with import_transaction(), connection.cursor() as cursor:
                cursor.execute("SHOW statement_timeout")
                return cursor.fetchone()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with editorial_transaction(), connection.cursor() as cursor:
            pending = pool.submit(importer)
            assert starting.wait(timeout=5)
            # The holder stays in its transaction beyond the import statement timeout.
            # Only lock_timeout should bound acquisition; statements tighten afterwards.
            cursor.execute("SELECT pg_sleep(0.3)")
            assert not pending.done()
        assert pending.result(timeout=5) == ("100ms",)
