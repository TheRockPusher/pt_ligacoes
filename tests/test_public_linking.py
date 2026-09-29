from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from ligacoes.core.catalogue import DATASETS
from ligacoes.core.events import EventInput, PartyInput, sync_events, withdraw_event
from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Event,
    Evidence,
    Relationship,
    Source,
    SourceIdentity,
    Term,
)
from ligacoes.core.services import publish_relationship

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
EUROS = "\u00a0€"


def profile_url(entity):
    return reverse("public:entity_detail", kwargs={"slug": entity.slug})


def events_url(entity):
    return reverse("public:entity_events", kwargs={"slug": entity.slug})


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
    return buyer, supplier, other


def contract(record_id, buyer, supplier, *, amount="100.00", day=DAY, kind="contract"):
    roles = ("buyer", "supplier") if kind == "contract" else ("grantor", "beneficiary")
    return EventInput(
        record_id=record_id,
        kind=kind,
        title=f"Registo fictício {record_id}",
        date=day,
        amount=Decimal(amount) if amount else None,
        record_url=f"https://example.org/registos/{record_id}",
        parties=(
            PartyInput(roles[0], buyer.name, buyer),
            PartyInput(
                roles[1],
                supplier.name if isinstance(supplier, Entity) else supplier,
                supplier if isinstance(supplier, Entity) else None,
            ),
        ),
    )


def sync(events, *, scope="2026", dataset="base_contratos"):
    return sync_events(dataset=dataset, scope=scope, events=events, as_of=DAY)


def titles(response):
    return {row.event.title for row in response.context["events"]}


def test_ledger_shows_role_term_date_precision_and_temporal_status(client, catalog, reviewer):
    term = Term.objects.create(
        kind="legislature",
        code="XVI",
        label="XVI Legislatura",
        institution=catalog.company,
        start_date=date(2024, 3, 26),
    )
    relation = catalog.relation
    relation.role = "Presidente da comissão fictícia"
    relation.term = term
    relation.start_date = date(2019, 1, 1)
    relation.start_precision = "year"
    relation.end_date = date(2020, 3, 31)
    relation.end_precision = "month"
    relation.temporal_status = "ended"
    relation.description = "Contexto fictício " * 20
    relation.save()
    publish_relationship(relation, reviewer)
    body = client.get(profile_url(catalog.person)).content.decode()
    assert "Presidente da comissão fictícia" in body
    assert '<span class="period-term">XVI Legislatura</span>' in body
    # Year and month precision are shown as such, never as an invented day.
    assert '<time datetime="2019">2019</time>' in body
    assert '<time datetime="2020-03">03/2020</time>' in body
    assert "01/01/2019" not in body and "31/03/2020" not in body
    assert "terminada" in body
    # A long source description stays available but secondary (collapsed).
    assert '<details class="period-more">' in body


def test_structure_kinds_are_listed_and_counted(client, catalog, reviewer):
    parent = Entity.objects.create(
        name="Ministério Fictício", slug="ministerio-ficticio", kind="organisation", is_public=True
    )
    unit = Entity.objects.create(
        name="Direção Fictícia", slug="direcao-ficticia", kind="organisation", is_public=True
    )
    relation = Relationship.objects.create(subject=unit, object=parent, kind="part_of")
    Evidence.objects.create(
        relationship=relation, source=catalog.source, excerpt="Estrutura fictícia.", is_public=True
    )
    publish_relationship(relation, reviewer)
    response = client.get(profile_url(parent))
    assert [kind for kind, *_rest in response.context["summary"].breakdown] == ["part_of"]
    assert 'id="tipo-part_of"' in response.content.decode()


def test_profile_shows_public_identifiers_only(client, catalog):
    person = catalog.person
    for scheme, value in [
        ("parliament", "4321"),
        ("wikidata", "Q4242"),
        ("ept", "ept-holder-canary"),
        ("government", "person:government-canary"),
    ]:
        SourceIdentity.objects.create(source=scheme, external_id=value, entity=person)
    response = client.get(profile_url(person))
    body = response.content.decode()
    assert "https://www.parlamento.pt/DeputadoGP/Paginas/Biografia.aspx?BID=4321" in body
    assert "https://www.wikidata.org/wiki/Q4242" in body
    assert "(pista)" in body
    assert "ept-holder-canary" not in body
    assert "government-canary" not in body
    assert [(item.value, item.hint) for item in response.context["identifiers"]] == [
        ("4321", False),
        ("Q4242", True),
    ]
    path = reverse("public:path_finder") + f"?de={person.slug}"
    assert path in body


def test_events_show_only_public_records_with_aggregates(client, parties, django_user_model):
    buyer, supplier, other = parties
    sync(
        [
            contract("c-1", buyer, supplier, amount="100.00", day=date(2025, 3, 1)),
            contract("c-2", buyer, supplier, amount="250.50", day=date(2026, 5, 2)),
            contract("c-3", buyer, other, amount="10.00", day=date(2026, 1, 1)),
            contract("c-4", buyer, "Fornecedor por identificar"),
            contract("c-5", buyer, supplier, amount="7000.00"),
        ]
    )
    sync([contract("c-6", buyer, supplier, amount="8000.00")], scope="2024")
    sync([], scope="2024")
    editor = django_user_model.objects.create_user(username="fictional-event-editor")
    editor.user_permissions.add(Permission.objects.get(codename="withdraw_event"))
    withdraw_event(Event.objects.get(record_id="c-5"), editor)
    assert set(Event.objects.values_list("record_id", "status")) == {
        ("c-1", "published"),
        ("c-2", "published"),
        ("c-3", "published"),
        ("c-4", "draft"),
        ("c-5", "withdrawn"),
        ("c-6", "ceased"),
    }

    profile = client.get(profile_url(buyer))
    [section] = profile.context["event_sections"]
    assert (section.kind, section.count, section.amount) == ("contract", 3, "360,50" + EUROS)
    assert (section.first, section.last) == (date(2025, 3, 1), date(2026, 5, 2))
    rows = [(row.entity, row.count, row.amount, row.role) for row in section.counterparts]
    assert rows == [
        (supplier, 2, "350,50" + EUROS, "Adjudicatário"),
        (other, 1, "10,00" + EUROS, "Adjudicatário"),
    ]
    body = profile.content.decode()
    assert "Fornecedor por identificar" not in body
    datasets = {(use.key, use.events) for use in profile.context["datasets"]}
    assert datasets == {("base_contratos", 3)}
    assert reverse("public:sources") + "#base_contratos" in body

    listed = client.get(events_url(buyer))
    assert titles(listed) == {
        "Registo fictício c-1",
        "Registo fictício c-2",
        "Registo fictício c-3",
    }
    assert [row.event.record_id for row in listed.context["events"]] == ["c-2", "c-3", "c-1"]
    body = listed.content.decode()
    assert "https://example.org/registos/c-2" in body
    assert reverse("public:sources") + "#base_contratos" in body
    for hidden in ["c-4", "c-5", "c-6"]:
        assert f"Registo fictício {hidden}" not in body

    # A counterpart withdrawn from publication hides its events everywhere.
    other.is_public = False
    other.save()
    [section] = client.get(profile_url(buyer)).context["event_sections"]
    assert (section.count, section.amount) == (2, "350,50" + EUROS)
    assert "Registo fictício c-3" not in client.get(events_url(buyer)).content.decode()
    assert client.get(events_url(buyer), {"com": other.slug}).status_code == 404
    assert client.get(events_url(other)).status_code == 404


def test_event_list_filters_by_kind_counterpart_and_date(client, parties):
    buyer, supplier, other = parties
    sync(
        [
            contract("c-1", buyer, supplier, day=date(2025, 3, 1)),
            contract("c-2", buyer, supplier, day=date(2026, 5, 2)),
            contract("c-3", buyer, other, day=date(2026, 1, 1)),
        ]
    )
    sync(
        [contract("s-1", buyer, supplier, kind="subsidy", day=date(2024, 6, 1))],
        dataset="igf_subvencoes",
    )
    url = events_url(buyer)
    assert titles(client.get(url, {"com": supplier.slug})) == {
        "Registo fictício c-1",
        "Registo fictício c-2",
        "Registo fictício s-1",
    }
    assert titles(client.get(url, {"tipo": "subsidy"})) == {"Registo fictício s-1"}
    # Only records dated on or before the observed day; the day itself is included.
    dated = client.get(url, {"at": "2025-03-01", "com": supplier.slug, "tipo": "contract"})
    assert titles(dated) == {"Registo fictício c-1"}
    assert dated.context["page_obj"].paginator.count == 1
    [section] = client.get(profile_url(buyer), {"at": "2025-12-31"}).context["event_sections"][1:]
    assert (section.kind, section.count) == ("subsidy", 1)
    assert client.get(url, {"tipo": "party"}).status_code == 400
    assert client.get(url, {"at": "2025-02-30"}).status_code == 400
    assert client.get(url, {"com": buyer.slug}).status_code == 404


def test_event_list_is_paginated(client, parties):
    buyer, supplier, _other = parties
    sync(
        [
            contract(f"c-{number:02}", buyer, supplier, day=date(2020, 1, 1 + number % 28))
            for number in range(51)
        ]
    )
    first = client.get(events_url(buyer))
    second = client.get(events_url(buyer), {"page": 2})
    assert len(first.context["events"]) == 50
    assert len(second.context["events"]) == 1
    assert titles(first).isdisjoint(titles(second))
    assert first.context["page_obj"].paginator.count == 51


def test_profile_sources_list_relationship_and_event_datasets(client, published, parties):
    buyer, _supplier, _other = parties
    Source.objects.filter(pk=published.source.pk).update(dataset="ar_informacao_base")
    SourceIdentity.objects.create(source="nipc", external_id="502000007", entity=published.company)
    sync([contract("c-1", buyer, published.company)])
    response = client.get(profile_url(published.company))
    uses = {(use.key, use.relationships, use.events) for use in response.context["datasets"]}
    assert uses == {("ar_informacao_base", 1, 0), ("base_contratos", 0, 1)}
    body = response.content.decode()
    for key in ["ar_informacao_base", "base_contratos"]:
        assert reverse("public:sources") + f"#{key}" in body
        assert DATASETS[key].title in body


def test_evidence_page_links_its_catalogue_dataset(client, published):
    Source.objects.filter(pk=published.source.pk).update(dataset="ar_informacao_base")
    response = client.get(reverse("public:evidence_detail", kwargs={"pk": published.evidence.pk}))
    body = response.content.decode()
    assert reverse("public:sources") + "#ar_informacao_base" in body
    assert DATASETS["ar_informacao_base"].title in body
    assert published.source.url in body


def test_directory_filters_by_classification(client, catalog):
    for number, classification in enumerate(["municipality", "municipality", "parish"]):
        Entity.objects.create(
            name=f"Autarquia fictícia {number}",
            slug=f"autarquia-ficticia-{number}",
            kind="organisation",
            classification=classification,
            is_public=True,
        )
    Entity.objects.create(
        name="Autarquia reservada",
        slug="autarquia-reservada",
        kind="organisation",
        classification="municipality",
    )
    index = reverse("public:index")
    everything = client.get(index)
    counts = {
        value: count for value, _label, count, _query in everything.context["classifications"]
    }
    assert counts == {"municipality": 2, "parish": 1, "company": 1}
    municipalities = client.get(index, {"classificacao": "municipality"})
    assert {item.slug for item in municipalities.context["page_obj"]} == {
        "autarquia-ficticia-0",
        "autarquia-ficticia-1",
    }
    assert municipalities.context["page_obj"].paginator.count == 2
    assert "<p>Município</p>" in municipalities.content.decode()
    combined = client.get(index, {"tipo": "company", "classificacao": "municipality"})
    assert list(combined.context["page_obj"]) == []
    assert client.get(index, {"classificacao": "party"}).status_code == 400
