import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.urls import path, reverse

from ligacoes.core.models import Entity, SourceIdentity

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
