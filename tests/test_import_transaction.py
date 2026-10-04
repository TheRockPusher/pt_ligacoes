from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event

import pytest
from django.db import connection, transaction

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


@pytest.mark.django_db(transaction=True)
def test_caught_nested_bulk_failure_cannot_report_a_partial_success():
    with pytest.raises(transaction.TransactionManagementError), import_transaction():
        models.Entity.objects.create(
            name="Antes Fictício", slug="antes-ficticio", kind="organisation"
        )
        try:
            with editorial_transaction():
                models.Entity.objects.create(
                    name="Falha Fictícia", slug="falha-ficticia", kind="organisation"
                )
                raise ValueError("Falha fictícia após uma escrita.")
        except ValueError:
            pass
    assert not models.Entity.objects.exists()
    # A failed bulk scope must not leak its mode into later ordinary editorial work.
    with editorial_transaction():
        models.Entity.objects.create(
            name="Depois Fictício", slug="depois-ficticio", kind="organisation"
        )
        try:
            with editorial_transaction():
                models.Entity.objects.create(
                    name="Rejeitado Fictício", slug="rejeitado-ficticio", kind="organisation"
                )
                raise ValueError("Falha fictícia recuperável.")
        except ValueError:
            pass
    assert list(models.Entity.objects.values_list("slug", flat=True)) == ["depois-ficticio"]


@pytest.mark.django_db(transaction=True)
def test_ordinary_nested_failure_preserves_other_editorial_writes():
    with editorial_transaction():
        models.Entity.objects.create(
            name="Antes Fictício", slug="antes-ficticio", kind="organisation"
        )
        try:
            with editorial_transaction():
                models.Entity.objects.create(
                    name="Rejeitado Fictício", slug="rejeitado-ficticio", kind="organisation"
                )
                raise ValueError("Falha fictícia recuperável.")
        except ValueError:
            pass
        models.Entity.objects.create(
            name="Depois Fictício", slug="depois-ficticio", kind="organisation"
        )
    assert set(models.Entity.objects.values_list("slug", flat=True)) == {
        "antes-ficticio",
        "depois-ficticio",
    }


@pytest.mark.django_db(transaction=True)
def test_copied_bulk_context_cannot_bypass_another_connections_editorial_lock():
    starting = Event()

    def importer():
        try:
            starting.set()
            with import_transaction():
                models.Entity.objects.create(
                    name="Entidade Concorrente Fictícia",
                    slug="entidade-concorrente-ficticia",
                    kind="organisation",
                )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with import_transaction(), connection.cursor() as cursor:
            inherited = copy_context()
            pending = pool.submit(inherited.run, importer)
            assert starting.wait(timeout=5)
            cursor.execute("SELECT pg_sleep(0.3)")
            assert not pending.done()
        pending.result(timeout=5)
    assert models.Entity.objects.get().slug == "entidade-concorrente-ficticia"
