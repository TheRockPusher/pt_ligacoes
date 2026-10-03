from datetime import date
from io import StringIO

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from ligacoes.core.identity import (
    AR_NIPC,
    ar_institution,
    ar_person,
    reconcile_identities,
    record_alias,
    reject_suggestion,
    resolve_person,
    scoped_person,
)
from ligacoes.core.models import (
    Entity,
    EntityAlias,
    EntityRedirect,
    Event,
    EventEntitySummary,
    EventParty,
    Evidence,
    IdentityDecision,
    IdentityMerge,
    IdentitySuggestion,
    ParliamentMember,
    ParliamentStatusInterval,
    Relationship,
    Source,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.public.selectors import public_events

pytestmark = pytest.mark.django_db
DAY = date(2026, 6, 3)
NAME = "Ana Clara Fictícia Ramos"
BASIS = "Nome publicado no registo oficial fictício."


def government():
    return Entity.objects.create(
        name="Governo Fictício",
        slug="fictional-government",
        kind="organisation",
        classification="government",
        is_public=True,
    )


def office(person, institution, *, start=DAY, status="published", role="Titular fictícia"):
    source = Source.objects.create(
        title="Fonte oficial fictícia",
        url="https://example.org/fictional-office",
        dataset="gov_composicao",
        is_public=True,
    )
    relationship = Relationship.objects.create(
        subject=person,
        object=institution,
        kind="public_office",
        start_date=start,
        role=role,
    )
    evidence = Evidence.objects.create(
        relationship=relationship,
        source=source,
        excerpt="Cargo documentado na fonte fictícia.",
        is_public=True,
    )
    Relationship.objects.filter(pk=relationship.pk).update(
        status=status,
        reviewed_at=timezone.now() if status == "published" else None,
    )
    return relationship, evidence


def suspension(person, *, cadastro="93001", status="Suspenso"):
    return ParliamentStatusInterval.objects.create(
        cadastro_id=cadastro,
        entity=person,
        legislature="XVII",
        status=status,
        start=DAY,
    )


def split_pair():
    parliament = ar_person("93001", NAME)
    minister = resolve_person("government", "fictional-minister", name=NAME, basis=BASIS)
    body = government()
    office(minister, body)
    return parliament, minister, body


def test_late_suspension_reconciles_four_source_profiles_with_claims_and_redirects():
    parliament, minister, body = split_pair()
    first_holder = resolve_person("ept", "fictional-holder-1", name=NAME, basis=BASIS)
    second_holder = resolve_person("ept", "fictional-holder-2", name=NAME, basis=BASIS)
    assembly = ar_institution()
    first_claim, first_evidence = office(first_holder, body)
    second_claim, second_evidence = office(second_holder, assembly)
    ParliamentMember.objects.create(cadastro_id="93001", entity=parliament, as_of=DAY)
    SourceIdentity.objects.filter(entity__in=[minister, first_holder, second_holder]).update(
        used_at=timezone.now(),
    )
    suspension(parliament)
    from_slugs = {minister.slug, first_holder.slug, second_holder.slug}
    before_claims = set(Relationship.objects.values_list("pk", flat=True))
    plan = reconcile_identities()
    assert len(plan) == 3
    assert Entity.objects.filter(kind="person", is_public=True).count() == 4
    assert not IdentityMerge.objects.exists()
    merges = reconcile_identities(apply=True)
    assert len(merges) == 3
    assert Entity.objects.filter(kind="person", is_public=True).get() == parliament
    assert set(
        SourceIdentity.objects.filter(entity=parliament).values_list("source", flat=True)
    ) == {
        "parliament",
        "government",
        "ept",
    }
    assert SourceIdentity.objects.filter(source="ept", entity=parliament).count() == 2
    assert set(Relationship.objects.values_list("pk", flat=True)) == before_claims
    for claim, evidence in ((first_claim, first_evidence), (second_claim, second_evidence)):
        claim.refresh_from_db()
        evidence.refresh_from_db()
        assert claim.subject == parliament and claim.status == "published"
        assert evidence.relationship == claim and evidence.is_public
    assert ParliamentMember.objects.get(cadastro_id="93001").entity == parliament
    assert (
        set(EntityRedirect.objects.filter(entity=parliament).values_list("old_slug", flat=True))
        == from_slugs
    )
    assert set(IdentityMerge.objects.values_list("from_slug", flat=True)) == from_slugs
    assert all(
        row.basis.startswith("Correspondência automática:") for row in IdentityMerge.objects.all()
    )
    assert reconcile_identities(apply=True) == []
    assert IdentityMerge.objects.count() == 3


def test_name_only_pairs_never_reconcile():
    first = ar_person("93001", NAME)
    second = resolve_person("government", "fictional-minister", name=NAME, basis=BASIS)
    assert reconcile_identities(apply=True) == []
    assert first != second and Entity.objects.filter(kind="person", is_public=True).count() == 2


def test_rejected_pair_remains_separate_after_late_corroboration(django_user_model):
    parliament, minister, _ = split_pair()
    suspension(parliament)
    editor = django_user_model.objects.create_user(username="fictional-merge-editor")
    editor.user_permissions.add(Permission.objects.get(codename="review_sourceidentity"))
    suggestion = IdentitySuggestion.objects.get(
        scheme="government",
        external_id="fictional-minister",
        candidate=parliament,
    )
    reject_suggestion(suggestion, editor)
    assert reconcile_identities(apply=True) == []
    assert SourceIdentity.objects.get(source="government").entity == minister
    assert not IdentityMerge.objects.exists()


def test_recorded_distinct_pair_blocks_reconciliation_in_either_orientation():
    parliament, minister, _ = split_pair()
    suspension(parliament)
    IdentityDecision.objects.create(
        first=minister,
        second=parliament,
        decision="distinct",
        basis="Deux personnes distinctes dans les sources officielles fictives.",
    )
    assert reconcile_identities(apply=True) == []
    assert not IdentityMerge.objects.exists()


def test_s4_links_ept_ar_office_to_elected_but_suspended_interval():
    parliament = ar_person("93001", NAME)
    holder = resolve_person("ept", "fictional-holder", name=NAME, basis=BASIS)
    office(holder, ar_institution())
    suspension(parliament, status="Suspenso (Eleito)")
    merges = reconcile_identities(apply=True)
    assert len(merges) == 1 and "S4" in merges[0].basis
    assert SourceIdentity.objects.get(source="ept").entity == parliament


def test_same_register_namesakes_are_not_merged_by_shared_ar_office():
    first = ar_person("93001", NAME)
    second = ar_person("93002", NAME)
    assembly = ar_institution()
    office(first, assembly)
    office(second, assembly)
    assert reconcile_identities(apply=True) == []
    assert Entity.objects.filter(kind="person", is_public=True).count() == 2


def test_register_ambiguity_blocks_ar_government_pairs():
    first = ar_person("93001", NAME)
    second = ar_person("93002", NAME)
    minister = resolve_person("government", "fictional-minister", name=NAME, basis=BASIS)
    office(minister, government())
    suspension(first)
    suspension(second, cadastro="93002")
    assert reconcile_identities(apply=True) == []
    assert Entity.objects.filter(kind="person", is_public=True).count() == 3


def test_alias_only_name_join_and_scoped_archive_link_to_stronger_anchor():
    parliament = ar_person("93001", NAME)
    archive = scoped_person("fictional:archive", "Ana Fictícia", basis=BASIS)
    record_alias(parliament, archive.name, scheme="parliament", external_id="93001")
    office(archive, government())
    suspension(parliament)
    assert len(reconcile_identities(apply=True)) == 1
    assert SourceIdentity.objects.get(source="scoped_name").entity == parliament
    assert EntityAlias.objects.filter(entity=parliament, normalised="ana ficticia").exists()


def test_ar_institution_attaches_its_exact_official_nipc():
    assembly = ar_institution()
    assert SourceIdentity.objects.get(source="nipc", external_id=AR_NIPC).entity == assembly
    assert ar_institution() == assembly


def test_existing_ar_nipc_organisation_merges_only_by_the_official_constant():
    duplicate = Entity.objects.create(
        name="Nome Fictício no Registo Fiscal",
        slug="fictional-ar-tax-record",
        kind="organisation",
        is_public=True,
    )
    SourceIdentity.objects.create(source="nipc", external_id=AR_NIPC, entity=duplicate)
    namesake = Entity.objects.create(
        name="Assembleia da República",
        slug="fictional-ar-namesake",
        kind="organisation",
        is_public=True,
    )
    assembly = ar_institution()
    claim, evidence = office(ar_person("93001", NAME), duplicate)
    assert SourceIdentity.objects.get(source="nipc", external_id=AR_NIPC).entity == duplicate
    assert len(reconcile_identities(apply=True)) == 1
    claim.refresh_from_db()
    evidence.refresh_from_db()
    assert claim.object == assembly and claim.status == "published"
    assert evidence.relationship == claim
    assert SourceIdentity.objects.get(source="nipc", external_id=AR_NIPC).entity == assembly
    assert Entity.objects.filter(pk=namesake.pk, is_public=True).exists()
    assert EntityRedirect.objects.get(old_slug=duplicate.slug).entity == assembly


def test_reconciliation_keeps_rejected_claims_and_separate_source_owned_claims():
    parliament, minister, body = split_pair()
    rejected, evidence = office(minister, body, status="rejected", role="Cargo retirado")
    same_claim, same_evidence = office(parliament, body)
    suspension(parliament)
    assert len(reconcile_identities(apply=True)) == 1
    rejected.refresh_from_db()
    same_claim.refresh_from_db()
    evidence.refresh_from_db()
    same_evidence.refresh_from_db()
    assert rejected.subject == parliament and rejected.status == "rejected"
    assert same_claim.subject == parliament and same_claim.status == "published"
    assert evidence.relationship == rejected and same_evidence.relationship == same_claim
    assert Relationship.objects.count() == 3


def test_self_link_is_withdrawn_and_keeps_hidden_historical_shell():
    parliament, minister, _ = split_pair()
    self_link = Relationship.objects.create(subject=parliament, object=minister, kind="family")
    Relationship.objects.filter(pk=self_link.pk).update(
        status="published", reviewed_at=timezone.now()
    )
    suspension(parliament)
    assert len(reconcile_identities(apply=True)) == 1
    self_link.refresh_from_db()
    minister.refresh_from_db()
    assert self_link.status == "rejected" and self_link.subject_id != self_link.object_id
    assert not minister.is_public
    assert EntityRedirect.objects.get(old_slug=minister.slug).entity == parliament


def test_same_anchor_rank_keeps_older_identity():
    body = government()
    first = resolve_person("ept", "fictional-holder-1", name=NAME, basis=BASIS)
    second = resolve_person("ept", "fictional-holder-2", name=NAME, basis=BASIS)
    office(first, body)
    office(second, body)
    assert reconcile_identities(apply=True)[0].to_entity == first
    assert SourceIdentity.objects.filter(source="ept", entity=first).count() == 2


def test_link_identities_command_is_dry_run_by_default():
    parliament, minister, _ = split_pair()
    suspension(parliament)
    output = StringIO()
    call_command("link_identities", stdout=output)
    assert minister.slug in output.getvalue() and parliament.slug in output.getvalue()
    assert "S3" in output.getvalue()
    assert not IdentityMerge.objects.exists()
    call_command("link_identities", apply=True, stdout=StringIO())
    assert IdentityMerge.objects.count() == 1


def test_merge_moves_event_parties_and_observation_references_and_rebuilds_summaries():
    parliament, minister, body = split_pair()
    suspension(parliament)
    identity = SourceIdentity.objects.get(source="government", entity=minister)
    source = Source.objects.create(
        title="Atividade fictícia",
        url="https://example.org/fictional-event",
        dataset="ar_atividades",
        is_public=True,
    )
    event = Event.objects.create(
        dataset="ar_atividades",
        scope="fictional",
        record_id="fictional-event",
        kind="hearing",
        title="Audição fictícia",
        date=DAY,
        source=source,
        fingerprint="a" * 64,
        as_of=DAY,
        retrieved_at=timezone.now(),
    )
    Event.objects.filter(pk=event.pk).update(status="published", published_at=timezone.now())
    party = EventParty.objects.create(event=event, entity=minister, role="attendee", name=NAME)
    EventParty.objects.create(event=event, entity=body, role="host", name=body.name)
    observation = SourceObservation.objects.create(
        source="government",
        scope="fictional",
        external_id="fictional-observation",
        revision="a",
        identity=identity,
        category="government_office",
        passage="Cargo oficial fictício.",
        source_url="https://example.org/fictional-office",
        publisher="Fonte fictícia",
        reference="Fictícia",
        title="Cargo fictício",
        as_of=DAY,
        object=body,
    )
    assert len(reconcile_identities(apply=True)) == 1
    party.refresh_from_db()
    observation.refresh_from_db()
    assert observation.identity is not None
    assert party.entity == parliament and observation.identity.entity == parliament
    assert EventEntitySummary.objects.get(entity=parliament).event_count == 1
    assert not EventEntitySummary.objects.filter(entity_id=minister.pk).exists()


def test_late_wikidata_hint_reconciles_already_mapped_ar_and_ep_profiles():
    from ligacoes.core.wikidata import CrosswalkSnapshot, apply_snapshot

    parliament = ar_person("93001", NAME)
    european = resolve_person("ep", "990001", name=NAME, basis=BASIS)
    assert parliament != european
    apply_snapshot(
        CrosswalkSnapshot(
            items={
                "Q990001": {
                    "parliament": frozenset({"93001"}),
                    "ep": frozenset({"990001"}),
                }
            },
            rows=2,
            dropped=0,
        )
    )
    hint = IdentitySuggestion.objects.get(
        scheme="ep",
        external_id="990001",
        candidate=parliament,
    )
    assert hint.name_as_published == "Wikidata Q990001"
    assert SourceIdentity.objects.get(source="ep").entity == european
    merges = reconcile_identities(apply=True)
    assert len(merges) == 1 and "S2" in merges[0].basis
    assert SourceIdentity.objects.get(source="ep").entity == parliament


def test_existing_distinct_decision_survives_a_merge_and_blocks_the_new_canonical_pair():
    parliament, minister, body = split_pair()
    third = resolve_person("ept", "fictional-third", name=NAME, basis=BASIS)
    office(third, body)
    suspension(parliament)
    IdentityDecision.objects.create(
        first=minister,
        second=third,
        decision="distinct",
        basis="Registos oficiais fictícios distinguem estas pessoas.",
    )
    reconcile_identities(apply=True)
    decision = IdentityDecision.objects.get()
    assert {decision.first_id, decision.second_id} == {parliament.pk, third.pk}
    assert SourceIdentity.objects.get(source="ept").entity == third


def test_merge_audit_cannot_be_edited_or_deleted():
    parliament, _, _ = split_pair()
    suspension(parliament)
    reconcile_identities(apply=True)
    audit = IdentityMerge.objects.get()
    audit.basis = "Alteração sem fundamento."
    with pytest.raises(ValidationError):
        audit.save()
    with pytest.raises(ValidationError):
        audit.delete()


def test_ar_nipc_reconciliation_never_republishes_a_hidden_entity_event():
    duplicate = Entity.objects.create(
        name="Entidade fiscal fictícia",
        slug="fictional-hidden-tax",
        kind="organisation",
        is_public=True,
    )
    SourceIdentity.objects.create(source="nipc", external_id=AR_NIPC, entity=duplicate)
    assembly = ar_institution()
    source = Source.objects.create(
        title="Fonte fictícia",
        url="https://example.org/fictional-event",
        dataset="ar_atividades",
        is_public=True,
    )
    event = Event.objects.create(
        dataset="ar_atividades",
        scope="fictional",
        record_id="fictional-hidden-event",
        kind="hearing",
        title="Audição fictícia",
        date=DAY,
        source=source,
        fingerprint="a" * 64,
        as_of=DAY,
        retrieved_at=timezone.now(),
    )
    EventParty.objects.create(event=event, entity=duplicate, role="attendee", name=duplicate.name)
    EventParty.objects.create(event=event, entity=assembly, role="host", name=assembly.name)
    Event.objects.filter(pk=event.pk).update(status="published", published_at=timezone.now())
    assert public_events().filter(pk=event.pk).exists()
    duplicate.is_public = False
    duplicate.save()
    assert not public_events().filter(pk=event.pk).exists()
    assert reconcile_identities(apply=True) == []
    assert not public_events().filter(pk=event.pk).exists()
    assert SourceIdentity.objects.get(source="nipc", external_id=AR_NIPC).entity == duplicate


def test_chained_merges_preserve_original_audit_targets_and_flatten_public_redirects():
    body = government()
    first = resolve_person("ept", "fictional-chain-1", name=NAME, basis=BASIS)
    second = resolve_person("ept", "fictional-chain-2", name=NAME, basis=BASIS)
    office(first, body)
    office(second, body)
    assert len(reconcile_identities(apply=True)) == 1
    audit = IdentityMerge.objects.get()
    assert audit.to_entity_id == first.pk
    parliament = ar_person("93001", NAME)
    office(parliament, body)
    assert len(reconcile_identities(apply=True)) == 1
    audit.refresh_from_db()
    assert audit.to_entity_id == first.pk
    assert Entity.objects.filter(pk=first.pk, is_public=False).exists()
    assert EntityRedirect.objects.get(old_slug=first.slug).entity == parliament
    assert EntityRedirect.objects.get(old_slug=second.slug).entity == parliament
    assert IdentityMerge.objects.get(from_slug=first.slug).to_entity == parliament
