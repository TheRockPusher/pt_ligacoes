"""An entity listed twice under one role on an event (aliases) counts its amount once."""

from datetime import date
from decimal import Decimal

import pytest

from ligacoes.core.event_summaries import rebuild_event_summaries
from ligacoes.core.events import EventInput, PartyInput, sync_events
from ligacoes.core.identity import official_entity
from ligacoes.public.selectors import event_counterparts, event_totals

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
FAR = date(9999, 12, 31)


def contract(record_id, buyer, supplier, *, aliases=False):
    suppliers = [PartyInput("supplier", supplier.name, supplier, "2")]
    if aliases:
        suppliers.append(PartyInput("supplier", f"{supplier.name}, Lda.", supplier, "3"))
    return EventInput(
        record_id=record_id,
        kind="contract",
        title=f"Contrato {record_id}",
        date=DAY,
        amount=Decimal("100.00"),
        record_url=f"https://example.org/{record_id}",
        parties=(PartyInput("buyer", buyer.name, buyer, "1"), *suppliers),
    )


@pytest.fixture
def pair():
    buyer, supplier = (
        official_entity(
            "nipc", nipc, name=f"Entidade Fictícia {nipc}", kind="company", classification=""
        )
        for nipc in ("500000000", "501000119")
    )
    sync_events(
        dataset="base_contratos",
        scope="2026",
        as_of=DAY,
        events=[
            contract("a", buyer, supplier, aliases=True),
            contract("b", buyer, supplier),
            contract("c", buyer, supplier),
        ],
    )
    return buyer, supplier


def assert_correct(buyer, supplier):
    for at in (None, FAR):
        for entity, other in ((buyer, supplier), (supplier, buyer)):
            (row,) = event_counterparts(entity, at=at)
            assert row["counterpart_id"] == other.pk
            assert row["count"] == 3
            assert row["amount_total"] == Decimal("300.00")
            (total,) = event_totals(entity, at=at)
            assert (total["count"], total["amount_total"]) == (3, Decimal("300.00"))


def test_alias_party_rows_do_not_double_the_amount_after_sync(pair):
    assert_correct(*pair)


def test_alias_party_rows_do_not_double_the_amount_after_rebuild(pair):
    rebuild_event_summaries()
    assert_correct(*pair)


def test_live_filters_and_slices_keep_the_deduplicated_totals():
    buyer, supplier, third = (
        official_entity(
            "nipc", nipc, name=f"Entidade Fictícia {nipc}", kind="company", classification=""
        )
        for nipc in ("500000000", "501000119", "501000208")
    )
    # Both sides aliased: four joined rows for one 100.00 event.
    both = EventInput(
        record_id="both",
        kind="contract",
        title="Contrato both",
        date=DAY,
        amount=Decimal("100.00"),
        record_url="https://example.org/both",
        parties=(
            PartyInput("buyer", buyer.name, buyer, "1"),
            PartyInput("buyer", f"{buyer.name}, S.A.", buyer, "4"),
            PartyInput("supplier", supplier.name, supplier, "2"),
            PartyInput("supplier", f"{supplier.name}, Lda.", supplier, "3"),
        ),
    )
    sync_events(
        dataset="base_contratos",
        scope="2026",
        as_of=DAY,
        events=[both, contract("other", buyer, third)],
    )

    def summary(rows):
        return [(row["counterpart_id"], row["count"], row["amount_total"]) for row in rows]

    everyone = event_counterparts(buyer, at=FAR)
    assert sorted(summary(everyone)) == sorted(
        [(supplier.pk, 1, Decimal("100.00")), (third.pk, 1, Decimal("100.00"))]
    )
    assert summary(event_counterparts(buyer, at=FAR, counterpart=supplier)) == [
        (supplier.pk, 1, Decimal("100.00"))
    ]
    assert summary(event_counterparts(buyer, at=FAR, name="501000208")) == [
        (third.pk, 1, Decimal("100.00"))
    ]
    assert len(everyone[:1]) == 1
    assert [*everyone[:1], *everyone[1:]] == list(everyone)
