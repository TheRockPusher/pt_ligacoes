import django.db.models.deletion
from django.db import migrations, models


def scope_existing(apps, schema_editor):
    State = apps.get_model("core", "ParliamentImportState")
    Member = apps.get_model("core", "ParliamentMember")
    Record = apps.get_model("core", "ParliamentRecord")
    Record.objects.update(is_current=False)
    for member in Member.objects.exclude(current_record=None).iterator():
        record = Record.objects.select_related("relationship").get(pk=member.current_record_id)
        record.period_start = record.relationship.start_date
        record.is_current = member.is_current
        record.save(update_fields=["period_start", "is_current"])
    state = State.objects.filter(key="assembly").first()
    if state is not None:
        for code in Record.objects.order_by().values_list("legislature", flat=True).distinct():
            State.objects.get_or_create(
                key=code,
                defaults={
                    "institution_id": state.institution_id,
                    "roster_source_id": state.roster_source_id,
                    "biography_source_id": state.biography_source_id,
                    "as_of": state.as_of,
                },
            )
        State.objects.filter(key="assembly").delete()
    # Flush deferred foreign-key checks before removing the legacy pointer and constraints.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):
    dependencies = [("core", "0015_linking_contract")]
    operations = [
        migrations.RemoveConstraint(
            model_name="parliamentimportstate", name="parliament_single_import"
        ),
        migrations.AlterField(
            model_name="parliamentimportstate",
            name="key",
            field=models.CharField(
                editable=False, max_length=24, primary_key=True, serialize=False
            ),
        ),
        migrations.AlterField(
            model_name="parliamentimportstate",
            name="institution",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="core.entity"),
        ),
        migrations.AddField(
            model_name="parliamentrecord",
            name="period_start",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="parliamentrecord",
            name="is_current",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(scope_existing, migrations.RunPython.noop),
        migrations.RemoveField(model_name="parliamentmember", name="current_record"),
        migrations.RemoveConstraint(
            model_name="parliamentrecord", name="parliament_member_revision_unique"
        ),
        migrations.AddConstraint(
            model_name="parliamentrecord",
            constraint=models.UniqueConstraint(
                fields=("member", "legislature", "period_start"), name="parliament_period_unique"
            ),
        ),
        migrations.AlterField(
            model_name="parliamentstatusinterval",
            name="start",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RemoveConstraint(
            model_name="parliamentstatusinterval", name="parliament_status_interval_unique"
        ),
        migrations.AddConstraint(
            model_name="parliamentstatusinterval",
            constraint=models.UniqueConstraint(
                fields=("cadastro_id", "legislature", "status", "start", "end"),
                name="parliament_status_interval_unique",
            ),
        ),
    ]
