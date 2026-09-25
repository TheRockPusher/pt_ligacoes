from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from ligacoes.core.enrichment import (
    ObservationInput,
    backfill_biography_roles,
    convert_observation,
    sync_observations,
)
from ligacoes.core.models import (
    ParliamentMember,
    ParliamentRecord,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.services import publish_relationship
from ligacoes.public.selectors import public_evidence

pytestmark = pytest.mark.django_db
RETRIEVED = datetime(2025, 1, 15, 12, tzinfo=UTC)
BACKFILLED = datetime(2026, 9, 25, 12, tzinfo=UTC)


def test_offline_backfill_preserves_original_consultation_date(client, catalog, reviewer):
    reviewer.user_permissions.add(
        Permission.objects.get(content_type__app_label="core", codename="review_sourceobservation")
    )
    member = ParliamentMember.objects.create(
        cadastro_id="91001", entity=catalog.person, as_of=RETRIEVED.date()
    )
    record = ParliamentRecord.objects.create(
        member=member,
        fingerprint="f" * 64,
        legislature="XVII",
        as_of=RETRIEVED.date(),
        retrieved_at=RETRIEVED,
        data={
            "biography": {
                "CadCargosFuncoes": [
                    {
                        "FunId": "91002",
                        "FunAntiga": "S",
                        "FunDes": "Consultoria profissional numa companhia inteiramente fictícia.",
                    }
                ]
            }
        },
        roster_url="https://example.org/roster-ficticio",
        biography_url="https://example.org/biografia-ficticia",
        relationship=catalog.relation,
        evidence=catalog.evidence,
    )
    member.current_record = record
    member.save()

    with patch("ligacoes.core.enrichment.timezone.now", return_value=BACKFILLED):
        assert backfill_biography_roles([record], reviewer)["created"] == 1
        observation = SourceObservation.objects.get()
        assert observation.retrieved_at == RETRIEVED
        relationship = convert_observation(
            observation,
            reviewer,
            object=catalog.company,
            kind="professional_activity",
            description="Consultoria profissional documentada, sem datas conhecidas.",
            review_notes="Organização e atividade verificadas na passagem biográfica fictícia.",
        )
        observation.refresh_from_db()
        evidence = observation.evidence
        assert evidence is not None
        assert evidence.source.retrieved_at == RETRIEVED
        evidence.source.is_public = True
        evidence.source.save()
        evidence.is_public = True
        evidence.save()
        published = publish_relationship(relationship, reviewer)

    with patch(
        "ligacoes.core.enrichment.timezone.now", return_value=BACKFILLED + timedelta(days=30)
    ):
        assert backfill_biography_roles([record], reviewer) == {
            "created": 0,
            "changed": 0,
            "ceased": 0,
            "drafts": 0,
        }
    observation.refresh_from_db()
    relationship.refresh_from_db()
    assert observation.retrieved_at == RETRIEVED
    assert relationship.reviewed_at == published.reviewed_at
    assert public_evidence().get(pk=evidence.pk).source.retrieved_at == RETRIEVED
    response = client.get(reverse("public:evidence_detail", kwargs={"pk": evidence.pk}))
    assert response.status_code == 200
    assert '<time datetime="2025-01-15">15/01/2025</time>' in response.content.decode()
    assert '<time datetime="2026-09-25">25/09/2026</time>' not in response.content.decode()


@pytest.mark.parametrize("retrieved_at", [None, RETRIEVED])
def test_retrieval_metadata_does_not_change_substantive_revision(catalog, retrieved_at):
    identity = SourceIdentity.objects.create(
        source="parliament", external_id="91001", entity=catalog.person
    )
    item = ObservationInput(
        external_id="role:91002",
        revision="unchanged-role",
        identity=identity,
        category="biography_role",
        passage="Consultoria numa companhia fictícia.",
        source_url="https://example.org/biografia-ficticia",
        publisher="Editor fictício",
        reference="FunId=91002",
        title="Biografia fictícia",
        retrieved_at=retrieved_at,
    )
    with patch("ligacoes.core.enrichment.timezone.now", return_value=BACKFILLED):
        sync_observations(
            source="parliament",
            scope="member:91001",
            observations=(item,),
            as_of=BACKFILLED.date(),
        )
    observation = SourceObservation.objects.get()
    assert observation.retrieved_at == (retrieved_at or BACKFILLED)
    result = sync_observations(
        source="parliament",
        scope="member:91001",
        observations=(replace(item, retrieved_at=BACKFILLED + timedelta(days=1)),),
        as_of=BACKFILLED.date() + timedelta(days=1),
    )
    assert result == {"created": 0, "changed": 0, "ceased": 0, "drafts": 0}
    retained = SourceObservation.objects.get()
    assert retained.pk == observation.pk
    assert retained.revision == observation.revision
    assert retained.retrieved_at == observation.retrieved_at
