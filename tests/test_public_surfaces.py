from datetime import date
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from ligacoes.core.models import Entity, Evidence, Relationship
from ligacoes.core.services import publish_relationship
from ligacoes.public.selectors import public_relationships

pytestmark = pytest.mark.django_db


def profile_url(entity, suffix="entity_detail"):
    return reverse(f"public:{suffix}", kwargs={"slug": entity.slug})


def evidence_url(evidence):
    return reverse("public:evidence_detail", kwargs={"pk": evidence.pk})


def test_drafts_never_expose_claims_or_evidence(client, catalog):
    for url in [reverse("public:index"), profile_url(catalog.person)]:
        response = client.get(url)
        assert response.status_code == 200
        assert catalog.relation.description not in response.content.decode()
        assert catalog.evidence.excerpt not in response.content.decode()
    assert client.get(evidence_url(catalog.evidence)).status_code == 404
    graph = client.get(profile_url(catalog.person, "graph"))
    assert graph.status_code == 200
    assert graph.json()["edges"] == []
    assert public_relationships().count() == 0


@pytest.mark.parametrize("hidden", ["person", "company", "source", "evidence"])
def test_dynamic_withdrawal_closes_every_public_surface(client, published, hidden):
    record = getattr(published, hidden)
    # Deliberately bypass editorial invalidation: every read must enforce its
    # own publication boundary even after an external import or SQL update.
    type(record).objects.filter(pk=record.pk).update(is_public=False)
    assert public_relationships().count() == 0
    assert client.get(evidence_url(published.evidence)).status_code == 404
    response = client.get(profile_url(published.person))
    graph = client.get(profile_url(published.person, "graph"))
    if hidden == "person":
        assert response.status_code == 404
        assert graph.status_code == 404
        index = client.get(reverse("public:index"))
        assert published.person.name not in index.content.decode()
    else:
        assert response.status_code == 200
        assert published.relation.description not in response.content.decode()
        assert graph.status_code == 200
        assert graph.json()["edges"] == []


def test_only_public_evidence_is_returned(client, catalog, reviewer):
    private = Evidence.objects.create(
        relationship=catalog.relation,
        source=catalog.source,
        excerpt="PRIVATE_EVIDENCE_CANARY",
        is_public=False,
    )
    publish_relationship(catalog.relation, reviewer)
    for url in [profile_url(catalog.person), evidence_url(catalog.evidence)]:
        response = client.get(url)
        assert response.status_code == 200
        body = response.content.decode()
        for secret in [
            "PRIVATE_ENTITY_CANARY",
            "PRIVATE_SOURCE_CANARY",
            "PRIVATE_RELATIONSHIP_CANARY",
            "PRIVATE_EVIDENCE_CANARY",
            reviewer.username,
        ]:
            assert secret not in body
    assert client.get(evidence_url(private)).status_code == 404
    graph = client.get(profile_url(catalog.person, "graph")).json()
    assert graph["edges"][0]["data"]["url"] == evidence_url(catalog.evidence)
    assert set(graph["edges"][0]["data"]) == {"id", "source", "target", "label", "url"}
    assert all(set(node["data"]) == {"id", "label", "kind", "url"} for node in graph["nodes"])


def test_private_slug_and_uuid_cannot_be_read_by_guessing(client, catalog):
    Entity.objects.filter(pk=catalog.person.pk).update(is_public=False)
    assert client.get(profile_url(catalog.person)).status_code == 404
    assert client.get(profile_url(catalog.person, "graph")).status_code == 404
    assert client.get(evidence_url(catalog.evidence)).status_code == 404
    assert (
        client.get(
            reverse("public:entity_detail", kwargs={"slug": "unknown-fictional"})
        ).status_code
        == 404
    )
    assert client.get(reverse("public:evidence_detail", kwargs={"pk": uuid4()})).status_code == 404


def test_unreviewed_status_cannot_expose_an_evidence_uuid(client, catalog):
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.filter(pk=catalog.relation.pk).update(status="published")
    assert client.get(evidence_url(catalog.evidence)).status_code == 404
    assert client.get(profile_url(catalog.person, "graph")).json()["edges"] == []


@pytest.mark.parametrize(
    ("start", "end", "at", "visible"),
    [
        (date(2020, 1, 1), date(2022, 12, 31), "2019-12-31", False),
        (date(2020, 1, 1), date(2022, 12, 31), "2020-01-01", True),
        (date(2020, 1, 1), date(2022, 12, 31), "2022-12-31", True),
        (date(2020, 1, 1), date(2022, 12, 31), "2023-01-01", False),
        (None, date(2022, 12, 31), "1900-01-01", True),
        (date(2020, 1, 1), None, "2099-01-01", True),
        (None, None, "2021-06-01", True),
    ],
)
def test_date_filter_matches_profile_graph_and_selector(
    client, catalog, reviewer, start, end, at, visible
):
    catalog.relation.start_date = start
    catalog.relation.end_date = end
    catalog.relation.save()
    publish_relationship(catalog.relation, reviewer)
    selector_ids = set(public_relationships(at=date.fromisoformat(at)).values_list("pk", flat=True))
    assert (catalog.relation.pk in selector_ids) is visible
    response = client.get(profile_url(catalog.person), {"at": at})
    assert response.status_code == 200
    assert (catalog.relation.description in response.content.decode()) is visible
    graph = client.get(profile_url(catalog.person, "graph"), {"at": at}).json()
    assert bool(graph["edges"]) is visible


@pytest.mark.parametrize(
    "invalid", ["2024-02-30", "20240101", "01/01/2024", "not-a-date", "2024-1-1"]
)
def test_bad_dates_are_not_silently_treated_as_all_time(client, catalog, invalid):
    for route in ["entity_detail", "graph"]:
        assert client.get(profile_url(catalog.person, route), {"at": invalid}).status_code == 400


def test_search_is_public_paginated_and_bounded(client, catalog):
    Entity.objects.create(name="Segredo fictício", slug="segredo-ficticio", kind="person")
    for number in range(30):
        Entity.objects.create(
            name=f"Pesquisa ficcional {number:02}",
            slug=f"pesquisa-ficcional-{number:02}",
            kind="organisation",
            is_public=True,
        )
    first = client.get(reverse("public:index"), {"q": "Pesquisa ficcional"})
    second = client.get(reverse("public:index"), {"q": "Pesquisa ficcional", "page": 2})
    assert first.status_code == second.status_code == 200
    first_ids = {item.pk for item in first.context["page_obj"]}
    second_ids = {item.pk for item in second.context["page_obj"]}
    assert len(first_ids) == 24
    assert len(second_ids) == 6
    assert first_ids.isdisjoint(second_ids)
    secret = client.get(reverse("public:index"), {"q": "Segredo fictício"})
    assert list(secret.context["page_obj"]) == []
    assert client.get(reverse("public:index"), {"q": "x" * 101}).status_code == 400
    assert client.get(reverse("public:index"), {"q": "x" * 100}).status_code == 200


def test_graph_is_bounded_and_reports_truncation(client, catalog, reviewer):
    publish_relationship(catalog.relation, reviewer)
    for number in range(100):
        endpoint = Entity.objects.create(
            name=f"Organização fictícia {number:03}",
            slug=f"organizacao-ficticia-{number:03}",
            kind="organisation",
            is_public=True,
        )
        relationship = Relationship.objects.create(
            subject=catalog.person,
            object=endpoint,
            kind="membership",
            description=f"Ligação fictícia {number:03}",
        )
        Evidence.objects.create(
            relationship=relationship,
            source=catalog.source,
            excerpt="Exemplo fictício de prova.",
            is_public=True,
        )
        publish_relationship(relationship, reviewer)
    graph = client.get(profile_url(catalog.person, "graph")).json()
    assert len(graph["edges"]) == 100
    assert graph["truncated"] is True
    assert len({edge["data"]["id"] for edge in graph["edges"]}) == 100
    assert len(graph["nodes"]) <= 101
    # The rendered alternative must be bounded too, not just the JSON endpoint.
    profile = client.get(profile_url(catalog.person))
    assert len(profile.context["relationships"]) == 100
