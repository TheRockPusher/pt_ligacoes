from dataclasses import replace
from datetime import date, timedelta

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.urls import include, path, reverse

from ligacoes.core.enrichment import (
    ObservationInput,
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
from ligacoes.core.services import withdraw_relationship
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


def test_office_holding_with_public_name_only_object_publishes(holder):
    target = Entity.objects.create(
        name="Instituto sem identificador",
        slug="instituto-sem-id",
        kind="organisation",
        is_public=True,
    )
    result = sync([office(holder, target)])
    assert (result["created"], result["drafts"], result["published"]) == (1, 1, 1)
    assert public_relationships().get().object == target


@pytest.mark.parametrize("kind", ["professional_activity", "declared_client", "shareholding"])
def test_declared_interests_publish_without_identity_review(holder, body, kind):
    result = sync(
        [office(holder, body, category="declared_interest", kind=kind, dataset="ept_declaracoes")]
    )
    assert result["published"] == 1
    assert public_relationships().get().kind == kind


def test_declared_interest_resolves_verified_nipc(holder, body):
    result = sync(
        [
            office(
                holder,
                None,
                category="declared_interest",
                kind="directorship",
                object_name=body.name,
                object_identifier="601234561",
                dataset="ept_declaracoes",
            )
        ]
    )
    assert result["published"] == 1
    assert SourceObservation.objects.get().object == body
    assert public_relationships().get().object == body


def test_declared_name_organisation_is_reused_across_observations(holder):
    first = office(
        holder,
        None,
        category="declared_interest",
        kind="declared_client",
        object_name="Companhia Inteiramente Fictícia, Lda.",
        dataset="ept_declaracoes",
    )
    second = replace(first, external_id="office:2")
    result = sync([first, second])
    assert result["published"] == 2
    identities = SourceIdentity.objects.filter(source="declared_name")
    assert identities.count() == 1
    assert set(public_relationships().values_list("object_id", flat=True)) == {
        identities.get().entity_id
    }
    assert sync([first, second], day=DAY + timedelta(days=1))["published"] == 0


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


def test_name_only_board_member_publishes_with_a_source_scoped_person(body):
    result = sync([name_only(body)], source="sioe", scope="entity:601234561")
    assert (result["created"], result["drafts"], result["published"]) == (1, 1, 1)
    observation = SourceObservation.objects.get()
    assert observation.identity is not None
    assert observation.evidence is not None
    assert observation.identity.source == "scoped_name"
    assert observation.identity.external_id.startswith("sioe:entity:601234561:")
    relationship = public_relationships().get()
    assert relationship.subject == observation.identity.entity
    assert relationship.object == body and relationship.kind == "directorship"
    assert observation.evidence.excerpt == observation.passage
    assert observation.evidence.source.is_public
    assert sync([name_only(body)], source="sioe", scope="entity:601234561")["created"] == 0


def test_admin_conversion_does_not_ask_to_rematch_a_scoped_person(client, editor, body):
    sync([name_only(body)], source="sioe", scope="entity:601234561")
    observation = SourceObservation.objects.get()
    client.force_login(editor)
    response = client.get(reverse("admin:core_sourceobservation_change", args=[observation.pk]))
    assert response.status_code == 200
    assert "reviewed_subject" not in response.context["adminform"].form.fields


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": ""},
        {"object": None},
        {"object": None, "object_name": ""},
    ],
)
def test_incomplete_observations_stay_private_and_are_counted(holder, body, changes):
    result = sync([office(holder, body, **changes)])
    assert result["skipped"] == 1 and result["published"] == 0
    assert SourceObservation.objects.get().relationship_id is None


def test_incompatible_kind_matrix_stays_private(holder):
    result = sync([office(holder, holder.entity)])
    assert result["skipped"] == 1
    assert SourceObservation.objects.get().relationship_id is None


def test_editor_withdrawal_survives_reimport_revision_change_and_return(holder, body, reviewer):
    item = office(holder, body, category="declared_interest", kind="declared_client")
    sync([item])
    claim = SourceObservation.objects.get().relationship
    assert claim is not None
    withdraw_relationship(claim, reviewer)
    assert sync([item], day=DAY + timedelta(days=1))["published"] == 0
    changed = replace(item, revision="revision-b", passage="Passagem fictícia corrigida.")
    assert sync([changed], day=DAY + timedelta(days=2))["published"] == 0
    sync([], day=DAY + timedelta(days=3))
    assert sync([item], day=DAY + timedelta(days=4))["published"] == 0
    claim.refresh_from_db()
    assert claim.status == "rejected" and not public_relationships().exists()


def test_change_cessation_and_return_republish_verifiable_observations(holder, body):
    item = office(holder, body, category="declared_interest", kind="directorship")
    sync([item])
    first = SourceObservation.objects.get()
    changed = replace(item, revision="revision-b", passage="Direção corrigida fictícia.")
    assert sync([changed], day=DAY + timedelta(days=1))["published"] == 1
    first.refresh_from_db()
    assert first.relationship is not None
    assert not first.is_current and first.relationship.status == "draft"
    sync([], day=DAY + timedelta(days=2))
    assert not public_relationships().exists()
    assert sync([item], day=DAY + timedelta(days=3))["published"] == 1
    assert public_relationships().get().pk == first.relationship_id


def test_staff_scoped_identity_is_shared_across_pages_of_one_government(body):
    item = replace(
        name_only(body), category="office_holding", kind="employment", dataset="gov_nomeacoes"
    )
    sync([item], source="government", scope="nominations:gc25:page-a")
    sync([item], source="government", scope="nominations:gc25:page-b")
    assert SourceIdentity.objects.filter(source="scoped_name").count() == 1
    assert public_relationships().values("subject").distinct().count() == 1


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


def test_election_scoped_identity_is_shared_within_one_legislature(body):
    item = replace(
        name_only(body), category="office_holding", kind="public_office", dataset="ar_atividades"
    )
    sync([item], source="parliament", scope="oex:XVI:activity-a")
    sync([item], source="parliament", scope="oex:XVI:activity-b")
    assert SourceIdentity.objects.filter(source="scoped_name").count() == 1
    sync([item], source="parliament", scope="oex:XVII:activity-a")
    # The office context corroborates this same person across legislatures.
    assert SourceIdentity.objects.filter(source="scoped_name").count() == 2
    assert public_relationships().values("subject").distinct().count() == 1


def test_snapshot_validation_failure_rolls_back_earlier_publications(holder, body):
    existing = office(holder, body)
    sync([existing])
    added = name_only(body)
    added = replace(added, dataset="ept_titulares", external_id="office:added")
    with pytest.raises(ValidationError):
        sync([added, replace(existing, passage="Passagem diferente na mesma revisão.")])
    assert SourceObservation.objects.count() == 1
    assert SourceIdentity.objects.filter(source="scoped_name").count() == 0
    assert public_relationships().count() == 1
