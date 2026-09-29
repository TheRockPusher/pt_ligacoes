from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from ligacoes.core.events import EventInput, PartyInput, sync_events, withdraw_event
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Event, EventParty, SourceIdentity
from ligacoes.public.selectors import event_counterparts, event_totals, public_events

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
DATASET = "base_contratos"


@pytest.fixture
def parties():
    buyer = official_entity(
        "nipc", "601234561", name="Município Fictício", kind="organisation", classification=""
    )
    supplier = official_entity(
        "nipc", "500000000", name="Construtora Fictícia", kind="company", classification=""
    )
    other = official_entity(
        "nipc", "501000119", name="Papelaria Fictícia", kind="company", classification=""
    )
    unanchored = Entity.objects.create(
        name="Empresa sem identificador", slug="empresa-sem-id", kind="company", is_public=True
    )
    return buyer, supplier, other, unanchored


def contract(record_id, buyer, supplier, *, amount="100.00", day=DAY, title=None, **changes):
    options = {
        "record_id": record_id,
        "kind": "contract",
        "title": title or f"Contrato fictício {record_id}",
        "date": day,
        "amount": Decimal(amount),
        "record_url": f"https://example.org/contratos/{record_id}",
        "parties": (
            PartyInput("buyer", buyer.name, buyer, "601234561"),
            PartyInput(
                "supplier",
                supplier.name if isinstance(supplier, Entity) else supplier,
                supplier if isinstance(supplier, Entity) else None,
            ),
        ),
    }
    options.update(changes)
    return EventInput(**options)


def sync(events, *, day=DAY, scope="2026", **options):
    return sync_events(dataset=DATASET, scope=scope, events=events, as_of=day, **options)


def test_only_events_whose_every_party_is_resolved_and_anchored_publish(parties):
    buyer, supplier, _, unanchored = parties
    result = sync(
        [
            contract("c-1", buyer, supplier),
            contract("c-2", buyer, unanchored),
            contract("c-3", buyer, "Fornecedor por identificar"),
        ]
    )
    assert result == {
        "created": 3,
        "changed": 0,
        "unchanged": 0,
        "ceased": 0,
        "published": 1,
        "draft": 2,
    }
    statuses = dict(Event.objects.values_list("record_id", "status"))
    assert statuses == {"c-1": "published", "c-2": "draft", "c-3": "draft"}
    assert list(public_events().values_list("record_id", flat=True)) == ["c-1"]
    published = Event.objects.get(record_id="c-1")
    assert published.published_at is not None and published.source.is_public
    assert published.source.dataset == DATASET
    unresolved = EventParty.objects.get(event__record_id="c-3", role="supplier")
    assert unresolved.entity is None and unresolved.name == "Fornecedor por identificar"


def test_changed_record_replaces_parties_and_unchanged_keeps_publication(parties):
    buyer, supplier, other, _ = parties
    sync([contract("c-1", buyer, supplier)])
    first = Event.objects.get()
    result = sync([contract("c-1", buyer, supplier)], day=DAY + timedelta(days=1))
    assert (result["unchanged"], result["changed"]) == (1, 0)
    current = Event.objects.get()
    assert current.published_at == first.published_at and current.as_of == DAY + timedelta(days=1)
    result = sync([contract("c-1", buyer, other, amount="90.00")], day=DAY + timedelta(days=2))
    assert (result["changed"], result["published"]) == (1, 1)
    current = Event.objects.get()
    assert current.pk == first.pk and current.amount == Decimal("90.00")
    assert set(current.parties.values_list("entity_id", flat=True)) == {buyer.pk, other.pk}
    assert current.parties.count() == 2


def test_complete_scope_ceases_missing_records_and_a_return_republishes(parties):
    buyer, supplier, other, _ = parties
    sync([contract("c-1", buyer, supplier), contract("c-2", buyer, other)])
    sync([contract("o-1", buyer, other)], scope="2025")
    partial = sync([contract("c-1", buyer, supplier)], day=DAY + timedelta(days=1), complete=False)
    assert partial["ceased"] == 0
    result = sync([contract("c-1", buyer, supplier)], day=DAY + timedelta(days=2))
    assert result["ceased"] == 1
    statuses = dict(Event.objects.values_list("record_id", "status"))
    assert statuses == {"c-1": "published", "c-2": "ceased", "o-1": "published"}
    assert set(public_events().values_list("record_id", flat=True)) == {"c-1", "o-1"}
    returned = sync(
        [contract("c-1", buyer, supplier), contract("c-2", buyer, other)],
        day=DAY + timedelta(days=3),
    )
    assert returned["published"] == 2
    assert Event.objects.get(record_id="c-2").status == "published"
    with pytest.raises(ValidationError):
        sync([contract("c-1", buyer, supplier)], day=DAY)


def test_withdrawn_event_is_never_republished_or_modified(parties, django_user_model):
    buyer, supplier, other, _ = parties
    sync([contract("c-1", buyer, supplier)])
    event = Event.objects.get()
    outsider = django_user_model.objects.create_user(username="fictional-outsider")
    with pytest.raises(PermissionDenied):
        withdraw_event(event, outsider)
    editor = django_user_model.objects.create_user(username="fictional-event-editor")
    editor.user_permissions.add(Permission.objects.get(codename="withdraw_event"))
    withdraw_event(event, editor)
    assert not public_events().exists()
    result = sync(
        [contract("c-1", buyer, other, title="Título fictício alterado")],
        day=DAY + timedelta(days=1),
    )
    assert (result["published"], result["changed"], result["ceased"]) == (0, 0, 0)
    sync([], day=DAY + timedelta(days=2))
    event.refresh_from_db()
    assert event.status == "withdrawn" and event.withdrawn_by == editor
    # Only a run that still lists the record refreshes its reference date.
    assert event.title == "Contrato fictício c-1" and event.as_of == DAY + timedelta(days=1)
    assert set(event.parties.values_list("entity_id", flat=True)) == {buyer.pk, supplier.pk}
    assert not public_events().exists()


def test_hidden_party_or_private_dataset_source_hides_a_published_event(parties):
    buyer, supplier, _, _ = parties
    sync([contract("c-1", buyer, supplier)])
    supplier.is_public = False
    supplier.save()
    assert Event.objects.get().status == "published"
    assert not public_events().exists()
    supplier.is_public = True
    supplier.save()
    source = Event.objects.get().source
    source.is_public = False
    source.save()
    assert not public_events().exists()
    # A private dataset source is an editorial decision that later runs respect.
    sync([contract("c-1", buyer, supplier)], day=DAY + timedelta(days=1))
    assert Event.objects.get().source == source and not public_events().exists()


def test_streamed_batches_detect_repeated_records(parties):
    buyer, supplier, _, _ = parties

    def stream():
        yield contract("c-1", buyer, supplier)
        yield contract("c-2", buyer, supplier)
        yield contract("c-1", buyer, supplier)

    with pytest.raises(ValidationError):
        sync(stream(), batch_size=1)
    assert not Event.objects.exists()
    assert sync(iter([contract("c-1", buyer, supplier)]), batch_size=1)["created"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": Decimal("-1.00")},
        {"start_date": DAY, "end_date": DAY - timedelta(days=1)},
        {"parties": ()},
        {"kind": "party_membership"},
        {"record_url": "https://localhost/contrato"},
    ],
)
def test_invalid_record_writes_nothing(parties, changes):
    buyer, supplier, _, _ = parties
    with pytest.raises(ValidationError):
        sync([contract("c-0", buyer, supplier), contract("c-1", buyer, supplier, **changes)])
    assert not Event.objects.exists()


def test_counterparts_and_totals_aggregate_only_public_events(parties):
    buyer, supplier, other, unanchored = parties
    sync(
        [
            contract("c-1", buyer, supplier, amount="100.00", day=date(2025, 3, 1)),
            contract("c-2", buyer, supplier, amount="250.50", day=date(2026, 5, 2)),
            contract("c-3", buyer, other, amount="10.00", day=date(2026, 1, 1)),
            contract("c-4", buyer, unanchored, amount="9999.00", day=date(2026, 1, 1)),
            contract(
                "s-1",
                buyer,
                supplier,
                kind="subsidy",
                amount="40.00",
                day=date(2024, 6, 1),
                parties=(
                    PartyInput("grantor", buyer.name, buyer),
                    PartyInput("beneficiary", supplier.name, supplier),
                ),
            ),
        ]
    )
    rows = list(event_counterparts(buyer))
    assert rows == [
        {
            "counterpart_id": supplier.pk,
            "kind": "contract",
            "role_of_entity": "buyer",
            "role_of_counterpart": "supplier",
            "count": 2,
            "amount_total": Decimal("350.50"),
            "first_date": date(2025, 3, 1),
            "last_date": date(2026, 5, 2),
        },
        {
            "counterpart_id": supplier.pk,
            "kind": "subsidy",
            "role_of_entity": "grantor",
            "role_of_counterpart": "beneficiary",
            "count": 1,
            "amount_total": Decimal("40.00"),
            "first_date": date(2024, 6, 1),
            "last_date": date(2024, 6, 1),
        },
        {
            "counterpart_id": other.pk,
            "kind": "contract",
            "role_of_entity": "buyer",
            "role_of_counterpart": "supplier",
            "count": 1,
            "amount_total": Decimal("10.00"),
            "first_date": date(2026, 1, 1),
            "last_date": date(2026, 1, 1),
        },
    ]
    assert [row["counterpart_id"] for row in event_counterparts(supplier)] == [buyer.pk, buyer.pk]
    assert [
        (row["kind"], row["count"])
        for row in event_counterparts(buyer, kind="contract", at=date(2025, 12, 31))
    ] == [("contract", 1)]
    totals = {row["kind"]: (row["count"], row["amount_total"]) for row in event_totals(buyer)}
    assert totals == {"contract": (3, Decimal("360.50")), "subsidy": (1, Decimal("40.00"))}


def test_publishing_freezes_anchor_identities_but_not_hints(parties):
    buyer, supplier, other, _ = parties
    hint = SourceIdentity.objects.create(source="wikidata", external_id="Q1", entity=supplier)
    sync([contract("c-1", buyer, supplier)])
    anchor = SourceIdentity.objects.get(source="nipc", entity=supplier)
    assert anchor.used_at is not None
    anchor.entity = other
    with pytest.raises(ValidationError):
        anchor.save()
    with pytest.raises(ValidationError):
        anchor.delete()
    hint.refresh_from_db()
    assert hint.used_at is None


def test_unchanged_records_restamp_anchors_that_predate_the_freeze(parties):
    buyer, supplier, other, _ = parties
    sync([contract("c-1", buyer, supplier)])
    SourceIdentity.objects.update(used_at=None)
    result = sync([contract("c-1", buyer, supplier)])
    assert result["unchanged"] == 1
    anchor = SourceIdentity.objects.get(source="nipc", entity=supplier)
    assert anchor.used_at is not None
    anchor.entity = other
    with pytest.raises(ValidationError):
        anchor.save()
    with pytest.raises(ValidationError):
        anchor.delete()


def test_migration_freezes_anchors_of_published_events_only(parties):
    buyer, supplier, other, unanchored = parties
    sync([contract("c-1", buyer, supplier), contract("c-2", buyer, unanchored)])
    hint = SourceIdentity.objects.create(source="wikidata", external_id="Q2", entity=supplier)
    SourceIdentity.objects.update(used_at=None)
    freeze = import_module("ligacoes.core.migrations.0014_freeze_event_anchors")
    with connection.schema_editor() as schema_editor:
        freeze.freeze_event_anchors(apps, schema_editor)
    stamped = {i.entity_id for i in SourceIdentity.objects.exclude(used_at=None)}
    assert stamped == {buyer.pk, supplier.pk}
    hint.refresh_from_db()
    assert hint.used_at is None
    assert not SourceIdentity.objects.filter(entity=other, used_at__isnull=False).exists()
