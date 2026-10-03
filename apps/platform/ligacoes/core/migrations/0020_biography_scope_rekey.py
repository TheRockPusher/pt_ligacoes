"""Retire shared biography scopes without discarding editorial withdrawals."""

from django.db import migrations


def retire_legacy_scopes(apps, schema_editor):
    alias = schema_editor.connection.alias
    Observation = apps.get_model("core", "SourceObservation")
    Relationship = apps.get_model("core", "Relationship")
    Evidence = apps.get_model("core", "Evidence")
    ReviewEvent = apps.get_model("core", "ReviewEvent")
    legacy = Observation.objects.using(alias).filter(
        source="parliament", category="biography_role", scope__regex=r"^member:[^:]+$"
    )
    published = Relationship.objects.using(alias).filter(
        pk__in=legacy.values("relationship_id"), status="published"
    )
    # Audit only publications actually invalidated, making repeated applies harmless.
    ReviewEvent.objects.using(alias).bulk_create(
        [
            ReviewEvent(relationship_id=pk, action="invalidate")
            for pk in published.values_list("pk", flat=True)
        ],
        batch_size=1000,
    )
    published.update(status="draft", reviewed_by_id=None, reviewed_at=None)
    Evidence.objects.using(alias).filter(pk__in=legacy.values("evidence_id")).update(
        is_public=False
    )
    legacy.update(is_current=False, reviewed_by_id=None, reviewed_at=None)
    # Keep legacy sync dates as lower bounds for the new legislature scopes. Rejected
    # observations also remain: imports consult their stable subject/FunId across scopes.


class Migration(migrations.Migration):
    dependencies = [("core", "0019_refresh_state")]

    operations = [migrations.RunPython(retire_legacy_scopes, migrations.RunPython.noop)]
