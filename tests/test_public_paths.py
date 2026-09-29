from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils.text import slugify

from ligacoes.core.events import EventInput, PartyInput, sync_events
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Evidence, Relationship, Source
from ligacoes.core.services import publish_relationship
from ligacoes.public import paths

pytestmark = pytest.mark.django_db
NO_PATH = "Não foi encontrado caminho até {} passos com estes filtros — isto não prova ausência de relação."


@pytest.fixture
def source(db):
    return Source.objects.create(
        title="Registo fictício de ligações",
        url="https://example.org/registo-ficticio",
        publisher="Arquivo fictício",
        is_public=True,
    )


@pytest.fixture
def make(db):
    def entity(name, kind="organisation", classification="", *, public=True):
        return Entity.objects.create(
            name=name,
            slug=slugify(name),
            kind=kind,
            classification=classification,
            is_public=public,
        )

    return entity


@pytest.fixture
def link(source, reviewer):
    def create(subject, obj, kind="employment", *, publish=True):
        relation = Relationship.objects.create(subject=subject, object=obj, kind=kind)
        Evidence.objects.create(
            relationship=relation,
            source=source,
            excerpt=f"{subject.name} e {obj.name}: passagem fictícia.",
            page_reference="registo fictício 1",
            is_public=True,
        )
        if publish:
            publish_relationship(relation, reviewer)
        return relation

    return create


@pytest.fixture
def people(make):
    return make("Alda Fictícia", "person"), make("Bruno Fictício", "person")


def search(client, start, end, **params):
    response = client.get(
        reverse("public:path_finder"), {"de": start.slug, "para": end.slug, **params}
    )
    assert response.status_code == 200
    return response


def routes(response):
    return [[node.name for node in path.nodes] for path in response.context["paths"]]


def test_finds_up_to_three_shortest_paths_with_every_hop_sourced(client, make, link, people):
    alda, bruno = people
    offices = [make(f"Instituto Fictício {number}") for number in range(4)]
    for office in offices:
        link(alda, office, "employment")
        link(bruno, office, "directorship")
    # A longer route must never be offered while a shorter one exists.
    detour = make("Associação Fictícia de Passagem")
    link(alda, detour)
    link(detour, offices[0], "part_of")

    response = search(client, alda, bruno)

    found = routes(response)
    assert len(found) == paths.MAX_PATHS
    assert all(route[0] == alda.name and route[-1] == bruno.name for route in found)
    assert all(len(route) == 3 for route in found)
    assert len({route[1] for route in found}) == paths.MAX_PATHS
    body = response.content.decode()
    for path in response.context["paths"]:
        for hop in path.hops:
            assert hop.relationships
            evidence = hop.relationships[0].public_evidence[0]
            assert reverse("public:evidence_detail", kwargs={"pk": evidence.pk}) in body
    assert "Não indica que as pessoas se conhecem, que agiram em conjunto" in body


def test_drafts_and_private_entities_are_never_traversed_or_offered(client, make, link, people):
    alda, bruno = people
    hidden = make("Organização Fictícia Retirada")
    link(alda, hidden)
    link(bruno, hidden)
    Entity.objects.filter(pk=hidden.pk).update(is_public=False)
    unreviewed = make("Associação Fictícia em Rascunho")
    link(alda, unreviewed)
    link(bruno, unreviewed, publish=False)

    response = search(client, alda, bruno, max="6")

    assert response.context["paths"] == []
    body = response.content.decode()
    assert NO_PATH.format(6) in body
    assert hidden.name not in body
    as_endpoint = client.get(reverse("public:path_finder"), {"de": hidden.slug, "para": alda.slug})
    assert as_endpoint.context["start"] is None
    assert as_endpoint.context["search"] is None
    assert hidden.name not in as_endpoint.content.decode()


def test_structural_hub_is_skipped_by_default_and_crossed_on_request(client, make, link, people):
    alda, bruno = people
    parliament = make("Parlamento Fictício", classification="parliament")
    link(alda, parliament, "public_office")
    link(bruno, parliament, "public_office")

    default = search(client, alda, bruno)
    assert default.context["paths"] == []
    assert [hub for hub, _reason in default.context["excluded"]] == [parliament]
    assert "incluir=centrais" in default.context["hubs_url"]

    crossed = search(client, alda, bruno, incluir="centrais")
    assert routes(crossed) == [[alda.name, parliament.name, bruno.name]]
    assert crossed.context["excluded"] == []

    # A hub chosen as an endpoint is always reachable.
    assert routes(search(client, alda, parliament)) == [[alda.name, parliament.name]]


def test_entities_above_the_degree_threshold_are_hubs(client, make, link, people, monkeypatch):
    alda, bruno = people
    busy = make("Fundação Fictícia Muito Ligada")
    for person in (alda, bruno, make("Carla Fictícia", "person")):
        link(person, busy)

    monkeypatch.setattr(paths, "HUB_DEGREE", 3)
    assert routes(search(client, alda, bruno)) == [[alda.name, busy.name, bruno.name]]

    monkeypatch.setattr(paths, "HUB_DEGREE", 2)
    default = search(client, alda, bruno)
    assert default.context["paths"] == []
    assert default.context["excluded"] == [(busy, "mais de 2 ligações publicadas")]
    crossed = search(client, alda, bruno, incluir="centrais")
    assert routes(crossed) == [[alda.name, busy.name, bruno.name]]


def test_depth_limit_is_respected(client, make, link, people):
    alda, bruno = people
    first, middle, last = (make(f"Direção Fictícia {letter}") for letter in "ABC")
    link(alda, first)
    link(first, middle, "part_of")
    link(last, middle, "part_of")
    link(bruno, last)

    short = search(client, alda, bruno, max="3")
    assert short.context["paths"] == []
    assert NO_PATH.format(3) in short.content.decode()
    exact = search(client, alda, bruno, max="4")
    assert routes(exact) == [[alda.name, first.name, middle.name, last.name, bruno.name]]
    for invalid in ("0", "7", "quatro"):
        response = client.get(
            reverse("public:path_finder"), {"de": alda.slug, "para": bruno.slug, "max": invalid}
        )
        assert response.status_code == 400


def test_event_hops_only_on_request_and_only_through_public_events(client, make, link, people):
    alda, bruno = people
    carla = make("Carla Fictícia", "person")
    council = official_entity(
        "nipc", "601234561", name="Município Fictício", kind="organisation", classification=""
    )
    builder = official_entity(
        "nipc", "500000000", name="Construtora Fictícia", kind="company", classification=""
    )
    unanchored = make("Empresa Fictícia Sem Identificador", "company")
    link(alda, council, "public_office")
    link(bruno, builder, "directorship")
    link(carla, unanchored, "directorship")
    sync_events(
        dataset="base_contratos",
        scope="2026",
        as_of=date(2026, 9, 1),
        events=[
            EventInput(
                record_id=f"c-{number}",
                kind="contract",
                title=f"Contrato fictício {number}",
                date=date(2026, 3, number),
                amount=Decimal("100.00"),
                record_url=f"https://example.org/contratos/c-{number}",
                parties=(
                    PartyInput("buyer", council.name, council, "601234561"),
                    PartyInput("supplier", supplier.name, supplier),
                ),
            )
            for number, supplier in ((1, builder), (2, unanchored))
        ],
    )

    assert search(client, alda, bruno, max="6").context["paths"] == []

    with_events = search(client, alda, bruno, incluir="eventos")
    assert routes(with_events) == [[alda.name, council.name, builder.name, bruno.name]]
    hop = with_events.context["paths"][0].hops[1]
    assert hop.relationships == []
    assert [(item.kind, item.count) for item in hop.events] == [("contract", 1)]
    url = reverse("public:entity_events", kwargs={"slug": council.slug}) + f"?com={builder.slug}"
    assert hop.events_url == url
    assert url in with_events.content.decode()

    # The contract with an unanchored supplier stays a draft and joins nothing.
    assert search(client, alda, carla, incluir="eventos").context["paths"] == []


def test_autocomplete_offers_public_entities_only(client, make):
    for number in range(paths.OPTION_LIMIT + 2):
        make(f"Cooperativa Fictícia {number:02d}")
    hidden = make("Cooperativa Fictícia Reservada", public=False)
    url = reverse("public:path_entities")

    response = client.get(url, {"q": "cooperativa fict"})
    options = response.context["options"]
    assert len(options) == paths.OPTION_LIMIT
    assert all(option.is_public for option in options)
    assert client.get(url, {"q": "Reservada"}).context["options"] == []
    assert hidden.name not in client.get(url, {"q": "Reservada"}).content.decode()

    picked = client.get(url, {"campo": "para", "para_q": "Cooperativa Fictícia 03"})
    assert [option.name for option in picked.context["options"]] == ["Cooperativa Fictícia 03"]
    assert 'name="para"' in picked.content.decode()
    assert client.get(url, {"campo": "outro", "q": "Cooperativa"}).status_code == 400
