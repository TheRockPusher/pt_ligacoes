from datetime import date

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from ligacoes.core.identity import (
    OfficeContext,
    accept_suggestion,
    ar_institution,
    ar_person,
    declared_organisation,
    normalise_name,
    official_entities_bulk,
    official_entity,
    record_alias,
    reject_suggestion,
    resolve_person,
    scoped_person,
    scoped_person_id,
    suggest_identity,
    suggest_person,
    valid_nipc,
)
from ligacoes.core.models import (
    Entity,
    EntityAlias,
    IdentitySuggestion,
    ParliamentImportState,
    ParliamentMember,
    ParliamentStatusInterval,
    Relationship,
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


def test_namesake_gets_a_separate_public_entity_and_advisory_suggestion(identity_reviewer):
    namesake = person("Beatriz Ficticia Ramos", slug="beatriz-ficticia")
    before = Entity.objects.count()
    created = resolve()
    assert resolve() == created
    suggestion = IdentitySuggestion.objects.get()
    assert (suggestion.candidate_id, suggestion.status) == (namesake.pk, "pending")
    assert created.pk != namesake.pk and created.is_public and created.kind == "person"
    assert Entity.objects.count() == before + 1
    assert SourceIdentity.objects.get(source="ep", external_id="ep-fictional-1").entity == created
    reject_suggestion(suggestion, identity_reviewer)
    assert resolve() == created


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
    suggest_person("ep", "ep-fictional-1", name="Beatriz Fictícia Ramos", basis=BASIS)
    outsider = django_user_model.objects.create_user(username="fictional-outsider")
    suggestion = IdentitySuggestion.objects.get()
    with pytest.raises(PermissionDenied):
        accept_suggestion(suggestion, outsider)
    with pytest.raises(PermissionDenied):
        reject_suggestion(suggestion, outsider)
    assert IdentitySuggestion.objects.get().status == "pending"
    assert not SourceIdentity.objects.exists()


def test_private_namesakes_are_not_candidates_but_other_ids_do_not_exclude_people():
    person("Beatriz Fictícia Ramos", slug="beatriz-privada", public=False)
    mapped = person("Beatriz Fictícia Ramos", slug="beatriz-mapeada")
    SourceIdentity.objects.create(source="ep", external_id="ep-fictional-other", entity=mapped)
    created = resolve()
    assert created != mapped
    assert IdentitySuggestion.objects.get().candidate == mapped


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
    data: JSONObject = {
        "roster": {
            "DepCadId": "91001",
            "DepIds": ["fictional-1"],
            "DepNomeCompleto": "Pessoa Fictícia 91001",
            "DepNomeParlamentar": "Pessoa Fictícia 91001",
            "DepSituacao": [
                {
                    "DepId": "fictional-1",
                    "sioDes": "Efetivo",
                    "sioDtInicio": DAY.isoformat(),
                    "sioDtFim": "",
                }
            ],
        },
        "biography": {},
    }
    apply_snapshot(
        ParliamentSnapshot(
            legislature="XVII",
            as_of=DAY,
            legislature_start=DAY,
            members=(
                MemberRecord(
                    "91001",
                    "Pessoa Fictícia 91001",
                    DAY,
                    None,
                    data,
                    periods=(("fictional-1", DAY, None),),
                ),
            ),
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


def institution(slug="fictional-institution", classification="public_body"):
    return Entity.objects.create(
        name=f"Instituição Fictícia {slug}",
        slug=slug,
        kind="organisation",
        classification=classification,
        is_public=True,
    )


def published_relationship(subject, object_, *, start=None, end=None, kind="public_office"):
    # Fictional, already-published source data for identity corroboration.
    relationship = Relationship(
        subject=subject,
        object=object_,
        kind=kind,
        start_date=start,
        end_date=end,
        status="published",
        reviewed_at=timezone.now(),
    )
    Relationship.objects.bulk_create([relationship])
    return relationship


def test_existing_identity_reuse_records_every_new_official_alias():
    retained = person("Beatriz Fictícia Ramos", slug="retained")
    SourceIdentity.objects.create(source="ep", external_id="ep-fictional-1", entity=retained)
    assert (
        resolve_person(
            "ep",
            "ep-fictional-1",
            name="Beatriz F. Ramos",
            aliases=("Dra. Beatriz Fictícia Ramos",),
            basis=BASIS,
        )
        == retained
    )
    assert set(
        EntityAlias.objects.filter(entity=retained).values_list("normalised", flat=True)
    ) == {
        "beatriz f ramos",
        "beatriz ficticia ramos",
    }
    assert not IdentitySuggestion.objects.exists()


@pytest.mark.parametrize(
    ("start", "end"),
    [(date(2026, 1, 1), None), (None, None), (None, DAY), (DAY, DAY)],
)
def test_s1_links_one_namesake_with_overlapping_institution_interval(start, end):
    retained = person("Beatriz Fictícia Ramos", slug="retained")
    body = institution()
    relationship = published_relationship(retained, body, start=start, end=end)
    resolved = resolve_person(
        "ept",
        "fictional-holder",
        name="Beatriz Fictícia Ramos",
        basis=BASIS,
        offices=(OfficeContext(body, DAY, DAY),),
    )
    assert resolved == retained
    identity = SourceIdentity.objects.get(source="ept", external_id="fictional-holder")
    assert identity.reviewed_by is None and identity.reviewed_at is None
    assert identity.review_notes.startswith("Correspondência automática: S1,")
    assert str(relationship.pk) in identity.review_notes


@pytest.mark.parametrize("incoming_parent", [True, False])
def test_s1_matches_part_of_in_either_direction(incoming_parent):
    retained = person("Beatriz Fictícia Ramos", slug="retained")
    parent, child = institution("parent"), institution("child")
    published_relationship(child, parent, kind="part_of")
    incoming, existing = (parent, child) if incoming_parent else (child, parent)
    published_relationship(retained, existing, start=DAY)
    assert (
        resolve_person(
            "ept",
            "fictional-holder",
            name=retained.name,
            basis=BASIS,
            offices=(OfficeContext(incoming, DAY, None),),
        )
        == retained
    )


@pytest.mark.parametrize("reason", ["disjoint", "draft", "private", "sibling"])
def test_s1_does_not_link_without_a_published_overlapping_institution(reason):
    retained = person("Beatriz Fictícia Ramos", slug="retained")
    body = institution()
    relationship = published_relationship(
        retained,
        body,
        start=date(2026, 1, 1),
        end=date(2026, 8, 31) if reason == "disjoint" else None,
    )
    incoming = body
    if reason == "draft":
        Relationship.objects.filter(pk=relationship.pk).update(status="draft")
    elif reason == "private":
        Entity.objects.filter(pk=body.pk).update(is_public=False)
    elif reason == "sibling":
        parent, incoming = institution("parent"), institution("sibling")
        published_relationship(body, parent, kind="part_of")
        published_relationship(incoming, parent, kind="part_of")
    resolved = resolve_person(
        "ept",
        "fictional-holder",
        name=retained.name,
        basis=BASIS,
        offices=(OfficeContext(incoming, DAY, None),),
    )
    assert resolved != retained
    assert IdentitySuggestion.objects.get().candidate == retained


def test_aliases_find_the_candidate_but_still_require_corroboration():
    retained = person("Beatriz Maria Fictícia Ramos", slug="retained")
    body = institution()
    record_alias(retained, "Beatriz Ramos", scheme="parliament", external_id="91004")
    published_relationship(retained, body, start=DAY)
    assert (
        resolve_person(
            "ept",
            "fictional-holder",
            name="Beatriz Ramos",
            basis=BASIS,
            offices=(OfficeContext(body, DAY, None),),
        )
        == retained
    )
    other = resolve_person("ep", "fictional-mep", name="Beatriz Ramos", basis=BASIS)
    assert other != retained
    assert IdentitySuggestion.objects.get(scheme="ep").candidate == retained


def test_incoming_alias_can_find_canonical_name_candidate():
    retained = person("Beatriz Maria Fictícia Ramos", slug="retained")
    body = institution()
    published_relationship(retained, body, start=DAY)
    assert (
        resolve_person(
            "ept",
            "fictional-holder",
            name="Beatriz Ramos",
            aliases=(retained.name,),
            basis=BASIS,
            offices=(OfficeContext(body, DAY, None),),
        )
        == retained
    )


@pytest.mark.parametrize("offset", [-15, 0, 15])
def test_s3a_links_government_office_near_parliament_suspension(offset):
    from datetime import timedelta

    retained = ar_person("91003", "Beatriz Fictícia Ramos")
    ParliamentStatusInterval.objects.create(
        cadastro_id="91003",
        entity=retained,
        legislature="XVII",
        status="Suspenso por exercício de funções",
        start=DAY + timedelta(days=offset),
    )
    government = institution(classification="government_department")
    assert (
        resolve_person(
            "government",
            "fictional-minister",
            name=retained.name,
            basis=BASIS,
            offices=(OfficeContext(government, DAY, None),),
        )
        == retained
    )
    assert "S3(a)" in SourceIdentity.objects.get(source="government").review_notes


@pytest.mark.parametrize(
    ("status", "offset", "classification"),
    [("Suspenso", 16, "government"), ("Efetivo", 0, "government"), ("Suspenso", 0, "public_body")],
)
def test_s3a_requires_suspension_government_and_fifteen_day_window(status, offset, classification):
    from datetime import timedelta

    retained = ar_person("91003", "Beatriz Fictícia Ramos")
    ParliamentStatusInterval.objects.create(
        cadastro_id="91003",
        entity=retained,
        legislature="XVII",
        status=status,
        start=DAY + timedelta(days=offset),
    )
    body = institution(classification=classification)
    assert (
        resolve_person(
            "government",
            "fictional-minister",
            name=retained.name,
            basis=BASIS,
            offices=(OfficeContext(body, DAY, None),),
        )
        != retained
    )


def test_s3b_links_incoming_ar_suspension_to_existing_government_person():
    retained = official_entity(
        "government",
        "fictional-minister",
        name="Beatriz Fictícia Ramos",
        kind="person",
        classification="",
    )
    government = institution(classification="government_office")
    published_relationship(retained, government, start=date(2026, 9, 16))
    assert ar_person("91003", retained.name, suspensions=(DAY,)) == retained
    assert "S3(b)" in SourceIdentity.objects.get(source="parliament").review_notes


def test_s3b_is_only_a_signal_for_incoming_parliament_identity():
    retained = person("Beatriz Fictícia Ramos", slug="retained")
    published_relationship(retained, institution(classification="government"), start=DAY)
    assert (
        resolve_person(
            "ept",
            "fictional-holder",
            name=retained.name,
            basis=BASIS,
            suspensions=(DAY,),
        )
        != retained
    )
    assert ar_person("91003", retained.name, suspensions=(date(2026, 10, 1),)) != retained


def test_two_corroborated_candidates_create_separate_person_without_merging():
    first = person("Beatriz Fictícia Ramos", slug="first")
    second = person(first.name, slug="second")
    body = institution()
    for candidate in (first, second):
        published_relationship(candidate, body, start=DAY)
    created = resolve_person(
        "ept",
        "fictional-holder",
        name=first.name,
        basis=BASIS,
        offices=(OfficeContext(body, DAY, None),),
    )
    assert created not in (first, second)
    assert set(IdentitySuggestion.objects.values_list("candidate_id", flat=True)) == {
        first.pk,
        second.pk,
    }


def test_exactly_one_corroborated_candidate_wins_even_with_uncorroborated_namesake():
    retained = person("Beatriz Fictícia Ramos", slug="retained")
    person(retained.name, slug="namesake")
    body = institution()
    published_relationship(retained, body, start=DAY)
    assert (
        resolve_person(
            "ept",
            "fictional-holder",
            name=retained.name,
            basis=BASIS,
            offices=(OfficeContext(body, DAY, None),),
        )
        == retained
    )


def test_s2_uses_imported_qid_carrying_both_official_identifiers():
    from ligacoes.core.wikidata import CrosswalkSnapshot
    from ligacoes.core.wikidata import apply_snapshot as apply_crosswalk

    retained = ar_person("91003", "Beatriz Fictícia Ramos")
    apply_crosswalk(
        CrosswalkSnapshot(
            items={"Q990001": {"parliament": frozenset({"91003"}), "ep": frozenset({"990001"})}},
            rows=2,
            dropped=0,
        )
    )
    assert resolve_person("ep", "990001", name=retained.name, basis=BASIS) == retained
    assert "S2, Wikidata Q990001" in SourceIdentity.objects.get(source="ep").review_notes


@pytest.mark.parametrize("invalid", ["qid_only", "name_suggestion", "wrong_anchor", "rejected"])
def test_s2_does_not_use_bare_qid_or_non_crosswalk_suggestions(invalid):
    from ligacoes.core.wikidata import CrosswalkSnapshot
    from ligacoes.core.wikidata import apply_snapshot as apply_crosswalk

    retained = ar_person("91003", "Beatriz Fictícia Ramos")
    apply_crosswalk(
        CrosswalkSnapshot(
            items={"Q990001": {"parliament": frozenset({"91003"}), "ep": frozenset({"990001"})}},
            rows=2,
            dropped=0,
        )
    )
    suggestion = IdentitySuggestion.objects.get(scheme="ep", external_id="990001")
    if invalid == "qid_only":
        suggestion.delete()
    elif invalid == "name_suggestion":
        IdentitySuggestion.objects.filter(pk=suggestion.pk).update(
            name_as_published=retained.name,
            basis=BASIS,
        )
    elif invalid == "wrong_anchor":
        IdentitySuggestion.objects.filter(pk=suggestion.pk).update(
            basis=suggestion.basis.replace("91003", "91004"),
        )
    else:
        IdentitySuggestion.objects.filter(pk=suggestion.pk).update(status="rejected")
    assert resolve_person("ep", "990001", name=retained.name, basis=BASIS) != retained


def test_scoped_person_reuses_scope_and_name_without_merging_across_scopes():
    first = scoped_person("fictional:scope-a", "Dra. Beatriz Fictícia Ramos", basis=BASIS)
    assert scoped_person("fictional:scope-a", "BEATRIZ FICTICIA RAMOS", basis=BASIS) == first
    other = scoped_person("fictional:scope-b", "Beatriz Fictícia Ramos", basis=BASIS)
    assert other != first and other.is_public
    assert (
        SourceIdentity.objects.get(
            source="scoped_name",
            external_id="fictional:scope-a:beatriz ficticia ramos",
        ).entity
        == first
    )
    assert IdentitySuggestion.objects.get().candidate == first


def test_scoped_person_links_with_corroboration_and_retains_its_scoped_identity():
    retained = ar_person("91003", "Beatriz Fictícia Ramos")
    body = institution()
    published_relationship(retained, body, start=DAY)
    assert (
        scoped_person(
            "fictional:scope",
            retained.name,
            basis=BASIS,
            offices=(OfficeContext(body, DAY, None),),
        )
        == retained
    )
    assert (
        SourceIdentity.objects.get(
            source="scoped_name",
            external_id=scoped_person_id("fictional:scope", retained.name),
        ).entity
        == retained
    )


def test_oversized_scoped_key_hashes_name_deterministically_without_truncation():
    name = "Beatriz " + "Fictícia " * 20
    scope = "fictional:" + "a" * 70
    key = scoped_person_id(scope, name)
    assert len(key) <= 240 and key.startswith(f"{scope}:sha256:")
    assert scoped_person_id(scope, name.upper()) == key
    assert scoped_person_id(scope, name + " Ramos") != key
    assert scoped_person(scope, name, basis=BASIS) == scoped_person(scope, name, basis=BASIS)
    with pytest.raises(ValidationError):
        scoped_person_id("a" * 240, name)


def test_declared_organisation_prefers_valid_nipc_and_records_declared_alias():
    retained = official_entity(
        "nipc",
        "500000000",
        name="Empresa Fictícia, S.A.",
        kind="company",
        classification="",
    )
    assert declared_organisation("Novo Nome Fictício", nipc="500000000") == retained
    assert EntityAlias.objects.get(entity=retained).normalised == "novo nome ficticio"


def test_declared_organisation_uses_unique_anchored_name_or_alias():
    retained = official_entity(
        "sioe",
        "fictional-body",
        name="Instituição Fictícia",
        kind="organisation",
        classification="",
    )
    record_alias(
        retained, "Instituição Fictícia Antiga", scheme="sioe", external_id="fictional-body"
    )
    assert declared_organisation("INSTITUICAO FICTICIA") == retained
    assert declared_organisation("Instituição Fictícia Antiga") == retained
    assert not SourceIdentity.objects.filter(source="declared_name").exists()


@pytest.mark.parametrize("nipc", ["", "123456789", "500000001"])
def test_declared_organisation_name_only_reuse_does_not_store_invalid_or_personal_nipc(nipc):
    person("Empresa Fictícia", slug="person-namesake")
    created = declared_organisation("Empresa Fictícia", nipc=nipc)
    assert created.kind == "organisation" and created.is_public
    assert declared_organisation("EMPRESA FICTICIA") == created
    identity = SourceIdentity.objects.get(source="declared_name")
    assert identity.external_id == "empresa ficticia"
    assert not SourceIdentity.objects.filter(source="nipc").exists()


def test_ambiguous_anchored_organisations_fall_back_to_separate_declared_name():
    for identifier in ("fictional-a", "fictional-b"):
        official_entity(
            "sioe",
            identifier,
            name="Instituição Fictícia",
            kind="organisation",
            classification="",
        )
    created = declared_organisation("Instituição Fictícia")
    assert Entity.objects.count() == 3
    assert SourceIdentity.objects.get(source="declared_name").entity == created
    assert declared_organisation("Instituição Fictícia") == created


def test_resolve_person_rejects_organisation_schemes_and_existing_organisation_mappings():
    body = institution()
    SourceIdentity.objects.create(source="ep", external_id="fictional-body", entity=body)
    with pytest.raises(ValidationError):
        resolve_person("ep", "fictional-body", name=body.name, basis=BASIS)
    for scheme in ("nipc", "sioe", "lei", "eu_tr", "ec", "declared_name"):
        with pytest.raises(ValidationError):
            resolve_person(scheme, "500000000", name="Pessoa Fictícia", basis=BASIS)
    with pytest.raises(ValidationError):
        suggest_identity(
            "nipc",
            "500000000",
            candidate=person("Fictícia", slug="ficticia"),
            name="Fictícia",
            basis=BASIS,
        )


def test_canonical_normalised_name_is_maintained_on_entity_rename():
    retained = person("Dra. Beatriz Fictícia Ramos", slug="retained")
    assert retained.normalised_name == normalise_name(retained.name)
    retained.name = "Beatriz Fictícia Lopes"
    retained.save(update_fields=["name"])
    retained.refresh_from_db()
    assert retained.normalised_name == "beatriz ficticia lopes"
    suggest_person("ep", "fictional-id", name="Beatriz Fictícia Lopes", basis=BASIS)
    assert IdentitySuggestion.objects.get().candidate == retained


def test_declared_organisation_reuses_private_anchor_without_publishing_it():
    retained = official_entity(
        "sioe",
        "fictional-private-body",
        name="Instituição Fictícia",
        kind="organisation",
        classification="",
        public=False,
    )
    assert declared_organisation(retained.name) == retained
    retained.refresh_from_db()
    assert not retained.is_public
    assert not SourceIdentity.objects.filter(source="declared_name").exists()


def test_scoped_identity_suggestions_reject_organisation_candidates():
    body = institution()
    with pytest.raises(ValidationError):
        suggest_identity(
            "scoped_name",
            "fictional:scope:name",
            candidate=body,
            name=body.name,
            basis=BASIS,
        )


def test_s1_correlated_namesakes_outside_restriction_cannot_link():
    retained = person("Beatriz Fictícia Ramos", slug="retained")
    body = institution()
    published_relationship(retained, body, start=DAY)
    assert (
        resolve_person(
            "ept",
            "fictional-holder",
            name=retained.name,
            basis=BASIS,
            offices=(OfficeContext(body, DAY, None),),
            restrict_to=Entity.objects.none(),
        )
        != retained
    )
    assert not IdentitySuggestion.objects.exists()


def test_simultaneous_namesake_ar_cadastros_are_separate_people():
    assembly = ar_institution()
    first = ar_person("91005", "Beatriz Fictícia Ramos")
    published_relationship(first, assembly, start=DAY)
    second = ar_person(
        "91006",
        first.name,
        offices=(OfficeContext(assembly, DAY, None),),
    )
    assert second != first
    assert SourceIdentity.objects.get(source="parliament", external_id="91005").entity == first
    assert SourceIdentity.objects.get(source="parliament", external_id="91006").entity == second
    ParliamentMember.objects.create(cadastro_id="91005", entity=first, as_of=DAY)
    ParliamentMember.objects.create(cadastro_id="91006", entity=second, as_of=DAY)
    assert (
        IdentitySuggestion.objects.get(
            scheme="parliament",
            external_id="91006",
        ).candidate
        == first
    )


def test_distinct_ept_holder_ids_can_link_one_corroborated_person():
    body = institution()
    first = resolve_person(
        "ept",
        "fictional-holder-a",
        name="Beatriz Fictícia Ramos",
        basis=BASIS,
    )
    published_relationship(first, body, start=DAY)
    second = resolve_person(
        "ept",
        "fictional-holder-b",
        name=first.name,
        basis=BASIS,
        offices=(OfficeContext(body, DAY, None),),
    )
    assert second == first
    assert set(
        SourceIdentity.objects.filter(source="ept", entity=first).values_list(
            "external_id",
            flat=True,
        )
    ) == {"fictional-holder-a", "fictional-holder-b"}


def test_scoped_people_can_link_across_distinct_scopes_with_corroboration():
    body = institution()
    first = scoped_person("fictional:scope-a", "Beatriz Fictícia Ramos", basis=BASIS)
    published_relationship(first, body, start=DAY)
    second = scoped_person(
        "fictional:scope-b",
        first.name,
        basis=BASIS,
        offices=(OfficeContext(body, DAY, None),),
    )
    assert second == first
    assert SourceIdentity.objects.filter(source="scoped_name", entity=first).count() == 2
