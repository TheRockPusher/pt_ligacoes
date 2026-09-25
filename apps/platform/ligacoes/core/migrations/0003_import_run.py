import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_parliament_import"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        verbose_name="identificador do pedido",
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("dry_run", "Validar sem aplicar"),
                            ("apply", "Aplicar rascunhos"),
                        ],
                        default="dry_run",
                        max_length=8,
                        verbose_name="modo",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Em fila"),
                            ("running", "Em execução"),
                            ("succeeded", "Concluída"),
                            ("failed", "Falhou"),
                        ],
                        default="queued",
                        max_length=9,
                        verbose_name="estado",
                    ),
                ),
                ("legislature", models.CharField(max_length=12, verbose_name="legislatura")),
                ("as_of", models.DateField(verbose_name="data de referência")),
                (
                    "origin",
                    models.CharField(
                        choices=[("admin", "Administração"), ("github", "GitHub")],
                        max_length=6,
                        verbose_name="origem",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="pedido em")),
                (
                    "started_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="iniciado em"),
                ),
                (
                    "finished_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="terminado em"),
                ),
                ("result", models.JSONField(blank=True, default=dict, verbose_name="resumo")),
                ("error", models.CharField(blank=True, max_length=240, verbose_name="erro")),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="import_runs",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="pedido por",
                    ),
                ),
            ],
            options={
                "verbose_name": "importação parlamentar",
                "verbose_name_plural": "importações parlamentares",
                "ordering": ["-created_at", "pk"],
                "permissions": [("run_import", "Pode executar importações parlamentares")],
                "constraints": [
                    models.UniqueConstraint(
                        models.Value(1),
                        condition=models.Q(status__in=["queued", "running"]),
                        name="import_single_active",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(mode__in=["dry_run", "apply"]),
                        name="import_run_mode_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(status__in=["queued", "running", "succeeded", "failed"]),
                        name="import_run_status_valid",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(origin="admin", requested_by__isnull=False)
                            | models.Q(origin="github", requested_by__isnull=True)
                        ),
                        name="import_run_origin_actor",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                status="queued",
                                started_at__isnull=True,
                                finished_at__isnull=True,
                                error="",
                            )
                            | models.Q(
                                status="running",
                                started_at__isnull=False,
                                finished_at__isnull=True,
                                error="",
                            )
                            | models.Q(
                                status="succeeded",
                                started_at__isnull=False,
                                finished_at__isnull=False,
                                error="",
                            )
                            | (
                                models.Q(
                                    status="failed",
                                    started_at__isnull=False,
                                    finished_at__isnull=False,
                                )
                                & ~models.Q(error="")
                            )
                        ),
                        name="import_run_lifecycle",
                    ),
                ],
            },
        ),
    ]
