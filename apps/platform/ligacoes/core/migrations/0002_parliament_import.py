import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="ParliamentImportState",
            fields=[
                (
                    "key",
                    models.CharField(
                        default="assembly",
                        editable=False,
                        max_length=24,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("as_of", models.DateField()),
                (
                    "institution",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="core.entity"
                    ),
                ),
                (
                    "roster_source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="parliament_roster_imports",
                        to="core.source",
                    ),
                ),
                (
                    "biography_source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="parliament_biography_imports",
                        to="core.source",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(key="assembly"), name="parliament_single_import"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ParliamentMember",
            fields=[
                ("cadastro_id", models.CharField(max_length=20, primary_key=True, serialize=False)),
                ("is_current", models.BooleanField(default=True)),
                ("as_of", models.DateField()),
                (
                    "entity",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="core.entity"
                    ),
                ),
            ],
            options={
                "ordering": ["cadastro_id"],
                "verbose_name": "deputado importado",
                "verbose_name_plural": "deputados importados",
            },
        ),
        migrations.CreateModel(
            name="ParliamentRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("fingerprint", models.CharField(max_length=64)),
                ("legislature", models.CharField(max_length=12)),
                ("as_of", models.DateField()),
                ("retrieved_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("data", models.JSONField()),
                ("roster_url", models.URLField(max_length=2048)),
                ("biography_url", models.URLField(max_length=2048)),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="records",
                        to="core.parliamentmember",
                    ),
                ),
                (
                    "relationship",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="core.relationship"
                    ),
                ),
                (
                    "evidence",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="core.evidence"
                    ),
                ),
            ],
            options={
                "ordering": ["-retrieved_at", "pk"],
                "verbose_name": "observação parlamentar",
                "verbose_name_plural": "observações parlamentares",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("member", "fingerprint"), name="parliament_member_revision_unique"
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="parliamentmember",
            name="current_record",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="current_for",
                to="core.parliamentrecord",
            ),
        ),
    ]
