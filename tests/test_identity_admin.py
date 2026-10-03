import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.urls import path, reverse
from django.utils import timezone

from ligacoes.core.identity import resolve_person
from ligacoes.core.models import Entity, EntityAlias, IdentitySuggestion, SourceIdentity

pytestmark = pytest.mark.django_db
urlpatterns = [path("admin/", admin.site.urls)]
LEGAL = "501234560"
PERSONAL = ["101234562", "201234564", "301234566", "451234561", "701234563", "741234564"]
PERSONAL += ["751234567", "801234565"]


@pytest.fixture(autouse=True)
def admin_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def editor(django_user_model):
    user = django_user_model.objects.create_user(username="fictional-id-editor", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="review_sourceidentity"))
    return user


def entity(kind, name="Fictícia Lda"):
    return Entity.objects.create(name=name, slug=f"{kind}-{name[:4]}".lower(), kind=kind)


def test_model_rejects_personal_prefixes_and_bad_check_digit():
    company = entity("company")
    for value in [*PERSONAL, "501234561", "60123456"]:
        with pytest.raises(ValidationError) as caught:
            SourceIdentity(source="nipc", external_id=value, entity=company).full_clean()
        assert "external_id" in caught.value.message_dict, value


def test_model_accepts_legal_nipc_on_company():
    SourceIdentity(source="nipc", external_id=LEGAL, entity=entity("company")).full_clean()


def _post(client, source_id, entity_pk, external_id):
    return client.post(
        reverse("admin:core_sourceidentity_add"),
        {
            "source": source_id,
            "external_id": external_id,
            "entity": entity_pk,
            "review_notes": "Confirmado no registo oficial fictício.",
        },
    )


def test_admin_form_rejects_invalid_nipc(client, editor):
    company = entity("company")
    client.force_login(editor)
    for value in [*PERSONAL, "501234561"]:
        response = _post(client, "nipc", company.pk, value)
        assert response.status_code == 200, value
        assert "external_id" in response.context["adminform"].form.errors, value
    assert not SourceIdentity.objects.exists()


def test_admin_form_accepts_company_with_valid_nipc(client, editor):
    company = entity("company")
    client.force_login(editor)
    response = _post(client, "nipc", company.pk, LEGAL)
    assert response.status_code == 302
    assert SourceIdentity.objects.get().entity == company


def test_admin_form_rejects_person_for_organisation_scheme(client, editor):
    person = entity("person", "Beatriz Fictícia")
    client.force_login(editor)
    response = _post(client, "nipc", person.pk, LEGAL)
    assert response.status_code == 200
    assert not SourceIdentity.objects.exists()


def test_picker_offers_company_and_university(client, editor):
    company = entity("company", "Fictícia Empresa")
    university = entity("university", "Fictícia Universidade")
    client.force_login(editor)
    params = {
        "app_label": "core",
        "model_name": "sourceidentity",
        "field_name": "entity",
        "term": "Fictícia",
    }
    found = client.get(reverse("admin:core_sourceidentity_entity_picker"), params).json()
    assert {row["id"] for row in found["results"]} == {str(company.pk), str(university.pk)}


def advisory_suggestion():
    namesake = Entity.objects.create(
        name="Beatriz Fictícia Ramos",
        slug="fictional-existing-person",
        kind="person",
        is_public=True,
    )
    created = resolve_person(
        "ep",
        "fictional-new-person",
        name=namesake.name,
        basis="Nome publicado na fonte fictícia; correspondência exige confirmação.",
    )
    return namesake, created, IdentitySuggestion.objects.get()


def decide(client, suggestion, action):
    return client.post(
        reverse("admin:core_identitysuggestion_changelist"),
        {"action": action, "_selected_action": [str(suggestion.pk)]},
        follow=True,
    )


def test_admin_accepts_advisory_suggestion_for_an_unused_mapping(client, editor):
    namesake, separate, suggestion = advisory_suggestion()
    identity = SourceIdentity.objects.get(source="ep", external_id="fictional-new-person")
    assert identity.entity == separate and identity.used_at is None
    client.force_login(editor)
    response = decide(client, suggestion, "accept_selected")
    assert response.status_code == 200
    identity.refresh_from_db()
    suggestion.refresh_from_db()
    assert identity.entity == namesake and identity.reviewed_by == editor
    assert suggestion.status == "accepted" and suggestion.reviewed_by == editor
    assert Entity.objects.filter(pk=separate.pk).exists()
    assert (
        EntityAlias.objects.get(scheme="ep", external_id="fictional-new-person").entity == namesake
    )
    assert (
        resolve_person(
            "ep",
            "fictional-new-person",
            name=namesake.name,
            basis="Fonte fictícia.",
        )
        == namesake
    )


def test_admin_cannot_redirect_used_advisory_mapping_but_can_reject(client, editor):
    namesake, separate, suggestion = advisory_suggestion()
    identity = SourceIdentity.objects.get(source="ep", external_id="fictional-new-person")
    identity.used_at = timezone.now()
    identity.save()
    client.force_login(editor)
    assert decide(client, suggestion, "accept_selected").status_code == 200
    identity.refresh_from_db()
    suggestion.refresh_from_db()
    assert identity.entity == separate and identity.entity != namesake
    assert suggestion.status == "pending"
    assert decide(client, suggestion, "reject_selected").status_code == 200
    suggestion.refresh_from_db()
    assert suggestion.status == "rejected" and suggestion.reviewed_by == editor


def test_admin_rejects_organisation_for_person_scoped_scheme(client, editor):
    company = entity("company")
    client.force_login(editor)
    response = _post(client, "scoped_name", company.pk, "fictional:scope:name")
    assert response.status_code == 200
    assert "entity" in response.context["adminform"].form.errors
    assert not SourceIdentity.objects.exists()


def test_admin_rejects_person_for_declared_organisation_scheme(client, editor):
    person = entity("person", "Beatriz Fictícia")
    client.force_login(editor)
    response = _post(client, "declared_name", person.pk, "beatriz ficticia")
    assert response.status_code == 200
    assert "entity" in response.context["adminform"].form.errors
    assert not SourceIdentity.objects.exists()
