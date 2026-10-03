import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0020_biography_scope_rekey")]

    operations = [
        migrations.CreateModel(
            name="EntityRedirect",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("old_slug", models.SlugField(max_length=160, unique=True)),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="redirects",
                        to="core.entity",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="IdentityMerge",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("from_slug", models.SlugField(max_length=160)),
                ("from_name", models.CharField(max_length=240)),
                ("basis", models.TextField(max_length=2000)),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                (
                    "to_entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="identity_merges",
                        to="core.entity",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="IdentityDecision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "decision",
                    models.CharField(choices=[("distinct", "Pessoas distintas")], max_length=16),
                ),
                ("basis", models.TextField(max_length=2000)),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                (
                    "first",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.entity",
                    ),
                ),
                (
                    "second",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.entity",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=~models.Q(first=models.F("second")),
                        name="identity_decision_distinct",
                    ),
                ]
            },
        ),
    ]
