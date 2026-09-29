from datetime import date

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError

from ligacoes.core.identity import (
    accept_suggestion,
    ar_institution,
    ar_person,
    normalise_name,
    official_entities_bulk,
    official_entity,
    reject_suggestion,
    resolve_person,
    suggest_person,
    valid_nipc,
)
from ligacoes.core.models import (
    Entity,
    IdentitySuggestion,
    ParliamentImportState,
    ParliamentMember,
    SourceIdentity,
)
from ligacoes.core.parliament_import import apply_snapshot
from ligacoes.core.parliament_parse import JSONObject, MemberRecord, ParliamentSnapshot

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
BASIS = "Nome publicado igual; mandato europeu fictício conferido na fonte oficial."


@pytest.fixture
def identity_reviewer(django_user_model):
    user = django_user_model.objects.create_user(username="fictional-identity-reviewer")
    user.user_permissions.add(Permission.objects.get(codename="review_sourceidentity"))
    return user


def person(name, *, slug, public=True):
    return Entity.objects.create(name=name, slug=slug, kind="person", is_public=public)


def resolve(name="Dra. Beatriz Fictícia Ramos", external_id="ep-fictional-1"):
    return resolve_person("ep", external_id, name=name, basis=BASIS)


@pytest.mark.parametrize(
    "value",
    ["500000000", "501000119", "601234561", "710000014", "905555554", "990001237"],
)
def test_nipc_accepts_legal_person_numbers(value):
    assert valid_nipc(value)


@pytest.mark.parametrize(
    "value",
    [
        # Valid check digits, but natural persons, estates or sole traders.
        "123456789",
        "450000010",
        "700000011",
        "800000013",
        # Legal-person prefix with a wrong check digit, and malformed values.
        "500000001",
        "50000000",
        "5000000000",
        "50000000a",
        "",
    ],
)
def test_nipc_rejects_natural_person_prefixes_and_bad_check_digits(value):
    assert not valid_nipc(value)


def test_name_normalisation_ignores_accents_titles_and_punctuation_but_keeps_surnames():
    assert normalise_name("Prof.ª Doutora  Ana-Maria Conceição") == "ana maria conceicao"
    assert normalise_name("Engº. RUI   Simões") == "rui simoes"
    # "Mestre" is also a surname: only leading titles are dropped.
    assert normalise_name("Rui Mestre") == "rui mestre"


def test_namesake_becomes_a_pending_suggestion_instead_of_a_new_entity(identity_reviewer):
    namesake = person("Beatriz Ficticia Ramos", slug="beatriz-ficticia")
    before = Entity.objects.count()
    assert resolve() is None
    assert resolve() is None
    suggestion = IdentitySuggestion.objects.get()
    assert (suggestion.candidate_id, suggestion.status) == (namesake.pk, "pending")
    assert Entity.objects.count() == before
    assert not SourceIdentity.objects.exists()
    reject_suggestion(suggestion, identity_reviewer)
    created = resolve()
    assert created is not None and created.pk != namesake.pk
    assert created.is_public and created.kind == "person"
    assert resolve() == created
    assert Entity.objects.count() == before + 1
    assert SourceIdentity.objects.get(source="ep", external_id="ep-fictional-1").entity == created


def test_accepting_one_candidate_records_reviewed_identity_and_rejects_siblings(
    identity_reviewer,
):
    first = person("Beatriz Fictícia Ramos", slug="beatriz-ficticia-a")
    second = person("BEATRIZ FICTICIA RAMOS", slug="beatriz-ficticia-b")
    suggestions = suggest_person("ep", "ep-fictional-1", name="Beatriz Fictícia Ramos", basis=BASIS)
    assert {item.candidate_id for item in suggestions} == {first.pk, second.pk}
    chosen = next(item for item in suggestions if item.candidate_id == second.pk)
    identity = accept_suggestion(chosen, identity_reviewer)
    assert (identity.entity_id, identity.reviewed_by_id) == (second.pk, identity_reviewer.pk)
    assert identity.review_notes == BASIS
    assert dict(IdentitySuggestion.objects.values_list("candidate_id", "status")) == {
        first.pk: "rejected",
        second.pk: "accepted",
    }
    assert resolve() == second
    with pytest.raises(ValidationError):
        reject_suggestion(chosen, identity_reviewer)


def test_suggestion_decisions_need_a_fresh_identity_permission(django_user_model):
    person("Beatriz Fictícia Ramos", slug="beatriz-ficticia")
    assert resolve() is None
    outsider = django_user_model.objects.create_user(username="fictional-outsider")
    suggestion = IdentitySuggestion.objects.get()
    with pytest.raises(PermissionDenied):
        accept_suggestion(suggestion, outsider)
    with pytest.raises(PermissionDenied):
        reject_suggestion(suggestion, outsider)
    assert IdentitySuggestion.objects.get().status == "pending"
    assert not SourceIdentity.objects.exists()


def test_private_or_already_identified_namesakes_are_not_candidates():
    person("Beatriz Fictícia Ramos", slug="beatriz-privada", public=False)
    mapped = person("Beatriz Fictícia Ramos", slug="beatriz-mapeada")
    SourceIdentity.objects.create(source="ep", external_id="ep-fictional-other", entity=mapped)
    created = resolve()
    assert created is not None and created.slug.startswith("ep-ep-fictional-1-")
    assert not IdentitySuggestion.objects.exists()


def test_restricted_matching_ignores_namesakes_outside_the_restriction():
    person("Beatriz Fictícia Ramos", slug="beatriz-fora")
    inside = person("Carla Fictícia Lopes", slug="carla-dentro")
    created = resolve_person(
        "ep",
        "ep-fictional-1",
        name="Beatriz Fictícia Ramos",
        basis=BASIS,
        restrict_to=Entity.objects.filter(pk=inside.pk),
    )
    assert created is not None
    assert not IdentitySuggestion.objects.exists()


def test_official_entities_are_keyed_only_by_valid_anchors():
    company = official_entity(
        "nipc", "500000000", name="Empresa Fictícia, S.A.", kind="company", classification=""
    )
    assert company.classification == "company" and company.is_public
    again = official_entity(
        "nipc", "500000000", name="Outro Nome Fictício", kind="company", classification=""
    )
    assert again == company and again.name == "Empresa Fictícia, S.A."
    for scheme, external_id, kind in (
        ("nipc", "500000001", "company"),
        ("nipc", "123456789", "company"),
        ("sioe", "12345", "person"),
        ("wikidata", "Q1", "organisation"),
    ):
        with pytest.raises(ValidationError):
            official_entity(scheme, external_id, name="Fictícia", kind=kind, classification="")
    assert Entity.objects.count() == 1


def test_bulk_resolution_reuses_existing_and_validates_before_writing():
    existing = official_entity(
        "nipc", "500000000", name="Empresa Fictícia", kind="company", classification="company"
    )
    resolved = official_entities_bulk(
        "nipc",
        {
            "500000000": ("Nome alterado", "company", "company"),
            "601234561": ("Associação Fictícia", "organisation", "association"),
        },
    )
    assert resolved["500000000"] == existing
    created = Entity.objects.get(pk=resolved["601234561"].pk)
    assert (created.classification, created.is_public) == ("association", True)
    assert SourceIdentity.objects.get(source="nipc", external_id="601234561").entity == created
    with pytest.raises(ValidationError):
        official_entities_bulk(
            "nipc",
            {
                "710000014": ("Fundação Fictícia", "organisation", "foundation"),
                "123456789": ("Pessoa singular fictícia", "company", "company"),
            },
        )
    assert not SourceIdentity.objects.filter(external_id="710000014").exists()
    assert Entity.objects.count() == 2


def test_ar_resolvers_never_duplicate_the_assembly_or_a_deputy():
    namesake = person("Pessoa Fictícia 91001", slug="homonima-parlamentar")
    deputy = ar_person("91001", "Pessoa Fictícia 91001")
    assert deputy.pk != namesake.pk
    assert ar_person("91001", "Nome Alterado") == deputy
    assembly = ar_institution()
    assert ar_institution() == assembly
    assert assembly.classification == "parliament"
    data: JSONObject = {"roster": {"DepCadId": "91001"}, "biography": {}}
    apply_snapshot(
        ParliamentSnapshot(
            legislature="XVII",
            as_of=DAY,
            expected_count=1,
            members=(MemberRecord("91001", "Pessoa Fictícia 91001", DAY, None, data),),
            roster_url="https://app.parlamento.pt/webutils/docs/doc.txt?path=fictional&fich=InformacaoBaseXVII_json.txt&Inline=true",
            biography_url="https://app.parlamento.pt/webutils/docs/doc.txt?path=fictional&fich=RegistoBiograficoXVII_json.txt&Inline=true",
        )
    )
    assert ParliamentImportState.objects.get().institution == assembly
    assert ParliamentMember.objects.get(cadastro_id="91001").entity == deputy
    assert ar_person("91001", "Pessoa Fictícia 91001") == deputy
    assert ar_institution() == assembly
    assert Entity.objects.filter(kind="organisation").count() == 1
    assert Entity.objects.filter(kind="person").count() == 2


def test_ar_person_adopts_an_existing_deputy_without_an_identity():
    retained = person("Deputada Fictícia", slug="deputada-retida")
    ParliamentMember.objects.create(cadastro_id="91002", entity=retained, as_of=DAY)
    assert ar_person("91002", "Deputada Fictícia") == retained
    identity = SourceIdentity.objects.get(source="parliament", external_id="91002")
    assert identity.entity == retained
