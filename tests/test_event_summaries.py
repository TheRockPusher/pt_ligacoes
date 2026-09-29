"""The stored per-entity and per-pair summaries must equal the live aggregation."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command

from ligacoes.core.events import EventInput, PartyInput, sync_events, withdraw_event
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Event, EventEntitySummary, EventPairSummary
from ligacoes.public.selectors import (
    event_counterparts,
    event_datasets,
    event_totals,
    public_events,
)

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
# A date after every event: the live path then aggregates exactly the public events.
FAR = date(9999, 12, 31)
CONTRACTS = "base_contratos"
SUBSIDIES = "igf_subvencoes"


@pytest.fixture
def entities():
    return [
        official_entity(
            "nipc", nipc, name=f"Entidade Fictícia {nipc}", kind="company", classification=""
        )
        for nipc in ("500000000", "501000119", "501000208", "501000305")
    ]


def contract(record_id, buyer, supplier, *, amount="100.00", day=DAY):
    return EventInput(
        record_id=record_id,
        kind="contract",
        title=f"Contrato {record_id}",
        date=day,
        amount=Decimal(amount),
        record_url=f"https://example.org/{record_id}",
        parties=(
            PartyInput("buyer", buyer.name, buyer, "1"),
            PartyInput("supplier", supplier.name, supplier, "2"),
        ),
    )


def subsidy(record_id, grantor, beneficiary, *, amount="40.00"):
    return EventInput(
        record_id=record_id,
        kind="subsidy",
        title=f"Apoio {record_id}",
        start_date=DAY,
        end_date=DAY + timedelta(days=30),
        amount=Decimal(amount),
        record_url=f"https://example.org/{record_id}",
        parties=(
            PartyInput("grantor", grantor.name, grantor),
            PartyInput("beneficiary", beneficiary.name, beneficiary),
        ),
    )


def sync(dataset, events, *, scope="2026", day=DAY):
    return sync_events(dataset=dataset, scope=scope, events=events, as_of=day)


def assert_summaries_match_live(entities):
    """Stored rows (no date) and live aggregation (far date) agree for every entity."""
    for entity in entities:
        stored = list(event_counterparts(entity))
        assert stored == list(event_counterparts(entity, at=FAR)), entity.name
        assert list(event_totals(entity)) == list(event_totals(entity, at=FAR)), entity.name
        assert sorted(event_datasets(entity), key=lambda row: row["dataset"]) == sorted(
            event_datasets(entity, at=FAR), key=lambda row: row["dataset"]
        )
    # No stray rows for entities outside the public events.
    assert public_events().exists() == EventPairSummary.objects.exists()


def populate(a, b, c, d):
    sync(
        CONTRACTS,
        [
            contract("c-1", a, b, amount="100.00"),
            contract("c-2", a, b, amount="250.50", day=DAY - timedelta(days=90)),
            contract("c-3", a, c, amount="10.00"),
            contract("c-4", b, c, amount="7.00"),
        ],
    )
    sync(SUBSIDIES, [subsidy("s-1", a, b), subsidy("s-2", d, b, amount="5.00")])


def test_summaries_follow_sync_change_and_cease(entities):
    a, b, c, d = entities
    populate(a, b, c, d)
    assert_summaries_match_live(entities)
    assert EventEntitySummary.objects.filter(entity=a).count() == 2

    # A changed record swaps a party and its counterpart rows move with it.
    sync(CONTRACTS, [contract("c-1", a, d), contract("c-2", a, b), contract("c-3", a, c)])
    assert_summaries_match_live(entities)
    assert {row["counterpart_id"] for row in event_counterparts(d)} >= {a.pk}

    # c-4 is absent from a complete snapshot and ceases.
    assert Event.objects.get(record_id="c-4").status == "ceased"
    assert not [
        row for row in event_counterparts(b, kind="contract") if row["counterpart_id"] == c.pk
    ]
    # Another dataset's rows are untouched by a rebuild of this one.
    assert event_datasets(b)


def test_summaries_follow_withdrawal(entities, django_user_model):
    a, b, c, d = entities
    populate(a, b, c, d)
    editor = django_user_model.objects.create_user(username="fictional-event-editor")
    editor.user_permissions.add(Permission.objects.get(codename="withdraw_event"))
    editor = django_user_model.objects.get(pk=editor.pk)
    withdraw_event(Event.objects.get(record_id="c-3"), editor)
    assert_summaries_match_live(entities)
    assert not [
        row for row in event_counterparts(a, kind="contract") if row["counterpart_id"] == c.pk
    ]


def test_summaries_follow_entity_and_source_visibility(entities):
    a, b, c, d = entities
    populate(a, b, c, d)
    c.is_public = False
    c.save()
    assert_summaries_match_live(entities)
    assert c.pk not in {row["counterpart_id"] for row in event_counterparts(a)}
    # Events with the hidden party vanish from everyone's totals, not only the pairs.
    assert sum(row["count"] for row in event_totals(a) if row["kind"] == "contract") == 2

    c.is_public = True
    c.save()
    assert_summaries_match_live(entities)
    assert c.pk in {row["counterpart_id"] for row in event_counterparts(a)}

    source = Event.objects.get(record_id="s-1").source
    source.is_public = False
    source.save()
    assert_summaries_match_live(entities)
    assert not list(event_totals(d))
    source.is_public = True
    source.save()
    assert_summaries_match_live(entities)
    assert [row["kind"] for row in event_totals(d)] == ["subsidy"]


def test_rebuild_command_restores_deleted_summaries(entities):
    populate(*entities)
    before = list(EventPairSummary.objects.order_by("pk").values_list("entity", "counterpart"))
    EventPairSummary.objects.all().delete()
    EventEntitySummary.objects.all().delete()
    call_command("rebuild_event_summaries", "--dataset", SUBSIDIES)
    assert {row.dataset for row in EventPairSummary.objects.all()} == {SUBSIDIES}
    call_command("rebuild_event_summaries")
    assert sorted(EventPairSummary.objects.values_list("entity", "counterpart")) == sorted(before)
    assert_summaries_match_live(Entity.objects.all())
