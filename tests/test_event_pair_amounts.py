"""A pair carries an amount only when it is the money relation itself."""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from ligacoes.core.event_summaries import rebuild_event_summaries
from ligacoes.core.events import EventInput, PartyInput, sync_events
from ligacoes.core.identity import official_entity
from ligacoes.public.graph_data import events_label
from ligacoes.public.selectors import event_counterparts, event_totals

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
FAR = date(9999, 12, 31)
NIPCS = ("500000000", "501000119", "501000208", "501000305", "501000402", "501000500")


def event(record_id, kind, amount, *parties):
    return EventInput(
        record_id=record_id,
        kind=kind,
        title=f"Registo {record_id}",
        date=DAY,
        amount=Decimal(amount),
        record_url=f"https://example.org/{record_id}",
        parties=tuple(PartyInput(role, who.name, who) for role, who in parties),
    )


@pytest.fixture
def world():
    names = ["buyer", "supplier", "bidder", "grantor", "first", "second"]
    people = {
        name: official_entity(
            "nipc", nipc, name=f"Entidade Fictícia {name}", kind="company", classification=""
        )
        for name, nipc in zip(names, NIPCS, strict=True)
    }
    p = people
    sync_events(
        dataset="base_contratos",
        scope="2026",
        as_of=DAY,
        events=[
            event(
                "c-1",
                "contract",
                "100.00",
                ("buyer", p["buyer"]),
                ("supplier", p["supplier"]),
                ("bidder", p["bidder"]),
            ),
            event("c-2", "contract", "50.00", ("buyer", p["buyer"]), ("supplier", p["supplier"])),
        ],
    )
    sync_events(
        dataset="igf_subvencoes",
        scope="2026",
        as_of=DAY,
        events=[
            event(
                "s-1",
                "subsidy",
                "40.00",
                ("grantor", p["grantor"]),
                ("beneficiary", p["first"]),
                ("beneficiary", p["second"]),
            )
        ],
    )
    # The whole project amount is recorded on every party, as in EU funding records.
    sync_events(
        dataset="pt2030_operacoes",
        scope="2026",
        as_of=DAY,
        events=[
            event(
                "f-1",
                "eu_funding",
                "1000.00",
                ("beneficiary", p["first"]),
                ("supplier", p["supplier"]),
                ("intermediary", p["bidder"]),
            )
        ],
    )
    return p


def pairs(entity, at):
    return {
        (
            row["counterpart_id"],
            row["kind"],
            row["role_of_entity"],
            row["role_of_counterpart"],
        ): (row["count"], row["amount_total"])
        for row in event_counterparts(entity, at=at)
    }


def check(p):
    for at in (None, FAR):
        buyer = pairs(p["buyer"], at)
        # Buyer and supplier: the money relation keeps its sum ...
        assert buyer[(p["supplier"].pk, "contract", "buyer", "supplier")] == (2, Decimal("150.00"))
        # ... a bidder only shares the record.
        assert buyer[(p["bidder"].pk, "contract", "buyer", "bidder")] == (1, None)

        supplier = pairs(p["supplier"], at)
        assert supplier[(p["buyer"].pk, "contract", "supplier", "buyer")] == (2, Decimal("150.00"))
        assert supplier[(p["first"].pk, "eu_funding", "supplier", "beneficiary")] == (1, None)
        assert supplier[(p["bidder"].pk, "contract", "supplier", "bidder")] == (1, None)

        grantor = pairs(p["grantor"], at)
        assert grantor[(p["first"].pk, "subsidy", "grantor", "beneficiary")] == (
            1,
            Decimal("40.00"),
        )
        assert pairs(p["first"], at)[(p["grantor"].pk, "subsidy", "beneficiary", "grantor")] == (
            1,
            Decimal("40.00"),
        )
        # Co-beneficiaries and an intermediary share the record without the money.
        assert pairs(p["first"], at)[(p["second"].pk, "subsidy", "beneficiary", "beneficiary")] == (
            1,
            None,
        )
        assert pairs(p["bidder"], at)[
            (p["first"].pk, "eu_funding", "intermediary", "beneficiary")
        ] == (
            1,
            None,
        )

        # Money relations rank before pairs without an amount.
        counterparts = [
            (row["counterpart_id"], row["amount_total"])
            for row in event_counterparts(p["buyer"], kind="contract", at=at)
        ]
        assert counterparts == [(p["supplier"].pk, Decimal("150.00")), (p["bidder"].pk, None)]

    # An entity's totals still describe its own records, whatever its role.
    for at in (None, FAR):
        totals = {row["kind"]: row["amount_total"] for row in event_totals(p["bidder"], at=at)}
        assert totals == {"contract": Decimal("100.00"), "eu_funding": Decimal("1000.00")}
    assert all(
        list(event_counterparts(entity)) == list(event_counterparts(entity, at=FAR))
        for entity in p.values()
    )


def test_only_money_relations_sum_after_sync(world):
    check(world)


def test_only_money_relations_sum_after_rebuild(world):
    rebuild_event_summaries()
    check(world)


def test_profile_shows_no_amount_for_a_bidder_and_explains_the_rule(client, world):
    url = reverse("public:entity_detail", kwargs={"slug": world["buyer"].slug})
    response = client.get(url)
    assert "Montantes só são somados entre entidade adjudicante e adjudicatário" in (
        response.content.decode()
    )
    sections = {section.kind: section for section in response.context["event_sections"]}
    assert [(row.role, row.amount) for row in sections["contract"].counterparts] == [
        ("Adjudicatário", "150,00\u00a0€"),
        ("Concorrente", ""),
    ]


@pytest.mark.parametrize("at", ["", "2030-01-01"])
def test_events_page_header_sums_only_a_money_pair(client, world, at):
    def page(counterpart):
        params = {"com": world[counterpart].slug, **({"at": at} if at else {})}
        url = reverse("public:entity_events", kwargs={"slug": world["buyer"].slug})
        return client.get(url, params)

    supplier = page("supplier")
    assert supplier.context["total_amount"] == "150,00\u00a0€"
    assert "Montante do registo" in supplier.content.decode()

    # The bidder shares the first contract but was not paid its price.
    bidder = page("bidder")
    body = bidder.content.decode()
    assert bidder.context["total_amount"] == ""
    assert "<dt>Montante registado</dt><dd>—</dd>" in body
    assert "Montantes só são somados entre entidade adjudicante" in body
    # The record keeps its own price, labelled as the record's.
    assert "100,00\u00a0€" in body
    assert "Montante do registo" in body


def test_events_page_without_a_counterpart_keeps_the_entitys_own_total(client, world):
    url = reverse("public:entity_events", kwargs={"slug": world["bidder"].slug})
    response = client.get(url)
    assert response.context["total_amount"] == "1\u202f100,00\u00a0€"
    assert "Montante do registo" not in response.content.decode()


def test_a_pair_without_an_amount_is_labelled_by_its_shared_records():
    assert events_label("contract", 2, None) == "2 registos em comum"
    assert events_label("eu_funding", 1, None) == "1 registo em comum"
    assert events_label("contract", 2, Decimal("150.00")) == "2 contratos \u00b7 150 €"
