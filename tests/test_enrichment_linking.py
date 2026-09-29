from dataclasses import replace
from datetime import date, timedelta

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.urls import include, path, reverse
from django.utils import timezone

from ligacoes.core.enrichment import (
    ObservationInput,
    convert_observation,
    sync_observations,
    sync_scoped_snapshot,
)
from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Relationship,
    SourceIdentity,
    SourceObservation,
    SourceSyncState,
    Term,
)
from ligacoes.public.selectors import public_relationships

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
urlpatterns = [path("admin/", admin.site.urls), path("", include("ligacoes.public.urls"))]


@pytest.fixture(autouse=True)
def admin_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def editor(django_user_model):
    user = django_user_model.objects.create_user(username="fictional-linking-editor", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="review_sourceobservation"))
    return user


@pytest.fixture
def body():
    return official_entity(
        "nipc",
        "601234561",
        name="Instituto Público Fictício",
        kind="organisation",
        classification="public_body",
    )


@pytest.fixture
def holder():
    person = Entity.objects.create(
        name="Titular Fictícia", slug="titular-ficticia", kind="person", is_public=True
    )
    return SourceIdentity.objects.create(
        source="ept", external_id="holder:fictional-1", entity=person
    )


def office(identity, target, **changes):
    options = {
        "external_id": "office:1",
        "revision": "revision-a",
        "category": "office_holding",
        "passage": "Vogal do conselho diretivo de um instituto fictício.",
        "source_url": "https://example.org/titulares/1",
        "publisher": "Editor fictício",
        "reference": "Registo fictício 1",
        "title": "Titulares inteiramente fictícios",
        "identity": identity,
        "object": target,
        "kind": "public_office",
        "dataset": "ept_titulares",
    }
    options.update(changes)
    return ObservationInput(**options)


def sync(items, *, source="ept", scope="holder:fictional-1", day=DAY):
    return sync_observations(source=source, scope=scope, observations=tuple(items), as_of=day)


def test_office_holding_with_anchored_object_publishes_itself(holder, body):
    result = sync([office(holder, body)])
    assert (result["drafts"], result["published"]) == (1, 1)
    observation = SourceObservation.objects.get()
    relationship = observation.relationship
    assert relationship is not None
    assert relationship.status == "published" and relationship.reviewed_by is None
    assert public_relationships().get().pk == relationship.pk
    assert observation.evidence is not None
    assert observation.evidence.source.dataset == "ept_titulares"
    assert SourceIdentity.objects.get(source="nipc").used_at is not None


def test_office_holding_with_unanchored_object_stays_a_private_candidate(holder):
    unanchored = Entity.objects.create(
        name="Instituto sem identificador",
        slug="instituto-sem-id",
        kind="organisation",
        is_public=True,
    )
    result = sync([office(holder, unanchored)])
    assert (result["created"], result["drafts"], result["published"]) == (1, 0, 0)
    assert SourceObservation.objects.get().relationship_id is None
    assert not Relationship.objects.exists()


def test_declared_interest_is_never_auto_published(django_user_model, holder, body):
    reviewer = django_user_model.objects.create_user(username="fictional-identity-reviewer")
    holder.reviewed_by = reviewer
    holder.reviewed_at = timezone.now()
    holder.review_notes = "Identificador fictício conferido com o cargo."
    holder.save()
    result = sync(
        [
            office(
                holder,
                body,
                category="declared_interest",
                kind="professional_activity",
                declared_on=DAY,
            )
        ]
    )
    assert (result["drafts"], result["published"]) == (0, 0)
    assert not Relationship.objects.exists()


def name_only(target):
    return office(
        None,
        target,
        external_id="cargo:77",
        subject_name="Vogal Fictício Sem Identificador",
        subject_reference="sioe:cargo:77",
        kind="directorship",
        dataset="sioe",
    )


def test_name_only_candidate_stays_private_and_converts_only_with_a_chosen_person(editor, body):
    result = sync([name_only(body)], source="sioe", scope="entity:601234561")
    assert (result["created"], result["drafts"]) == (1, 0)
    candidate = SourceObservation.objects.get()
    assert candidate.identity_id is None and candidate.relationship_id is None
    options = {
        "object": body,
        "kind": "directorship",
        "description": "Vogal do conselho diretivo, conforme o registo oficial fictício.",
        "review_notes": "Pessoa verificada pelo despacho de nomeação fictício, não pelo nome.",
    }
    with pytest.raises(ValidationError):
        convert_observation(candidate, editor, **options)
    with pytest.raises(ValidationError):
        convert_observation(candidate, editor, subject=body, **options)
    assert not Relationship.objects.exists()
    chosen = Entity.objects.create(
        name="Pessoa Verificada Fictícia", slug="pessoa-verificada", kind="person"
    )
    relationship = convert_observation(candidate, editor, subject=chosen, **options)
    candidate.refresh_from_db()
    assert relationship.subject_id == chosen.pk and relationship.status == "draft"
    assert candidate.identity_id is None and candidate.relationship_id == relationship.pk
    assert not public_relationships().exists()


def test_admin_review_asks_for_a_person_only_without_an_official_identity(
    client, editor, body, holder
):
    sync([name_only(body)], source="sioe", scope="entity:601234561")
    candidate = SourceObservation.objects.get()
    url = reverse("admin:core_sourceobservation_change", args=[candidate.pk])
    client.force_login(editor)
    response = client.get(url)
    assert response.status_code == 200
    assert "reviewed_subject" in response.context["adminform"].form.fields
    picker = reverse("admin:core_sourceobservation_subject_picker")
    params = {"app_label": "core", "model_name": "sourceobservation", "field_name": "object"}
    found = client.get(picker, {**params, "term": "Fictíci"}).json()["results"]
    assert found == [{"id": str(holder.entity_id), "text": holder.entity.name}]
    payload = {
        "reviewed_object": str(body.pk),
        "reviewed_kind": "directorship",
        "reviewed_start": "",
        "reviewed_end": "",
        "reviewed_description": "Vogal do conselho diretivo fictício.",
        "conversion_notes": "Pessoa confirmada no despacho fictício de nomeação.",
        "_save": "Guardar",
    }
    response = client.post(url, payload)
    assert response.status_code == 200
    assert "reviewed_subject" in response.context["adminform"].form.errors
    response = client.post(url, {**payload, "reviewed_subject": str(holder.entity_id)})
    assert response.status_code == 302
    candidate.refresh_from_db()
    assert candidate.relationship is not None
    assert candidate.relationship.subject_id == holder.entity_id
    sync([office(holder, body)])
    identified = SourceObservation.objects.get(source="ept")
    response = client.get(reverse("admin:core_sourceobservation_change", args=[identified.pk]))
    assert "reviewed_subject" not in response.context["adminform"].form.fields


def test_scoped_snapshot_ceases_scopes_absent_from_a_complete_run(holder, body):
    first = office(holder, body)
    second = replace(first, external_id="office:2", passage="Presidente de um órgão fictício.")
    result = sync_scoped_snapshot(
        source="ept",
        prefix="holder:",
        snapshots={"holder:a": (first,), "holder:b": (second,)},
        as_of=DAY,
    )
    assert (result["created"], result["published"]) == (2, 2)
    result = sync_scoped_snapshot(
        source="ept",
        prefix="holder:",
        snapshots={"holder:a": (first,)},
        as_of=DAY + timedelta(days=1),
    )
    assert result["ceased"] == 1
    ceased = SourceObservation.objects.get(scope="holder:b")
    assert not ceased.is_current
    assert ceased.relationship is not None and ceased.relationship.status == "draft"
    assert SourceObservation.objects.get(scope="holder:a").is_current
    assert SourceSyncState.objects.get(scope="holder:b").as_of == DAY + timedelta(days=1)
    with pytest.raises(ValidationError):
        sync_scoped_snapshot(
            source="ept",
            prefix="holder:",
            snapshots={"entity:a": (first,)},
            as_of=DAY + timedelta(days=2),
        )


def test_role_term_precision_and_temporal_status_are_copied_to_the_draft(holder, body):
    term = Term.objects.create(
        kind="legislature",
        code="FIC1",
        label="Legislatura fictícia",
        institution=body,
        start_date=date(2024, 1, 1),
    )
    sync(
        [
            office(
                holder,
                body,
                role="Vogal do conselho diretivo",
                role_class="member",
                term=term,
                effective_start=date(2024, 3, 1),
                start_precision="month",
                effective_end=date(2025, 12, 31),
                end_precision="year",
                temporal_status="ended",
            )
        ]
    )
    relationship = Relationship.objects.get()
    assert (relationship.role, relationship.role_class, relationship.term_id) == (
        "Vogal do conselho diretivo",
        "member",
        term.pk,
    )
    assert (relationship.start_date, relationship.start_precision) == (date(2024, 3, 1), "month")
    assert (relationship.end_date, relationship.end_precision) == (date(2025, 12, 31), "year")
    assert relationship.temporal_status == "ended" and relationship.status == "published"
