"""Replace raw-JSON Parliament roster excerpts with the readable passage.

Rewrites only excerpts still equal to the importer's former JSON rendering, so
editor-changed passages are kept. Uses queryset updates: the wording changes,
not the documented facts, so publication is not invalidated.
"""

from django.db import migrations

from ligacoes.core.parliament_import import roster_passage
from ligacoes.core.parliament_parse import canonical_json


def rewrite(apps, schema_editor):
    Evidence = apps.get_model("core", "Evidence")
    ParliamentRecord = apps.get_model("core", "ParliamentRecord")
    for record in ParliamentRecord.objects.select_related("evidence").iterator():
        roster = record.data.get("roster")
        if roster and record.evidence.excerpt == canonical_json(roster):
            Evidence.objects.filter(pk=record.evidence_id).update(excerpt=roster_passage(roster))


class Migration(migrations.Migration):
    dependencies = [("core", "0006_publish_existing_parliament_imports")]

    operations = [migrations.RunPython(rewrite, migrations.RunPython.noop)]
