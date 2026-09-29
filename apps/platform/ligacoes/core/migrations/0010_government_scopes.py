"""Government observations are scoped per Government: the old single scope was gc25.

Only the scope label changes, so claims, revisions and publication are untouched and the
next gc25 import continues the same observation history instead of duplicating it.
"""

from django.db import migrations

LEGACY_SCOPE = "current-government"
GC25_SCOPE = "government:gc25"


def _rename(apps, old: str, new: str) -> None:
    for name in ("SourceObservation", "SourceSyncState"):
        model = apps.get_model("core", name)
        rows = model.objects.filter(source="government")
        if rows.filter(scope=new).exists() and rows.filter(scope=old).exists():
            raise RuntimeError(f"Âmbitos governamentais {old!r} e {new!r} coexistem.")
        rows.filter(scope=old).update(scope=new)


def forwards(apps, schema_editor):
    _rename(apps, LEGACY_SCOPE, GC25_SCOPE)


def backwards(apps, schema_editor):
    _rename(apps, GC25_SCOPE, LEGACY_SCOPE)


class Migration(migrations.Migration):
    dependencies = [("core", "0009_linking_backfill")]

    operations = [migrations.RunPython(forwards, backwards)]
