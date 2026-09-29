from django.db import migrations
from django.db.models import Exists, OuterRef
from django.utils import timezone


def freeze_event_anchors(apps, schema_editor):
    """Anchors already relied on by published events become immutable (one UPDATE)."""
    SourceIdentity = apps.get_model("core", "SourceIdentity")
    EventParty = apps.get_model("core", "EventParty")
    # As in 0012: millions of existing events exceed the web statement timeout; this
    # lifts it for the migration transaction only.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = 0")
    relied = EventParty.objects.filter(entity_id=OuterRef("entity_id"), event__status="published")
    (
        SourceIdentity.objects.filter(used_at__isnull=True)
        .exclude(source="wikidata")
        .filter(Exists(relied))
        .update(used_at=timezone.now())
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0013_event_listing_index")]

    operations = [migrations.RunPython(freeze_event_anchors, migrations.RunPython.noop)]
