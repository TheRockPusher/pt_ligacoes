"""Publish Parliament mandates imported as drafts before automatic publication existed.

Only current, untouched imports qualify: a relationship with any review event was
published, invalidated or withdrawn by an editor and is left alone.
"""

from django.db import migrations
from django.utils import timezone


def publish_existing(apps, schema_editor):
    Entity = apps.get_model("core", "Entity")
    Evidence = apps.get_model("core", "Evidence")
    ParliamentImportState = apps.get_model("core", "ParliamentImportState")
    ParliamentMember = apps.get_model("core", "ParliamentMember")
    Relationship = apps.get_model("core", "Relationship")
    ReviewEvent = apps.get_model("core", "ReviewEvent")
    Source = apps.get_model("core", "Source")

    records = [
        member.current_record
        for member in ParliamentMember.objects.filter(
            is_current=True, current_record__isnull=False
        ).select_related("current_record__relationship", "current_record__evidence")
        if member.current_record.relationship.status == "draft"
        and not ReviewEvent.objects.filter(
            relationship_id=member.current_record.relationship_id
        ).exists()
    ]
    if not records:
        return
    state = ParliamentImportState.objects.filter(key="assembly").first()
    entity_ids = {record.relationship.subject_id for record in records}
    if state is not None:
        entity_ids.add(state.institution_id)
    Entity.objects.filter(pk__in=entity_ids).update(is_public=True)
    evidence_ids = [record.evidence_id for record in records]
    Source.objects.filter(evidence__pk__in=evidence_ids).update(is_public=True)
    Evidence.objects.filter(pk__in=evidence_ids).update(is_public=True)
    now = timezone.now()
    relationship_ids = [record.relationship_id for record in records]
    Relationship.objects.filter(pk__in=relationship_ids).update(
        status="published", reviewed_by=None, reviewed_at=now
    )
    ReviewEvent.objects.bulk_create(
        ReviewEvent(relationship_id=pk, action="auto_publish") for pk in relationship_ids
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0005_auto_publication")]

    operations = [migrations.RunPython(publish_existing, migrations.RunPython.noop)]
