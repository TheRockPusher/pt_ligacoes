from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from ligacoes.core.identity import record_alias
from ligacoes.core.models import (
    Entity,
    EntityRedirect,
    Evidence,
    Relationship,
    Source,
    SourceIdentity,
)
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
    listed = client.get(reverse("public:index")).context["profiles"]
    assert {item.entity.pk: item.total for item in listed}[published.person.pk] == 1
    # Deliberately bypass editorial invalidation: every read must enforce its
    # own publication boundary even after an external import or SQL update.
    type(record).objects.filter(pk=record.pk).update(is_public=False)
    assert public_relationships().count() == 0
    assert client.get(evidence_url(published.evidence)).status_code == 404
    response = client.get(profile_url(published.person))
    graph = client.get(profile_url(published.person, "graph"))
    listed = client.get(reverse("public:index")).context["profiles"]
    # Counts are a public surface too: a withdrawn claim must not be revealed by them.
    assert all(item.total == 0 for item in listed)
    if hidden == "person":
        assert response.status_code == 404
        assert graph.status_code == 404
        index = client.get(reverse("public:index"))
        assert published.person.name not in index.content.decode()
    else:
        assert response.status_code == 200
        assert published.relation.description not in response.content.decode()
        assert response.context["summary"].total == 0
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
    # An allow-list: any new field must be a deliberate public contract change.
    assert set(graph["edges"][0]["data"]) == {
        "id",
        "source",
        "target",
        "label",
        "kind",
        "role",
        "term",
        "start",
        "end",
        "start_precision",
        "end_precision",
        "temporal_status",
        "declared",
        "url",
    }
    assert all(
        set(node["data"])
        == {"id", "label", "kind", "classification", "classification_label", "url", "connections"}
        for node in graph["nodes"]
    )


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


@pytest.mark.parametrize("extreme", ["0001-01-01", "1600-01-01", "9999-12-31"])
def test_extreme_valid_dates_render_the_timeline(client, catalog, reviewer, extreme):
    # An undocumented bound keeps a dated claim visible at any date, stretching the time axis.
    catalog.relation.start_date = date(2019, 1, 1) if extreme > "2019" else None
    catalog.relation.end_date = date(2019, 1, 1) if extreme < "2019" else None
    catalog.relation.save()
    publish_relationship(catalog.relation, reviewer)
    for route in ["entity_detail", "graph"]:
        assert client.get(profile_url(catalog.person, route), {"at": extreme}).status_code == 200
    profile = client.get(profile_url(catalog.person), {"at": extreme})
    assert profile.context["axis"] is not None
    assert profile.context["relationships"] == [catalog.relation]


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
    people = client.get(reverse("public:index"), {"q": "fictíci", "tipo": "person"})
    assert [item.pk for item in people.context["page_obj"]] == [catalog.person.pk]
    assert client.get(reverse("public:index"), {"tipo": "party"}).status_code == 400


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
    # The rendered alternative must be bounded too, not just the JSON endpoint,
    # while every published connection stays reachable through pagination.
    first = client.get(profile_url(catalog.person)).context["relationships"]
    second = client.get(profile_url(catalog.person), {"page": 2}).context["relationships"]
    assert len(first) == 100
    assert len(second) == 1
    assert {item.pk for item in first}.isdisjoint(item.pk for item in second)
    assert {edge["data"]["id"] for edge in graph["edges"]} == {str(item.pk) for item in first}
    search = {"q": "Organização fictícia 042"}
    found = client.get(profile_url(catalog.person), search).context["relationships"]
    assert [item.object_id for item in found] == [
        Entity.objects.get(slug="organizacao-ficticia-042").pk
    ]
    # The map beside a searched list must draw the same subset, not the whole profile.
    searched_graph = client.get(profile_url(catalog.person, "graph"), search).json()
    assert [edge["data"]["id"] for edge in searched_graph["edges"]] == [str(found[0].pk)]


def publish(reviewer, subject, object_, source, **fields):
    relationship = Relationship.objects.create(
        subject=subject, object=object_, description="Ligação fictícia.", **fields
    )
    Evidence.objects.create(
        relationship=relationship, source=source, excerpt="Prova fictícia.", is_public=True
    )
    return publish_relationship(relationship, reviewer)


def test_search_ignores_accents_and_case_and_matches_every_token_or_an_alias(client, published):
    record_alias(published.person, "Alfa Conceição Fictícia", scheme="ept", external_id="x1")
    index = reverse("public:index")

    def found(query):
        profiles = client.get(index, {"q": query}).context["profiles"]
        return [listing.entity.pk for listing in profiles]

    person, company = published.person.pk, published.company.pk
    assert found("PESSOA alfa") == [person]
    assert found("conceicao") == [person]
    assert found("ficticia") == [company, person]
    # Every token must match one entity; a token matches the start of a word only.
    assert found("alfa companhia") == []
    assert found("lfa") == []
    listing = client.get(index, {"q": "alfa conceição"}).context["profiles"][0]
    assert listing.alias == "Alfa Conceição Fictícia"
    profile = client.get(profile_url(published.person)).content.decode()
    assert "Alfa Conceição Fictícia" in profile
    options = client.get(reverse("public:path_entities"), {"q": "Conceição"}).context["options"]
    assert [(option.entity.pk, option.alias) for option in options] == [
        (person, "Alfa Conceição Fictícia")
    ]
    # A profile's own search matches counterparts by alias as well.
    searched = client.get(profile_url(published.company), {"q": "conceicao"})
    assert searched.context["relationships"] == [published.relation]


def test_merged_slugs_redirect_permanently_to_a_public_target_only(client, published):
    EntityRedirect.objects.create(old_slug="pessoa-alfa-antiga", entity=published.person)
    hidden = Entity.objects.create(name="Oculta fictícia", slug="oculta-ficticia", kind="person")
    EntityRedirect.objects.create(old_slug="aponta-para-oculta", entity=hidden)
    # A live public profile keeps its own slug even if a redirect names it.
    EntityRedirect.objects.create(old_slug=published.company.slug, entity=published.person)
    person, company = published.person.slug, published.company.slug

    for route in ["entity_detail", "graph", "entity_events"]:
        old = reverse(f"public:{route}", kwargs={"slug": "pessoa-alfa-antiga"})
        response = client.get(old, {"at": "2025-01-01"})
        assert response.status_code == 301
        new = reverse(f"public:{route}", kwargs={"slug": person})
        assert response["Location"] == f"{new}?at=2025-01-01"
        hidden_url = reverse(f"public:{route}", kwargs={"slug": "aponta-para-oculta"})
        assert client.get(hidden_url).status_code == 404
        live = reverse(f"public:{route}", kwargs={"slug": company})
        assert client.get(live).status_code == 200

    events = client.get(
        reverse("public:entity_events", kwargs={"slug": company}), {"com": "pessoa-alfa-antiga"}
    )
    assert events.status_code == 301
    assert f"com={person}" in events["Location"]
    finder = client.get(
        reverse("public:path_finder"), {"de": "pessoa-alfa-antiga", "para": company}
    )
    assert finder.status_code == 301
    assert f"de={person}" in finder["Location"] and f"para={company}" in finder["Location"]
    hidden_finder = client.get(reverse("public:path_finder"), {"de": "aponta-para-oculta"})
    assert hidden_finder.status_code == 200
    assert hidden.name not in hidden_finder.content.decode()
    assert "oculta-ficticia" not in hidden_finder.content.decode()


@pytest.mark.parametrize("query", ["...", "Dr.", "—"])
def test_queries_without_searchable_words_find_nothing_without_failing(client, published, query):
    directory = client.get(reverse("public:index"), {"q": query})
    assert directory.status_code == 200
    assert list(directory.context["page_obj"]) == []
    picker = client.get(reverse("public:path_entities"), {"q": query})
    assert picker.status_code == 200
    assert picker.context["options"] == []
    finder = client.get(reverse("public:path_finder"), {"de_q": query, "para_q": query})
    assert finder.status_code == 200
    profile = client.get(profile_url(published.person), {"q": query})
    assert profile.status_code == 200
    assert profile.context["relationships"] == []


def test_name_only_identities_are_labelled_without_implying_verification(client, catalog):
    SourceIdentity.objects.create(
        source="declared_name", external_id="beta", entity=catalog.company
    )
    SourceIdentity.objects.create(source="scoped_name", external_id="alfa", entity=catalog.person)
    anchored = Entity.objects.create(
        name="Cooperativa Gama — fictícia",
        slug="cooperativa-gama",
        kind="company",
        is_public=True,
    )
    SourceIdentity.objects.create(source="declared_name", external_id="gama", entity=anchored)
    SourceIdentity.objects.create(source="nipc", external_id="500000000", entity=anchored)

    company = client.get(profile_url(catalog.company))
    declared_label = "Sem identificador oficial — nome como declarado na fonte"
    assert company.context["provenance"] == declared_label
    assert company.context["identifiers"] == []
    person = client.get(profile_url(catalog.person))
    assert person.context["provenance"] == "Identificada apenas pelo nome publicado na fonte"
    assert client.get(profile_url(anchored)).context["provenance"] == ""
    listings = client.get(reverse("public:index"), {"q": "fictícia"}).context["profiles"]
    assert {listing.entity.pk: bool(listing.provenance) for listing in listings} == {
        catalog.company.pk: True,
        catalog.person.pk: True,
        anchored.pk: False,
    }


def test_declared_interests_are_grouped_dated_and_filterable(client, published, reviewer):
    declaration = Source.objects.create(
        title="Declaração de interesses fictícia",
        url="https://example.org/declaracao-ficticia",
        dataset="ept_declaracoes",
        published_at=datetime(2024, 5, 20, 12, tzinfo=UTC),
        is_public=True,
    )
    casino = Entity.objects.create(
        name="Casino Delta — fictício", slug="casino-delta", kind="company", is_public=True
    )
    clinic = Entity.objects.create(
        name="Clínica Épsilon — fictícia",
        slug="clinica-epsilon",
        kind="company",
        is_public=True,
    )
    declared_client = publish(
        reviewer, published.person, casino, declaration, kind="declared_client"
    )
    board = publish(reviewer, published.person, clinic, declaration, kind="directorship")

    response = client.get(profile_url(published.person))
    assert response.context["summary"].declared == 2
    groups = [(group.kind, group.declared, group.total) for group in response.context["groups"]]
    # Officially documented connections first, then the declared ones, each by kind.
    assert groups == [
        ("employment", False, 1),
        ("directorship", True, 1),
        ("declared_client", True, 1),
    ]
    body = response.content.decode()
    assert "Declarado pela própria pessoa" in body
    assert f"{published.person.name} declarou {casino.name} como cliente" in body
    assert body.count("Declaração de interesses de") == 2
    assert "20/05/2024" in body

    only_declared = client.get(profile_url(published.person), {"declarado": "1"})
    assert {item.pk for item in only_declared.context["relationships"]} == {
        declared_client.pk,
        board.pk,
    }
    by_kind = client.get(profile_url(published.person), {"tipo": "declared_client"})
    assert by_kind.context["relationships"] == [declared_client]
    assert client.get(profile_url(published.person), {"tipo": "party"}).status_code == 400
    assert client.get(profile_url(published.person), {"declarado": "sim"}).status_code == 400
    # The counterpart's profile reads the same claim as declared by the person.
    casino_body = client.get(profile_url(casino)).content.decode()
    assert "Declarado pelas pessoas ligadas" in casino_body
    evidence = client.get(evidence_url(declared_client.evidence.get())).content.decode()
    assert "Consta da declaração de interesses de" in evidence


def test_current_offices_read_em_curso(client, published, reviewer):
    assembly = Entity.objects.create(
        name="Assembleia fictícia", slug="assembleia-ficticia", kind="organisation", is_public=True
    )
    office = publish(
        reviewer,
        published.person,
        assembly,
        published.source,
        kind="public_office",
        role="Deputada fictícia",
        start_date=date(2022, 3, 29),
        temporal_status="current",
    )
    publish(
        reviewer,
        published.person,
        published.company,
        published.source,
        kind="public_office",
        role="Cargo terminado fictício",
        start_date=date(2019, 1, 1),
        end_date=date(2020, 1, 1),
        temporal_status="ended",
    )

    response = client.get(profile_url(published.person))
    assert response.context["current_offices"] == [office]
    period = next(
        relationship for relationship in response.context["relationships"] if relationship == office
    )
    assert period.span.ongoing and not period.span.open_end
    body = response.content.decode()
    assert "Cargos públicos em curso" in body
    assert '<span class="period-status" data-status="current">Em curso</span>' in body
