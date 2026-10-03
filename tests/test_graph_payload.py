from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from django.urls import reverse

from ligacoes.core.events import EventInput, PartyInput, sync_events
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Evidence, Relationship, Source, Term
from ligacoes.core.services import publish_relationship
from ligacoes.public.graph_data import format_amount, format_number, graph_payload

pytestmark = pytest.mark.django_db
DAY = date(2026, 3, 2)


def nipc(seed):
    """A fictional legal-person NIPC with a valid check digit."""
    body = f"5{seed:07d}"
    total = sum(int(digit) * weight for digit, weight in zip(body, range(9, 1, -1), strict=True))
    check = 11 - total % 11
    return body + str(0 if check >= 10 else check)


def organisation(seed, name, *, kind="company", classification="company"):
    return official_entity("nipc", nipc(seed), name=name, kind=kind, classification=classification)


def municipality():
    return organisation(1, "Município Fictício", kind="organisation", classification="municipality")


def record(record_id, kind, amount, parties, day=DAY):
    return EventInput(
        record_id=record_id,
        kind=kind,
        title=f"Registo fictício {record_id}",
        date=day,
        amount=Decimal(amount),
        record_url=f"https://example.org/registos/{record_id}",
        parties=tuple(PartyInput(role, entity.name, entity) for role, entity in parties),
    )


def load(dataset, events):
    sync_events(dataset=dataset, scope="ficticio", events=events, as_of=DAY)


def contract(record_id, buyer, supplier, amount, day=DAY):
    return record(record_id, "contract", amount, [("buyer", buyer), ("supplier", supplier)], day)


def link(subject, object_, source, reviewer, **fields):
    relationship = Relationship.objects.create(
        subject=subject, object=object_, description="Ligação fictícia.", **fields
    )
    Evidence.objects.create(
        relationship=relationship, source=source, excerpt="Prova fictícia.", is_public=True
    )
    return publish_relationship(relationship, reviewer)


def events_url(entity, **params):
    return f"{reverse('public:entity_events', kwargs={'slug': entity.slug})}?{urlencode(params)}"


def edge_data(payload, kind=None):
    return [
        edge["data"] for edge in payload["edges"] if kind is None or edge["data"]["kind"] == kind
    ]


def test_relationship_edges_carry_role_term_and_dates_of_public_claims_only(catalog, reviewer):
    parliament = Entity.objects.create(
        name="Parlamento Fictício",
        slug="parlamento-ficticio",
        kind="organisation",
        classification="parliament",
        is_public=True,
    )
    group = Entity.objects.create(
        name="Grupo Parlamentar Fictício",
        slug="grupo-parlamentar-ficticio",
        kind="organisation",
        classification="parliamentary_group",
        is_public=True,
    )
    term = Term.objects.create(
        kind="legislature",
        code="XF",
        label="Legislatura fictícia",
        institution=parliament,
        start_date=date(2022, 3, 29),
    )
    office = link(
        catalog.person,
        group,
        catalog.source,
        reviewer,
        kind="public_office",
        role="Vice-presidente fictício",
        role_class="deputy_leadership",
        term=term,
        start_date=date(2022, 3, 1),
        start_precision="month",
        temporal_status="current",
    )
    hidden = Entity.objects.create(
        name="Associação oculta fictícia",
        slug="associacao-oculta-ficticia",
        kind="organisation",
        is_public=True,
    )
    link(catalog.person, hidden, catalog.source, reviewer, kind="membership")
    Entity.objects.filter(pk=hidden.pk).update(is_public=False)
    # catalog.relation (to the company) is still a draft.

    payload = graph_payload(catalog.person, at=None, query="")

    assert edge_data(payload) == [
        {
            "id": str(office.pk),
            "source": str(catalog.person.pk),
            "target": str(group.pk),
            "label": "Cargo público",
            "kind": "public_office",
            "role": "Vice-presidente fictício",
            "term": "Legislatura fictícia",
            "start": "2022-03-01",
            "end": None,
            "start_precision": "month",
            "end_precision": "day",
            "temporal_status": "current",
            "declared": False,
            "url": reverse("public:evidence_detail", kwargs={"pk": office.evidence.get().pk}),
        }
    ]
    nodes = {node["data"]["id"]: node["data"] for node in payload["nodes"]}
    assert set(nodes) == {str(catalog.person.pk), str(group.pk)}
    assert (
        nodes[str(group.pk)]["classification"],
        nodes[str(group.pk)]["classification_label"],
    ) == (
        "parliamentary_group",
        "Grupo parlamentar",
    )
    assert (
        nodes[str(catalog.person.pk)]["classification"],
        nodes[str(catalog.person.pk)]["classification_label"],
    ) == ("", "")
    assert payload["truncated"] is False


def test_event_edges_total_public_records_per_counterpart(db):
    buyer = municipality()
    builder = organisation(2, "Construtora Fictícia")
    stationer = organisation(3, "Papelaria Fictícia")
    hidden = organisation(4, "Oficina Oculta Fictícia")
    unanchored = Entity.objects.create(
        name="Empresa sem identificador fictícia",
        slug="empresa-sem-identificador-ficticia",
        kind="company",
        is_public=True,
    )
    load(
        "base_contratos",
        [
            contract("c-1", buyer, builder, "1200000.00"),
            contract("c-2", buyer, builder, "200000.00"),
            contract("c-3", buyer, stationer, "950.00"),
            contract("c-4", buyer, hidden, "5000000.00"),
            # Stays a draft: the supplier holds no official identifier.
            contract("c-5", buyer, unanchored, "7000000.00"),
        ],
    )
    Entity.objects.filter(pk=hidden.pk).update(is_public=False)

    payload = graph_payload(buyer, at=None, query="")

    edges = edge_data(payload)
    assert [(edge["target"], edge["label"]) for edge in edges] == [
        (str(builder.pk), "2 contratos \u00b7 1,4 M€"),
        (str(stationer.pk), "1 contrato \u00b7 950 €"),
    ]
    assert edges[0] == {
        "id": f"events:contract:{builder.pk}:buyer:supplier",
        "source": str(buyer.pk),
        "target": str(builder.pk),
        "label": "2 contratos \u00b7 1,4 M€",
        "kind": "events",
        "event_kind": "contract",
        "event_kind_label": "Contrato público",
        "count": 2,
        "amount": "1400000.00",
        "first": "2026-03-02",
        "last": "2026-03-02",
        "entity_role": "Entidade adjudicante",
        "counterpart_role": "Adjudicatário",
        "url": events_url(buyer, tipo="contract", com=builder.slug),
    }
    assert {node["data"]["id"] for node in payload["nodes"]} == {
        str(buyer.pk),
        str(builder.pk),
        str(stationer.pk),
    }
    # A search narrows the totals to matching counterparts, like the list beside the map.
    searched = graph_payload(buyer, at=None, query="papelaria")
    assert [edge["target"] for edge in edge_data(searched)] == [str(stationer.pk)]


# ``None`` reads the stored summaries; a date aggregates the events live.
@pytest.mark.parametrize("at", [None, date(2030, 1, 1)])
def test_event_edges_are_capped_and_the_rest_counted_once_per_kind(db, at):
    buyer = municipality()
    suppliers = [organisation(10 + n, f"Fornecedor fictício {n:02}") for n in range(16)]
    grantors = [
        organisation(
            40 + n, f"Fundação fictícia {n}", kind="organisation", classification="foundation"
        )
        for n in range(3)
    ]
    first = record(
        "c-0",
        "contract",
        "16000.00",
        [("buyer", buyer), ("supplier", suppliers[0]), ("bidder", suppliers[0])],
    )
    load(
        "base_contratos",
        [
            first,
            *(
                contract(f"c-{n}", buyer, supplier, f"{(16 - n) * 1000}.00")
                for n, supplier in enumerate(suppliers[1:], start=1)
            ),
        ],
    )
    load(
        "igf_subvencoes",
        [
            record(f"s-{n}", "subsidy", "10.00", [("grantor", grantor), ("beneficiary", buyer)])
            for n, grantor in enumerate(grantors)
        ],
    )

    at_param = {"at": at.isoformat()} if at else {}
    payload = graph_payload(buyer, at=at, query="")

    drawn = edge_data(payload, "events")
    assert len(drawn) == 15
    # The bidder row of the first supplier has no amount, so it ranks after every priced
    # pair and takes no edge: 15 contract counterparts are drawn, one supplier is left.
    assert [edge["target"] for edge in drawn] == [str(supplier.pk) for supplier in suppliers[:15]]
    more = [node["data"] for node in payload["nodes"] if node["data"]["kind"] == "more"]
    assert [(node["event_kind"], node["label"], node["count"], node["url"]) for node in more] == [
        ("contract", "+1 entidade", 1, events_url(buyer, tipo="contract", **at_param)),
        ("subsidy", "+3 entidades", 3, events_url(buyer, tipo="subsidy", **at_param)),
    ]
    assert [(edge["source"], edge["target"]) for edge in edge_data(payload, "more")] == [
        (str(buyer.pk), node["id"]) for node in more
    ]
    node_ids = {node["data"]["id"] for node in payload["nodes"]}
    assert str(suppliers[15].pk) not in node_ids
    assert not any(str(grantor.pk) in node_ids for grantor in grantors)


def test_at_hides_later_records_and_ended_relationships(catalog, reviewer):
    buyer = municipality()
    supplier = organisation(2, "Construtora Fictícia")
    link(
        catalog.person,
        supplier,
        catalog.source,
        reviewer,
        kind="directorship",
        start_date=date(2018, 1, 1),
        end_date=date(2019, 12, 31),
    )
    load(
        "base_contratos",
        [
            contract("c-1", buyer, supplier, "100.00", day=date(2020, 6, 1)),
            contract("c-2", buyer, supplier, "200.00"),
        ],
    )

    def drawn(at):
        return [
            (edge["kind"], edge["label"])
            for edge in edge_data(graph_payload(supplier, at=at, query=""))
        ]

    assert drawn(date(2019, 6, 1)) == [("directorship", "Administração")]
    assert drawn(date(2021, 1, 1)) == [("events", "1 contrato \u00b7 100 €")]
    assert drawn(None) == [
        ("directorship", "Administração"),
        ("events", "2 contratos \u00b7 300 €"),
    ]
    dated = edge_data(graph_payload(supplier, at=date(2021, 1, 1), query=""), "events")
    assert dated[0]["url"] == events_url(supplier, tipo="contract", com=buyer.slug, at="2021-01-01")


def test_declared_interests_are_flagged_and_filter_like_the_list(client, catalog, reviewer):
    buyer = municipality()
    supplier = organisation(2, "Construtora Fictícia")
    load("base_contratos", [contract("1", buyer, supplier, "100.00")])
    declaration = Source.objects.create(
        title="Declaração fictícia",
        url="https://example.org/declaracao-ficticia",
        dataset="ept_declaracoes",
        is_public=True,
    )
    client_link = link(catalog.person, supplier, declaration, reviewer, kind="declared_client")
    board = link(catalog.person, buyer, declaration, reviewer, kind="directorship")
    office = link(catalog.person, catalog.company, catalog.source, reviewer, kind="public_office")

    payload = graph_payload(catalog.person, at=None, query="")
    flags = {edge["id"]: edge["declared"] for edge in edge_data(payload)}
    assert flags == {str(client_link.pk): True, str(board.pk): True, str(office.pk): False}
    declared = graph_payload(catalog.person, at=None, query="", declared=True)
    assert {edge["id"] for edge in edge_data(declared)} == {str(client_link.pk), str(board.pk)}
    kind = graph_payload(catalog.person, at=None, query="", kind="declared_client")
    assert [edge["id"] for edge in edge_data(kind)] == [str(client_link.pk)]
    # Event totals are not relationships of a kind: a filtered map leaves them out.
    assert edge_data(graph_payload(supplier, at=None, query=""), "events")
    filtered = graph_payload(supplier, at=None, query="", kind="declared_client")
    assert edge_data(filtered, "events") == []
    assert [edge["id"] for edge in edge_data(filtered)] == [str(client_link.pk)]
    url = reverse("public:graph", kwargs={"slug": catalog.person.slug})
    assert client.get(url, {"tipo": "party"}).status_code == 400
    assert client.get(url, {"declarado": "sim"}).status_code == 400


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("950.40", "950 €"),
        ("999.60", "1 mil €"),
        ("12500.00", "12,5 mil €"),
        ("999960.00", "1 M€"),
        ("1400000.00", "1,4 M€"),
        ("2140000000.00", "2,1 mil M€"),
        ("12345000000000.00", "12\u00a0345 mil M€"),
    ],
)
def test_amounts_are_compact_in_portuguese(amount, expected):
    assert format_amount(Decimal(amount)) == expected


def test_counts_group_thousands_from_five_digits():
    assert (format_number(1234), format_number(12345)) == ("1234", "12\u00a0345")
