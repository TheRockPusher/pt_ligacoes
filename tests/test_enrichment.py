from dataclasses import replace
from datetime import date, timedelta

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import include, path, reverse
from django.utils import timezone

from ligacoes.core.enrichment import (
    ObservationInput,
    backfill_biography_roles,
    convert_observation,
    get_source_identity,
    require_source_approval,
    sync_observations,
)
from ligacoes.core.models import (
    Entity,
    Evidence,
    ParliamentMember,
    ParliamentRecord,
    Relationship,
    Source,
    SourceApproval,
    SourceIdentity,
    SourceObservation,
    SourceSyncState,
)
from ligacoes.core.parliament_import import apply_snapshot
from ligacoes.core.parliament_parse import JSONObject, MemberRecord, ParliamentSnapshot
from ligacoes.core.services import publish_relationship
from ligacoes.public.selectors import public_evidence, public_relationships

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)
urlpatterns = [path("admin/", admin.site.urls), path("", include("ligacoes.public.urls"))]


@pytest.fixture(autouse=True)
def admin_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def editor(django_user_model):
    user = django_user_model.objects.create_user(
        username="fictional-enrichment-editor", is_staff=True
    )
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="core",
            codename__in=[
                "review_sourceobservation",
                "review_sourceidentity",
                "approve_sourceapproval",
                "view_parliamentrecord",
            ],
        )
    )
    return user


@pytest.fixture
def identity_reviewer(django_user_model):
    user = django_user_model.objects.create_user(
        username="fictional-identity-reviewer", is_staff=True
    )
    user.user_permissions.add(Permission.objects.get(codename="review_sourceidentity"))
    return user


@pytest.fixture
def organisation():
    return Entity.objects.create(
        name="Empresa inteiramente fictícia", slug="empresa-ficticia", kind="company"
    )


@pytest.fixture
def observed(editor):
    person = Entity.objects.create(
        name="Pessoa fictícia das fontes", slug="pessoa-ficticia-fontes", kind="person"
    )
    identity = SourceIdentity.objects.create(
        source="ept",
        external_id="holder-fictional-1",
        entity=person,
        reviewed_by=editor,
        reviewed_at=timezone.now(),
        review_notes="Correspondência fictícia verificada pelo contexto do cargo e identificador oficial.",
    )
    return ObservationInput(
        external_id="declaration-fictional-1:role-1",
        revision="revision-a",
        identity=identity,
        category="declared_interest",
        passage="Consultoria fictícia para uma organização identificada no documento.",
        source_url="https://example.org/declaracao-ficticia",
        publisher="Editor fictício",
        reference="Declaração fictícia 1 / atividade 1",
        title="Declaração inteiramente fictícia",
        declared_on=DAY,
    )


def sync(items, *, day=DAY, scope="holder:fictional-1"):
    return sync_observations(source="ept", scope=scope, observations=tuple(items), as_of=day)


def convert(candidate, editor, organisation, **changes):
    options = {
        "object": organisation,
        "kind": "professional_activity",
        "description": "Consultoria documentada, sem inferir vínculo laboral.",
        "review_notes": "Organização verificada pelo contexto documental; consultoria explícita, datas desconhecidas.",
    }
    options.update(changes)
    return convert_observation(candidate, editor, **options)


def publish_candidate(candidate, reviewer):
    candidate.refresh_from_db()
    relationship = candidate.relationship
    for entity in (relationship.subject, relationship.object):
        entity.is_public = True
        entity.save()
    candidate.evidence.source.is_public = True
    candidate.evidence.source.save()
    candidate.evidence.is_public = True
    candidate.evidence.save()
    return publish_relationship(relationship, reviewer)


def test_candidate_conversion_keeps_unknown_dates_and_is_not_public(
    observed, editor, organisation, reviewer
):
    sync([observed])
    candidate = SourceObservation.objects.get()
    assert candidate.relationship_id is None
    assert not Relationship.objects.exists()
    relationship = convert(candidate, editor, organisation)
    candidate.refresh_from_db()
    assert relationship.kind == "professional_activity"
    assert relationship.start_date is None and relationship.end_date is None
    assert candidate.declared_on == DAY
    assert relationship.status == "draft" and relationship.reviewed_at is None
    assert candidate.reviewed_by == editor
    assert candidate.evidence is not None
    assert candidate.evidence.excerpt == observed.passage
    assert not candidate.evidence.is_public and not candidate.evidence.source.is_public
    assert not public_relationships().exists() and not public_evidence().exists()
    published = publish_candidate(candidate, reviewer)
    assert public_relationships().get().pk == published.pk
    assert public_evidence().get().pk == candidate.evidence_id


def test_conversion_requires_fresh_permission_and_correct_scope(
    observed, editor, organisation, reviewer
):
    sync([observed])
    candidate = SourceObservation.objects.get()
    with pytest.raises(PermissionDenied):
        convert(candidate, reviewer, organisation)
    # A previously cached permission must not outlive revocation.
    assert editor.has_perm("core.review_sourceobservation")
    editor.user_permissions.remove(Permission.objects.get(codename="review_sourceobservation"))
    with pytest.raises(PermissionDenied):
        convert(candidate, editor, organisation)
    assert not Relationship.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "membership"},
        {"kind": "family"},
        {"review_notes": ""},
        {"start_date": DAY, "end_date": DAY - timedelta(days=1)},
    ],
)
def test_conversion_rejects_unsupported_affiliations_and_dates(
    observed, editor, organisation, changes
):
    sync([observed])
    with pytest.raises(ValidationError):
        convert(SourceObservation.objects.get(), editor, organisation, **changes)
    assert not Relationship.objects.exists()


def test_identical_snapshot_preserves_review_and_editorial_prose(
    observed, editor, organisation, reviewer
):
    sync([observed])
    candidate = SourceObservation.objects.get()
    relation = convert(candidate, editor, organisation)
    relation.description = "Texto editorial que a fonte não pode substituir."
    relation.save()
    published = publish_candidate(candidate, reviewer)
    result = sync([observed], day=DAY + timedelta(days=1))
    relation.refresh_from_db()
    assert result == {"created": 0, "changed": 0, "ceased": 0, "drafts": 0}
    assert SourceObservation.objects.count() == 1
    assert relation.description == "Texto editorial que a fonte não pode substituir."
    assert relation.reviewed_at == published.reviewed_at
    assert public_relationships().filter(pk=relation.pk).exists()


@pytest.mark.parametrize("transition", ["change", "cessation"])
def test_source_withdrawal_and_return_never_restore_review(
    observed, editor, organisation, reviewer, transition
):
    sync([observed])
    candidate = SourceObservation.objects.get()
    relation = convert(candidate, editor, organisation)
    publish_candidate(candidate, reviewer)
    replacement = replace(observed, revision="revision-b", passage="Passagem fictícia corrigida.")
    sync([replacement] if transition == "change" else [], day=DAY + timedelta(days=1))
    candidate.refresh_from_db()
    relation.refresh_from_db()
    assert not candidate.is_current and candidate.reviewed_at is None
    assert relation.status == "draft" and relation.reviewed_by is None
    assert candidate.evidence is not None
    assert not candidate.evidence.is_public
    assert not public_relationships().exists() and not public_evidence().exists()
    with pytest.raises(ValidationError):
        convert(candidate, editor, organisation)
    sync([observed], day=DAY + timedelta(days=2))
    candidate.refresh_from_db()
    relation.refresh_from_db()
    assert candidate.is_current and candidate.reviewed_at is None
    assert relation.status == "draft" and not candidate.evidence.is_public
    assert not public_relationships().exists()
    convert(candidate, editor, organisation)
    publish_candidate(candidate, reviewer)
    assert public_relationships().get().pk == relation.pk


def test_empty_snapshot_records_staleness_and_scopes_do_not_withdraw_other_holders(observed):
    sync([observed], scope="holder:other")
    sync([], day=DAY + timedelta(days=1))
    with pytest.raises(ValidationError):
        sync([observed])
    assert SourceObservation.objects.get().is_current
    assert SourceSyncState.objects.get(scope="holder:fictional-1").as_of == DAY + timedelta(days=1)


def test_reused_revision_cannot_silently_replace_source_passage(observed):
    sync([observed])
    with pytest.raises(ValidationError):
        sync([replace(observed, passage="Outra passagem sob a mesma revisão.")])
    assert SourceObservation.objects.get().passage == observed.passage


@pytest.mark.parametrize("field", ["source", "external_id", "entity_id", "used_at"])
def test_used_identity_cannot_move_claims_or_remove_used_marker(observed, field):
    sync([observed])
    identity = SourceIdentity.objects.get(pk=observed.identity.pk)
    original = SourceIdentity.objects.values().get(pk=identity.pk)
    replacement = Entity.objects.create(
        name="Outra pessoa fictícia", slug="pessoa-substituta", kind="person"
    )
    changes = {
        "source": "government",
        "external_id": "holder:substituted",
        "entity_id": replacement.pk,
        "used_at": None,
    }
    setattr(identity, field, changes[field])
    with pytest.raises(ValidationError):
        identity.save()
    with pytest.raises(ValidationError):
        identity.delete()
    assert SourceIdentity.objects.values().get(pk=identity.pk) == original
    assert SourceObservation.objects.get().identity.entity_id == observed.identity.entity_id


def test_external_identity_resolution_never_joins_a_namesake(organisation):
    namesake = Entity.objects.create(
        name="Pessoa oficial fictícia", slug="homonima-ficticia", kind="person"
    )
    identity = get_source_identity(
        source="government", external_id="person:fictional-1", name=namesake.name
    )
    assert identity.entity_id != namesake.pk and not identity.entity.is_public
    assert (
        get_source_identity(
            source="government", external_id="person:fictional-1", name="Texto alterado"
        ).pk
        == identity.pk
    )
    assert identity.entity.name == namesake.name
    SourceIdentity.objects.create(source="ept", external_id="holder:unreviewed", entity=namesake)
    with pytest.raises(ValidationError):
        get_source_identity(source="ept", external_id="holder:unreviewed")
    with pytest.raises(ValidationError):
        get_source_identity(source="ept", external_id="holder:missing", name=namesake.name)


def test_approval_is_source_specific_active_and_time_limited(editor):
    with pytest.raises(PermissionDenied):
        require_source_approval("government", scope="government_office")
    approval = SourceApproval.objects.create(
        source="government",
        purpose="Finalidade fictícia",
        reuse_basis="Autorização fictícia de teste",
        allowed_scopes=["government_office"],
        retention_conditions="Conservação fictícia limitada",
        review_due_at=timezone.now() + timedelta(days=1),
        approved_by=editor,
        is_active=True,
    )
    assert require_source_approval("government", scope="government_office").pk == approval.pk
    with pytest.raises(PermissionDenied):
        require_source_approval("ept", scope="declared_interest")
    with pytest.raises(PermissionDenied):
        require_source_approval("government", scope="declared_interest")
    SourceApproval.objects.filter(pk=approval.pk).update(
        review_due_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(PermissionDenied):
        require_source_approval("government", scope="government_office")
    SourceApproval.objects.filter(pk=approval.pk).update(
        review_due_at=timezone.now() + timedelta(days=1), is_active=False
    )
    with pytest.raises(PermissionDenied):
        require_source_approval("government", scope="government_office")


@pytest.fixture
def legacy_record(organisation):
    person = Entity.objects.create(
        name="Deputada inteiramente fictícia", slug="deputada-ficticia", kind="person"
    )
    member = ParliamentMember.objects.create(cadastro_id="91001", entity=person, as_of=DAY)
    source = Source.objects.create(
        title="Fonte parlamentar fictícia", url="https://example.org/fonte-ficticia"
    )
    relationship = Relationship.objects.create(
        subject=person, object=organisation, kind="public_office"
    )
    evidence = Evidence.objects.create(
        relationship=relationship, source=source, excerpt="Mandato fictício."
    )
    record = ParliamentRecord.objects.create(
        member=member,
        fingerprint="f" * 64,
        legislature="XVII",
        as_of=DAY,
        data={
            "biography": {
                "CadProfissao": "Profissão sem empregador",
                "CadCargosFuncoes": [
                    {
                        "FunId": "91002",
                        "FunAntiga": "S",
                        "FunDes": "Exerceu consultoria numa entidade fictícia; datas não fornecidas.",
                    },
                ],
            }
        },
        roster_url="https://example.org/roster-ficticio",
        biography_url="https://example.org/biografia-ficticia",
        relationship=relationship,
        evidence=evidence,
    )
    member.current_record = record
    member.save()
    return record


def test_retained_data_backfill_creates_only_private_candidates_idempotently(legacy_record, editor):
    before = (Entity.objects.count(), Relationship.objects.count(), Source.objects.count())
    result = backfill_biography_roles([legacy_record], editor)
    candidate = SourceObservation.objects.get()
    assert result["created"] == 1
    assert candidate.identity.entity_id == legacy_record.member.entity_id
    assert (
        candidate.object_id is None and candidate.kind == "" and candidate.relationship_id is None
    )
    assert candidate.effective_start is None and candidate.effective_end is None
    assert "FunId=91002" in candidate.reference
    assert (Entity.objects.count(), Relationship.objects.count(), Source.objects.count()) == before
    assert backfill_biography_roles([legacy_record], editor)["created"] == 0
    assert not public_relationships().exists()
    legacy_record.member.is_current = False
    legacy_record.member.save()
    with pytest.raises(ValidationError):
        backfill_biography_roles([legacy_record], editor)


def test_parliament_import_extracts_roles_then_withdraws_them_on_cessation(
    editor, organisation, reviewer
):
    def snapshot(cadastro, day):
        data: JSONObject = {
            "roster": {"DepCadId": cadastro},
            "biography": {
                "CadCargosFuncoes": [
                    {
                        "FunId": "91003",
                        "FunAntiga": "S",
                        "FunDes": "Consultoria profissional fictícia.",
                    }
                ]
            },
        }
        return ParliamentSnapshot(
            legislature="XVII",
            as_of=day,
            expected_count=1,
            members=(MemberRecord(cadastro, f"Pessoa fictícia {cadastro}", DAY, None, data),),
            roster_url="https://app.parlamento.pt/webutils/docs/doc.txt?path=fictional&fich=InformacaoBaseXVII_json.txt&Inline=true",
            biography_url="https://app.parlamento.pt/webutils/docs/doc.txt?path=fictional&fich=RegistoBiograficoXVII_json.txt&Inline=true",
        )

    apply_snapshot(snapshot("91001", DAY))
    candidate = SourceObservation.objects.get()
    convert(candidate, editor, organisation)
    publish_candidate(candidate, reviewer)
    apply_snapshot(snapshot("91004", DAY + timedelta(days=1)))
    candidate.refresh_from_db()
    assert candidate.evidence is not None
    assert not candidate.is_current and not candidate.evidence.is_public
    assert not public_relationships().exists()
    apply_snapshot(snapshot("91001", DAY + timedelta(days=2)))
    candidate.refresh_from_db()
    assert candidate.is_current and candidate.reviewed_at is None
    assert not public_relationships().exists()


def test_admin_conversion_and_backfill_need_separate_permissions(
    client, observed, editor, organisation, legacy_record, reviewer
):
    sync([observed])
    candidate = SourceObservation.objects.get()
    url = reverse("admin:core_sourceobservation_change", args=[candidate.pk])
    reviewer.is_staff = True
    reviewer.save()
    client.force_login(reviewer)
    assert client.get(url).status_code == 403
    assert client.get(reverse("admin:core_sourceapproval_add")).status_code == 403
    assert client.get(reverse("admin:core_sourceidentity_add")).status_code == 403
    payload = {
        "reviewed_object": str(organisation.pk),
        "reviewed_kind": "professional_activity",
        "reviewed_start": "",
        "reviewed_end": "",
        "reviewed_description": "Consultoria fictícia revista.",
        "conversion_notes": "Organização e consultoria verificadas; limites temporais desconhecidos.",
        "_save": "Guardar",
    }
    assert client.post(url, payload).status_code == 403
    client.force_login(editor)
    assert client.get(url).status_code == 200
    response = client.post(url, payload)
    assert response.status_code == 302
    candidate.refresh_from_db()
    assert candidate.relationship is not None
    assert candidate.evidence is not None
    assert candidate.relationship.kind == "professional_activity"
    assert candidate.relationship.status == "draft" and not candidate.evidence.is_public
    response = client.post(
        reverse("admin:core_parliamentrecord_changelist"),
        {
            "action": "extract_biography_roles",
            "_selected_action": [str(legacy_record.pk)],
        },
    )
    assert response.status_code == 302
    assert SourceObservation.objects.filter(
        source="parliament", identity__entity=legacy_record.member.entity
    ).exists()


def test_admin_records_approval_and_reviewed_identity_without_publication(client, editor, observed):
    client.force_login(editor)
    deadline = timezone.localtime(timezone.now() + timedelta(days=30))
    response = client.post(
        reverse("admin:core_sourceapproval_add"),
        {
            "source": "ept",
            "purpose": "Finalidade fictícia",
            "reuse_basis": "Referência fictícia",
            "allowed_scopes": '["declared_interest"]',
            "retention_conditions": "Rever e eliminar quando desnecessário.",
            "review_due_at_0": deadline.date().isoformat(),
            "review_due_at_1": "12:00:00",
            "is_active": "on",
            "_save": "Guardar",
        },
    )
    assert response.status_code == 302
    approval = SourceApproval.objects.get()
    assert approval.approved_by == editor and approval.approved_at is not None
    response = client.post(
        reverse("admin:core_sourceidentity_add"),
        {
            "source": "government",
            "external_id": "person:fictional-reviewed",
            "entity": str(observed.identity.entity_id),
            "review_notes": "Identificador e cargo oficiais conferidos; não apenas o nome.",
            "_save": "Guardar",
        },
    )
    assert response.status_code == 302
    mapping = SourceIdentity.objects.get(source="government")
    assert mapping.reviewed_by == editor and mapping.reviewed_at is not None
    assert mapping.entity_id == observed.identity.entity_id
    assert not Relationship.objects.exists() and not public_relationships().exists()


def test_identity_only_reviewer_can_pick_entities_without_entity_access(
    client, identity_reviewer, observed, organisation
):
    entity = observed.identity.entity
    entity.private_notes = "Confidencial: não mostrar no seletor."
    entity.description = "Descrição privada que não pertence ao seletor."
    entity.save()
    client.force_login(identity_reviewer)
    response = client.get(reverse("admin:core_sourceidentity_add"))
    assert response.status_code == 200
    widget = response.context["adminform"].form.fields["entity"].widget.widget
    picker_url = widget.get_url()
    params = {
        "app_label": "core",
        "model_name": "sourceidentity",
        "field_name": "entity",
        "term": entity.name,
    }
    response = client.get(picker_url, params)
    assert response.status_code == 200
    assert response.json() == {
        "results": [{"id": str(entity.pk), "text": entity.name}],
        "pagination": {"more": False},
    }
    assert client.get(picker_url, {**params, "term": organisation.name}).json()["results"] == []
    assert (
        client.get(
            picker_url, {**params, "model_name": "relationship", "field_name": "subject"}
        ).status_code
        == 403
    )
    assert client.get(reverse("admin:autocomplete"), params).status_code == 403
    assert client.get(reverse("admin:core_entity_changelist")).status_code == 403
    entity_url = reverse("admin:core_entity_change", args=[entity.pk])
    assert client.get(entity_url).status_code == 403
    assert client.post(entity_url, {"name": "Alteração proibida"}).status_code == 403
    response = client.post(
        reverse("admin:core_sourceidentity_add"),
        {
            "source": "ept",
            "external_id": "holder:chosen-through-picker",
            "entity": response.json()["results"][0]["id"],
            "review_notes": "Pessoa e identificador conferidos na fonte oficial.",
            "_save": "Guardar",
        },
    )
    assert response.status_code == 302
    mapping = SourceIdentity.objects.get(external_id="holder:chosen-through-picker")
    assert mapping.entity_id == entity.pk
    assert mapping.reviewed_by_id == identity_reviewer.pk


def test_identity_viewer_cannot_pick_or_reattest(client, django_user_model, observed):
    sync([observed])
    viewer = django_user_model.objects.create_user(username="identity-viewer", is_staff=True)
    viewer.user_permissions.add(
        *Permission.objects.filter(codename__in=["view_sourceidentity", "view_entity"])
    )
    client.force_login(viewer)
    url = reverse("admin:core_sourceidentity_change", args=[observed.identity.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert not response.context["has_change_permission"]
    original = SourceIdentity.objects.values().get(pk=observed.identity.pk)
    assert (
        client.post(
            url, {"review_notes": "Tentativa sem autorização.", "_save": "Guardar"}
        ).status_code
        == 403
    )
    assert client.get(reverse("admin:core_sourceidentity_add")).status_code == 403
    assert (
        client.get(
            reverse("admin:core_sourceidentity_entity_picker"),
            {"app_label": "core", "model_name": "sourceidentity", "field_name": "entity"},
        ).status_code
        == 403
    )
    assert SourceIdentity.objects.values().get(pk=observed.identity.pk) == original


def test_used_identity_can_be_reattested_after_reviewer_deactivation(
    client, observed, editor, identity_reviewer, organisation
):
    sync([observed])
    candidate = SourceObservation.objects.get()
    convert(candidate, editor, organisation)
    original_identity = SourceIdentity.objects.get(pk=observed.identity.pk)
    original_observation = SourceObservation.objects.values().get(pk=candidate.pk)
    original_relationship = Relationship.objects.values().get()
    original_evidence = Evidence.objects.values().get()
    editor.is_active = False
    editor.save()
    with pytest.raises(ValidationError):
        get_source_identity(source="ept", external_id=original_identity.external_id)
    with pytest.raises(ValidationError):
        sync([observed], day=DAY + timedelta(days=1))
    client.force_login(identity_reviewer)
    url = reverse("admin:core_sourceidentity_change", args=[original_identity.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert response.context["has_change_permission"]
    form = response.context["adminform"].form
    assert set(form.fields) == {"review_notes"}
    assert form.fields["review_notes"].required
    assert b'name="_save"' in response.content
    response = client.post(url, {"review_notes": "", "_save": "Guardar"})
    assert response.status_code == 200
    assert "review_notes" in response.context["adminform"].form.errors
    notes = "Correspondência original novamente conferida, sem mover afirmações."
    assert client.post(url, {"review_notes": notes, "_save": "Guardar"}).status_code == 302
    renewed = get_source_identity(source="ept", external_id=original_identity.external_id)
    assert renewed.reviewed_by_id == identity_reviewer.pk
    assert renewed.reviewed_at is not None
    assert original_identity.reviewed_at is not None
    assert renewed.reviewed_at > original_identity.reviewed_at
    assert renewed.review_notes == notes
    assert (renewed.source, renewed.external_id, renewed.entity_id, renewed.used_at) == (
        original_identity.source,
        original_identity.external_id,
        original_identity.entity_id,
        original_identity.used_at,
    )
    assert SourceObservation.objects.values().get(pk=candidate.pk) == original_observation
    assert Relationship.objects.values().get() == original_relationship
    assert Evidence.objects.values().get() == original_evidence
    sync([replace(observed, identity=renewed)], day=DAY + timedelta(days=1))
    candidate.refresh_from_db()
    assert candidate.identity_id == renewed.pk
    assert candidate.relationship_id == original_observation["relationship_id"]
    assert candidate.evidence_id == original_observation["evidence_id"]


def test_used_identity_admin_ignores_forged_mapping_and_attestation_fields(
    client, observed, identity_reviewer
):
    sync([observed])
    original = SourceIdentity.objects.get(pk=observed.identity.pk)
    replacement = Entity.objects.create(
        name="Pessoa que não deve receber afirmações", slug="pessoa-sem-afirmacoes", kind="person"
    )
    client.force_login(identity_reviewer)
    response = client.post(
        reverse("admin:core_sourceidentity_change", args=[original.pk]),
        {
            "source": "government",
            "external_id": "holder:forged",
            "entity": str(replacement.pk),
            "used_at": "",
            "reviewed_by": str(original.reviewed_by_id),
            "reviewed_at": "2000-01-01",
            "review_notes": "Revisão da correspondência original.",
            "_save": "Guardar",
        },
    )
    assert response.status_code == 302
    mapping = SourceIdentity.objects.get(pk=original.pk)
    assert (mapping.source, mapping.external_id, mapping.entity_id, mapping.used_at) == (
        original.source,
        original.external_id,
        original.entity_id,
        original.used_at,
    )
    assert mapping.reviewed_by_id == identity_reviewer.pk
    assert mapping.reviewed_at is not None
    assert original.reviewed_at is not None
    assert mapping.reviewed_at > original.reviewed_at
    assert SourceObservation.objects.get().identity.entity_id == original.entity_id


def test_identity_changed_after_fetch_requires_new_collection(observed):
    replacement = Entity.objects.create(
        name="Outra pessoa inteiramente fictícia", slug="outra-pessoa-ficticia", kind="person"
    )
    mapping = SourceIdentity.objects.get(pk=observed.identity.pk)
    mapping.entity = replacement
    mapping.save()
    with pytest.raises(ValidationError):
        sync([observed])
    assert not SourceObservation.objects.exists()
    assert not SourceSyncState.objects.exists()


def test_inactive_identity_reviewer_blocks_interests(observed, editor):
    editor.is_active = False
    editor.save()
    with pytest.raises(ValidationError):
        get_source_identity(source="ept", external_id=observed.identity.external_id)
    with pytest.raises(ValidationError):
        sync([observed])
    assert not SourceObservation.objects.exists()


def test_read_only_and_withdrawn_candidates_cannot_be_converted(client, observed, editor, reviewer):
    sync([observed])
    candidate = SourceObservation.objects.get()
    url = reverse("admin:core_sourceobservation_change", args=[candidate.pk])
    reviewer.is_staff = True
    reviewer.save()
    reviewer.user_permissions.add(Permission.objects.get(codename="view_sourceobservation"))
    client.force_login(reviewer)
    response = client.get(url)
    assert response.status_code == 200
    assert not response.context["has_change_permission"]
    assert client.post(url, {"_save": "Guardar"}).status_code == 403
    sync([], day=DAY + timedelta(days=1))
    client.force_login(editor)
    response = client.get(url)
    assert response.status_code == 200
    assert not response.context["has_change_permission"]
    assert client.post(url, {"_save": "Guardar"}).status_code == 403
    assert not Relationship.objects.exists()
