import pytest
from django.db import connection

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
