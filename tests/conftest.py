from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Permission, User

from ligacoes.core.models import Entity, Evidence, Relationship, Source
from ligacoes.core.services import publish_relationship


@pytest.fixture
def reviewer(db, django_user_model: type[User]) -> User:
    user = django_user_model.objects.create_user(username="fictional-reviewer")
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="core", codename="publish_relationship")
    )
    return user


@pytest.fixture
def catalog(db):
    person = Entity.objects.create(
        name="Pessoa Alfa — personagem fictícia",
        slug="pessoa-alfa-ficticia",
        kind="person",
        is_public=True,
        description="Perfil inteiramente fictício para testes.",
        private_notes="PRIVATE_ENTITY_CANARY",
    )
    company = Entity.objects.create(
        name="Companhia Beta — entidade fictícia",
        slug="companhia-beta-ficticia",
        kind="company",
        is_public=True,
    )
    source = Source.objects.create(
        title="Documento de exemplo inteiramente fictício",
        url="https://example.org/documento-ficticio",
        publisher="Arquivo de exemplo fictício",
        retrieved_at="2025-01-15T12:00:00Z",
        is_public=True,
        private_notes="PRIVATE_SOURCE_CANARY",
    )
    relation = Relationship.objects.create(
        subject=person,
        object=company,
        kind="employment",
        description="Vínculo profissional inteiramente fictício.",
        private_notes="PRIVATE_RELATIONSHIP_CANARY",
    )
    evidence = Evidence.objects.create(
        relationship=relation,
        source=source,
        excerpt="A personagem fictícia integrou a companhia fictícia.",
        page_reference="p. 7",
        is_public=True,
    )
    return SimpleNamespace(
        person=person,
        company=company,
        source=source,
        relation=relation,
        evidence=evidence,
    )


@pytest.fixture
def published(catalog, reviewer):
    catalog.relation = publish_relationship(catalog.relation, reviewer)
    return catalog
