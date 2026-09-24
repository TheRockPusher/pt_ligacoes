import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import ligacoes.core.validators


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="Entity",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("slug", models.SlugField(max_length=160, unique=True)),
                ("name", models.CharField(max_length=240)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("person", "Pessoa"),
                            ("company", "Empresa"),
                            ("organisation", "Organização"),
                            ("university", "Universidade"),
                        ],
                        max_length=20,
                    ),
                ),
                ("is_public", models.BooleanField(default=False)),
                ("description", models.TextField(blank=True, max_length=10000)),
                ("private_notes", models.TextField(blank=True, max_length=20000)),
            ],
            options={
                "verbose_name": "entidade",
                "verbose_name_plural": "entidades",
                "ordering": ["name", "pk"],
                "indexes": [
                    models.Index(fields=["is_public", "name"], name="entity_public_name_idx")
                ],
            },
        ),
        migrations.CreateModel(
            name="Source",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("title", models.CharField(max_length=300)),
                (
                    "url",
                    models.URLField(
                        max_length=2048, validators=[ligacoes.core.validators.validate_source_url]
                    ),
                ),
                ("publisher", models.CharField(blank=True, max_length=240)),
                ("retrieved_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("is_public", models.BooleanField(default=False)),
                ("private_notes", models.TextField(blank=True, max_length=20000)),
            ],
            options={
                "verbose_name": "fonte",
                "verbose_name_plural": "fontes",
                "ordering": ["title", "pk"],
            },
        ),
        migrations.CreateModel(
            name="Relationship",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("employment", "Emprego"),
                            ("directorship", "Administração"),
                            ("shareholding", "Participação societária"),
                            ("membership", "Filiação"),
                            ("education", "Formação"),
                            ("family", "Relação familiar"),
                            ("public_office", "Cargo público"),
                        ],
                        max_length=24,
                    ),
                ),
                ("description", models.TextField(blank=True, max_length=10000)),
                ("start_date", models.DateField(blank=True, null=True)),
                ("end_date", models.DateField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Rascunho"),
                            ("published", "Publicado"),
                            ("rejected", "Rejeitado"),
                        ],
                        default="draft",
                        max_length=12,
                    ),
                ),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("private_notes", models.TextField(blank=True, max_length=20000)),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="outgoing_relationships",
                        to="core.entity",
                    ),
                ),
                (
                    "object",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="incoming_relationships",
                        to="core.entity",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviewed_relationships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "relação",
                "verbose_name_plural": "relações",
                "ordering": ["subject__name", "object__name", "pk"],
                "permissions": [("publish_relationship", "Pode rever e publicar relações")],
                "indexes": [
                    models.Index(
                        fields=["status", "start_date", "end_date"],
                        name="relation_status_dates_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=~models.Q(subject=models.F("object")),
                        name="relationship_distinct_entities",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(start_date__isnull=True)
                        | models.Q(end_date__isnull=True)
                        | models.Q(end_date__gte=models.F("start_date")),
                        name="relationship_valid_dates",
                    ),
                    models.CheckConstraint(
                        condition=~models.Q(status="published")
                        | (
                            models.Q(reviewed_by__isnull=False)
                            & models.Q(reviewed_at__isnull=False)
                        ),
                        name="published_relationship_reviewed",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Evidence",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("excerpt", models.TextField(max_length=20000)),
                ("page_reference", models.CharField(blank=True, max_length=160)),
                ("is_public", models.BooleanField(default=False)),
                (
                    "relationship",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="evidence",
                        to="core.relationship",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="evidence",
                        to="core.source",
                    ),
                ),
            ],
            options={
                "verbose_name": "evidência",
                "verbose_name_plural": "evidências",
                "ordering": ["pk"],
            },
        ),
        migrations.CreateModel(
            name="ReviewEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[("publish", "Publicação"), ("invalidate", "Revisão invalidada")],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "relationship",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="review_events",
                        to="core.relationship",
                    ),
                ),
                (
                    "reviewer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="review_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "evento de revisão",
                "verbose_name_plural": "eventos de revisão",
                "ordering": ["-created_at", "pk"],
            },
        ),
    ]
