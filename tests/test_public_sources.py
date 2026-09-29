from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.urls import reverse

from ligacoes.core.catalogue import DATASETS
from ligacoes.core.events import EventInput, PartyInput, sync_events
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Event, Evidence, Relationship, Source
from ligacoes.core.services import publish_relationship

pytestmark = pytest.mark.django_db
EVIDENCE_DATASET = "ept_declaracoes"
EVENT_DATASET = "base_contratos"
RETRIEVED = datetime(2025, 1, 15, 12, tzinfo=UTC)
LATER = datetime(2026, 6, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_figures():
    cache.clear()
    yield
    cache.clear()


def sources_page(client):
    response = client.get(reverse("public:sources"))
    assert response.status_code == 200
    return response


def entries(response):
    context = response.context
    listed = [entry for group in context["imported"] for entry in group.entries]
    listed += [*context["upcoming"], *context["secondary"]]
    return {entry.dataset.key: entry for entry in listed}


def contract(record_id, buyer, supplier):
    return EventInput(
        record_id=record_id,
        kind="contract",
        title=f"Contrato fictício {record_id}",
        date=date(2026, 3, 1),
        amount=Decimal("100.00"),
        record_url=f"https://example.org/contratos/{record_id}",
        parties=(
            PartyInput("buyer", buyer.name, buyer, "601234561"),
            PartyInput("supplier", supplier.name, supplier),
        ),
    )


def test_every_catalogue_dataset_is_listed_in_its_section_with_anchor_and_links(client):
    body = sources_page(client).content.decode()
    upcoming_at = body.index('id="previstas"')
    secondary_at = body.index('id="secundarias"')
    bounds = {
        "imported": (0, upcoming_at),
        "upcoming": (upcoming_at, secondary_at),
        "secondary": (secondary_at, len(body)),
    }
    for key, dataset in DATASETS.items():
        anchor = body.index(f'id="{key}"')
        start, end = bounds[dataset.status]
        assert start < anchor < end, key
        entry = body[anchor : body.index("</article>", anchor)]
        assert f'href="{dataset.url}"' in entry
        if dataset.licence_url:
            assert f'href="{dataset.licence_url}"' in entry
    # Hints are never presented as evidence, and planned sources publish nothing yet.
    assert "pista, nunca evidência" in body[secondary_at:]
    assert "ainda não importada" in body[upcoming_at:secondary_at]


def test_figures_count_only_public_relationships_and_events(client, catalog, reviewer):
    Source.objects.filter(pk=catalog.source.pk).update(dataset=EVIDENCE_DATASET)
    # A draft claim from the same dataset, retrieved later, must not move any figure.
    later = Source.objects.create(
        title="Declaração fictícia posterior",
        url="https://example.org/declaracao-posterior",
        publisher="Arquivo de exemplo fictício",
        retrieved_at=LATER,
        is_public=True,
        dataset=EVIDENCE_DATASET,
    )
    draft = Relationship.objects.create(
        subject=catalog.person, object=catalog.company, kind="directorship"
    )
    Evidence.objects.create(relationship=draft, source=later, excerpt="Rascunho.", is_public=True)
    # Private evidence from another dataset on the claim. Adding evidence withdraws
    # a reviewed claim, so it is attached before publication.
    hidden = Source.objects.create(
        title="Composição fictícia",
        url="https://example.org/composicao",
        publisher="Arquivo de exemplo fictício",
        retrieved_at=LATER,
        is_public=True,
        dataset="gov_composicao",
    )
    Evidence.objects.create(
        relationship=catalog.relation, source=hidden, excerpt="Privada.", is_public=False
    )
    publish_relationship(catalog.relation, reviewer)

    buyer = official_entity(
        "nipc", "601234561", name="Município Fictício", kind="organisation", classification=""
    )
    supplier = official_entity(
        "nipc", "500000000", name="Construtora Fictícia", kind="company", classification=""
    )
    withheld = official_entity(
        "nipc", "501000119", name="Papelaria Fictícia", kind="company", classification=""
    )
    unanchored = Entity.objects.create(
        name="Empresa sem identificador", slug="empresa-sem-id", kind="company", is_public=True
    )
    sync_events(
        dataset=EVENT_DATASET,
        scope="2026",
        events=[
            contract("c-1", buyer, supplier),
            contract("c-2", buyer, unanchored),
            contract("c-3", buyer, withheld),
        ],
        as_of=date(2026, 9, 1),
    )
    assert set(Event.objects.values_list("record_id", "status")) == {
        ("c-1", "published"),
        ("c-2", "draft"),
        ("c-3", "published"),
    }
    Entity.objects.filter(pk=withheld.pk).update(is_public=False)
    Event.objects.filter(record_id="c-1").update(retrieved_at=RETRIEVED)
    Event.objects.exclude(record_id="c-1").update(retrieved_at=LATER)

    listed = entries(sources_page(client))
    evidence_entry = listed[EVIDENCE_DATASET]
    assert (evidence_entry.relationships, evidence_entry.events) == (1, 0)
    assert evidence_entry.retrieved_at == RETRIEVED
    assert listed["gov_composicao"].relationships == 0
    assert listed["gov_composicao"].retrieved_at is None
    event_entry = listed[EVENT_DATASET]
    assert (event_entry.relationships, event_entry.events) == (0, 1)
    assert event_entry.retrieved_at == RETRIEVED


def test_figures_are_shared_until_refreshed(client, published, django_assert_num_queries):
    Source.objects.filter(pk=published.source.pk).update(dataset=EVIDENCE_DATASET)
    assert entries(sources_page(client))[EVIDENCE_DATASET].relationships == 1
    Evidence.objects.filter(pk=published.evidence.pk).update(is_public=False)
    # Within the refresh window the page is served without touching the database.
    with django_assert_num_queries(0):
        cached = entries(sources_page(client))
    assert cached[EVIDENCE_DATASET].relationships == 1
    cache.clear()
    assert entries(sources_page(client))[EVIDENCE_DATASET].relationships == 0


def test_header_and_footer_link_to_sources_and_paths(client):
    sources = reverse("public:sources")
    body = client.get(reverse("public:index")).content.decode()
    header = body[body.index('class="site-nav"') : body.index("</nav>")]
    footer = body[body.index('class="footer-nav"') :]
    assert f'href="{sources}"' in header
    assert f'href="{reverse("public:path_finder")}"' in header
    assert f'href="{sources}"' in footer
    page = sources_page(client).content.decode()
    assert f'href="{sources}" aria-current="page"' in page
