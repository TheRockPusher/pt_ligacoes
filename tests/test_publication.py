from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from ligacoes.core.models import Evidence, Relationship, ReviewEvent
from ligacoes.core.services import publish_relationship
from ligacoes.public.selectors import public_relationships

pytestmark = pytest.mark.django_db


def test_publication_requires_explicit_permission(catalog, django_user_model):
    unprivileged = django_user_model.objects.create_user(username="fictional-reader")
    with pytest.raises(PermissionDenied):
        publish_relationship(catalog.relation, unprivileged)
    catalog.relation.refresh_from_db()
    assert catalog.relation.status == "draft"
    assert catalog.relation.reviewed_by_id is None
    assert not ReviewEvent.objects.filter(relationship=catalog.relation).exists()


def test_inactive_reviewer_cannot_publish(catalog, reviewer):
    reviewer.is_active = False
    reviewer.save(update_fields=["is_active"])
    with pytest.raises(PermissionDenied):
        publish_relationship(catalog.relation, reviewer)
    assert not public_relationships().exists()


def test_revoked_permission_cannot_survive_a_cached_user(catalog, reviewer):
    assert reviewer.has_perm("core.publish_relationship")
    reviewer.user_permissions.clear()
    with pytest.raises(PermissionDenied):
        publish_relationship(catalog.relation, reviewer)
    assert not public_relationships().exists()
    assert not ReviewEvent.objects.filter(relationship=catalog.relation).exists()


@pytest.mark.parametrize("hidden", ["person", "company", "source", "evidence"])
def test_publication_requires_public_endpoints_and_evidence(catalog, reviewer, hidden):
    record = getattr(catalog, hidden)
    record.is_public = False
    record.save()
    with pytest.raises(ValidationError):
        publish_relationship(catalog.relation, reviewer)
    catalog.relation.refresh_from_db()
    assert catalog.relation.status == "draft"
    assert not ReviewEvent.objects.filter(relationship=catalog.relation).exists()


def test_missing_evidence_cannot_publish(catalog, reviewer):
    catalog.evidence.delete()
    with pytest.raises(ValidationError):
        publish_relationship(catalog.relation, reviewer)
    assert not public_relationships().exists()


def test_successful_publication_attributes_and_audits_decision(catalog, reviewer):
    relationship = publish_relationship(catalog.relation, reviewer)
    relationship.refresh_from_db()
    assert relationship.status == "published"
    assert relationship.reviewed_by_id == reviewer.pk
    assert relationship.reviewed_at is not None
    event = ReviewEvent.objects.get(relationship=relationship, action="publish")
    assert event.reviewer_id == reviewer.pk
    assert list(public_relationships().values_list("pk", flat=True)) == [relationship.pk]


def test_direct_status_assignment_cannot_bypass_review(catalog):
    catalog.relation.status = "published"
    with pytest.raises(ValidationError):
        catalog.relation.save()
    catalog.relation.refresh_from_db()
    assert catalog.relation.status == "draft"


def test_editing_public_claim_requires_fresh_review(published, reviewer):
    published.relation.description = "Descrição corrigida da relação fictícia."
    published.relation.save()
    published.relation.refresh_from_db()
    assert published.relation.status == "draft"
    assert published.relation.reviewed_by_id is None
    assert published.relation.reviewed_at is None
    assert not public_relationships().exists()
    assert ReviewEvent.objects.filter(relationship=published.relation, action="invalidate").exists()
    republished = publish_relationship(published.relation, reviewer)
    assert public_relationships().filter(pk=republished.pk).exists()


@pytest.mark.parametrize(
    ("record_name", "field", "value"),
    [
        ("person", "name", "Pessoa Alfa corrigida — personagem fictícia"),
        ("source", "url", "https://example.org/documento-ficticio-corrigido"),
        ("evidence", "excerpt", "Passagem documental fictícia corrigida."),
    ],
)
def test_editorial_dependency_change_invalidates_review(published, record_name, field, value):
    record = getattr(published, record_name)
    setattr(record, field, value)
    record.save()
    published.relation.refresh_from_db()
    assert published.relation.status == "draft"
    assert published.relation.reviewed_at is None
    assert not public_relationships().exists()


def test_deleting_last_evidence_withdraws_publication(published):
    published.evidence.delete()
    published.relation.refresh_from_db()
    assert published.relation.status == "draft"
    assert not public_relationships().exists()


@pytest.mark.parametrize("invalid", ["self", "reversed_dates"])
def test_invalid_claims_rejected_before_publication(catalog, invalid):
    if invalid == "self":
        catalog.relation.object = catalog.person
    else:
        catalog.relation.start_date = date(2025, 2, 1)
        catalog.relation.end_date = date(2025, 1, 1)
    with pytest.raises(ValidationError):
        catalog.relation.full_clean()
    assert not public_relationships().exists()


def test_private_evidence_is_neither_required_nor_made_public(catalog, reviewer):
    private = Evidence.objects.create(
        relationship=catalog.relation,
        source=catalog.source,
        excerpt="PRIVATE_EVIDENCE_CANARY",
        is_public=False,
    )
    relationship = publish_relationship(catalog.relation, reviewer)
    visible = public_relationships().get(pk=relationship.pk)
    assert {item.pk for item in visible.public_evidence} == {catalog.evidence.pk}
    private.refresh_from_db()
    assert private.is_public is False


def test_database_rejects_published_claim_without_review_metadata(catalog):
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.filter(pk=catalog.relation.pk).update(status="published")
    assert not public_relationships().exists()


def test_public_evidence_collection_has_a_per_relationship_limit(catalog, reviewer):
    for number in range(12):
        Evidence.objects.create(
            relationship=catalog.relation,
            source=catalog.source,
            excerpt=f"Passagem fictícia adicional {number:02}.",
            is_public=True,
        )
    publish_relationship(catalog.relation, reviewer)
    visible = public_relationships().get(pk=catalog.relation.pk)
    assert len(visible.public_evidence) == 10
    assert all(item.is_public and item.source.is_public for item in visible.public_evidence)
