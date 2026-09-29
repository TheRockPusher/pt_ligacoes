from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ligacoes.core.models import (
    Entity,
    Event,
    IdentitySuggestion,
    Relationship,
    ReviewEvent,
    SourceIdentity,
    SourceObservation,
    Term,
)

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)


def entity(kind: str, slug: str, **fields) -> Entity:
    return Entity.objects.create(name=f"Entidade fictícia {slug}", slug=slug, kind=kind, **fields)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("person", ""),
        ("company", "company"),
        ("university", "higher_education"),
        ("organisation", "other"),
    ],
)
def test_classification_defaults_from_kind(kind, expected):
    created = entity(kind, f"padrao-{kind}")
    created.refresh_from_db()
    assert created.classification == expected


@pytest.mark.parametrize(
    ("kind", "classification"),
    [("company", "state_company"), ("organisation", "regulator"), ("organisation", "parliament")],
)
def test_explicit_compatible_classification_is_kept(kind, classification):
    created = entity(kind, f"valida-{classification}", classification=classification)
    created.refresh_from_db()
    assert created.classification == classification


@pytest.mark.parametrize(
    ("kind", "classification"),
    [
        ("person", "company"),
        ("company", "higher_education"),
        ("company", "public_body"),
        ("university", "company"),
        ("organisation", "company"),
        ("organisation", "state_company"),
        ("organisation", "higher_education"),
    ],
)
def test_incompatible_kind_and_classification_are_rejected(kind, classification):
    with pytest.raises(ValidationError):
        entity(kind, "incompativel", classification=classification)
    assert not Entity.objects.filter(slug="incompativel").exists()


def test_database_rejects_unclassified_organisation_from_bulk_create():
    with pytest.raises(IntegrityError), transaction.atomic():
        Entity.objects.bulk_create(
            [Entity(name="Organização fictícia em massa", slug="em-massa", kind="organisation")]
        )


def test_editor_reclassification_invalidates_published_claims(published):
    published.company.classification = "state_company"
    published.company.save()
    published.relation.refresh_from_db()
    assert published.relation.status == "draft"


@pytest.mark.parametrize(
    ("kind", "subject_kind", "object_kind"),
    [
        ("part_of", "person", "organisation"),
        ("succession", "organisation", "person"),
        ("family", "person", "organisation"),
        ("family", "organisation", "organisation"),
        ("membership", "person", "person"),
        ("shareholding", "company", "person"),
        ("employment", "organisation", "company"),
        ("public_office", "person", "person"),
    ],
)
def test_kind_matrix_rejects_invalid_endpoints(kind, subject_kind, object_kind):
    subject = entity(subject_kind, "origem")
    target = entity(object_kind, "destino")
    with pytest.raises(ValidationError):
        Relationship.objects.create(subject=subject, object=target, kind=kind)
    assert not Relationship.objects.exists()


@pytest.mark.parametrize(
    ("kind", "subject_kind", "object_kind"),
    [
        ("part_of", "organisation", "organisation"),
        ("succession", "company", "company"),
        ("family", "person", "person"),
        ("membership", "organisation", "organisation"),
        ("shareholding", "company", "company"),
        ("directorship", "person", "university"),
    ],
)
def test_kind_matrix_accepts_valid_endpoints(kind, subject_kind, object_kind):
    subject = entity(subject_kind, "origem")
    target = entity(object_kind, "destino")
    Relationship.objects.create(subject=subject, object=target, kind=kind)
    assert Relationship.objects.filter(kind=kind).exists()


@pytest.mark.parametrize(
    ("fields", "valid"),
    [
        ({"start_date": date(2020, 1, 1), "start_precision": "year"}, True),
        ({"start_date": date(2020, 1, 2), "start_precision": "year"}, False),
        ({"start_date": date(2020, 3, 1), "start_precision": "month"}, True),
        ({"start_date": date(2020, 3, 31), "start_precision": "month"}, False),
        ({"end_date": date(2020, 12, 31), "end_precision": "year"}, True),
        ({"end_date": date(2020, 1, 1), "end_precision": "year"}, False),
        ({"end_date": date(2024, 2, 29), "end_precision": "month"}, True),
        ({"end_date": date(2024, 2, 28), "end_precision": "month"}, False),
        ({"end_date": date(2024, 2, 28), "end_precision": "day"}, True),
    ],
)
def test_date_precision_requires_normalised_boundaries(catalog, fields, valid):
    for name, value in fields.items():
        setattr(catalog.relation, name, value)
    if valid:
        catalog.relation.full_clean()
    else:
        with pytest.raises(ValidationError):
            catalog.relation.full_clean()


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", "Vogal fictício"), ("temporal_status", "ended"), ("term", "term")],
)
def test_editing_role_term_or_status_invalidates_publication(published, field, value):
    if value == "term":
        value = Term.objects.create(
            kind="legislature",
            code="XFIC",
            label="Legislatura fictícia",
            institution=published.company,
            start_date=date(2020, 1, 1),
        )
    setattr(published.relation, field, value)
    published.relation.save()
    published.relation.refresh_from_db()
    assert published.relation.status == "draft"
    assert published.relation.reviewed_at is None
    assert ReviewEvent.objects.filter(relationship=published.relation, action="invalidate").exists()


@pytest.mark.parametrize(
    ("scheme", "kind", "valid"),
    [
        ("nipc", "person", False),
        ("sioe", "person", False),
        ("lei", "person", False),
        ("eu_tr", "person", False),
        ("nipc", "company", True),
        ("parliament", "organisation", True),
        ("government", "company", True),
        ("ept", "person", True),
        ("wikidata", "person", True),
    ],
)
def test_identity_scheme_kind_rules(scheme, kind, valid):
    identified = entity(kind, "identificada")
    # A valid legal-person NIPC, so only the scheme/kind rule is under test.
    identity = SourceIdentity(source=scheme, external_id="500000000", entity=identified)
    if valid:
        identity.save()
        assert SourceIdentity.objects.filter(source=scheme).exists()
    else:
        with pytest.raises(ValidationError):
            identity.save()
        assert not SourceIdentity.objects.exists()


def observation(**fields) -> SourceObservation:
    values = {
        "source": "sioe",
        "scope": "organismo:ficticio",
        "external_id": "cargo-1",
        "revision": "revisao-a",
        "category": "office_holding",
        "passage": "Vogal fictício do conselho diretivo de um organismo fictício.",
        "source_url": "https://example.org/sioe-ficticio",
        "publisher": "Editor fictício",
        "reference": "Organismo fictício / cargo 1",
        "title": "Registo inteiramente fictício",
        "as_of": DAY,
    }
    values.update(fields)
    return SourceObservation(**values)


def test_observation_without_identity_requires_published_subject_name():
    with pytest.raises(ValidationError):
        observation(identity=None, subject_name=" ").save()
    observation(identity=None, subject_name="Pessoa Fictícia Sem Identificador").save()
    assert SourceObservation.objects.get().identity_id is None


def test_observation_rejects_hint_only_identity():
    person = entity("person", "pista")
    hint = SourceIdentity.objects.create(source="wikidata", external_id="Q0", entity=person)
    with pytest.raises(ValidationError):
        observation(identity=hint).save()
    anchor = SourceIdentity.objects.create(source="ept", external_id="holder:1", entity=person)
    observation(identity=anchor).save()
    assert SourceObservation.objects.get().identity_id == anchor.pk


@pytest.mark.parametrize(
    ("category", "kind", "valid"),
    [
        ("organisation_structure", "organisation", True),
        ("organisation_structure", "person", False),
        ("office_holding", "organisation", False),
    ],
)
def test_observation_subject_kind_follows_category(category, kind, valid):
    subject = entity(kind, "sujeito")
    identity = SourceIdentity.objects.create(source="parliament", external_id="1", entity=subject)
    candidate = observation(identity=identity, category=category)
    if valid:
        candidate.save()
    else:
        with pytest.raises(ValidationError):
            candidate.save()


def event(source, **fields) -> Event:
    values = {
        "dataset": "base_contratos",
        "scope": "2026",
        "record_id": "contrato-1",
        "kind": "contract",
        "title": "Contrato inteiramente fictício",
        "source": source,
        "fingerprint": "0" * 64,
        "as_of": DAY,
        "retrieved_at": timezone.now(),
    }
    values.update(fields)
    return Event.objects.create(**values)


def test_event_amount_cannot_be_negative(catalog):
    with pytest.raises(IntegrityError), transaction.atomic():
        event(catalog.source, amount=Decimal("-0.01"))
    event(catalog.source, amount=Decimal("0.00"))
    assert Event.objects.count() == 1


def test_event_record_is_unique_per_dataset(catalog):
    event(catalog.source)
    with pytest.raises(IntegrityError), transaction.atomic():
        event(catalog.source)
    event(catalog.source, dataset="igf_subvencoes")
    assert Event.objects.count() == 2


def test_withdrawn_event_requires_withdrawal_time(catalog):
    with pytest.raises(IntegrityError), transaction.atomic():
        event(catalog.source, status="withdrawn")
    with pytest.raises(IntegrityError), transaction.atomic():
        event(catalog.source, status="published", withdrawn_at=timezone.now())


def test_identity_suggestion_is_unique_per_candidate(catalog):
    fields = {
        "scheme": "parliament",
        "external_id": "91001",
        "name_as_published": catalog.person.name,
        "basis": "Nome publicado igual ao de uma pessoa fictícia.",
    }
    IdentitySuggestion.objects.create(candidate=catalog.person, **fields)
    with pytest.raises(IntegrityError), transaction.atomic():
        IdentitySuggestion.objects.create(candidate=catalog.person, **fields)
    other = entity("person", "homonima")
    IdentitySuggestion.objects.create(candidate=other, **fields)
    assert IdentitySuggestion.objects.filter(status="pending").count() == 2
