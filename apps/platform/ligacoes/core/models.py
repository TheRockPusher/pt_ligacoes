import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import F, Q
from django.utils import timezone

from .validators import validate_source_url


@contextmanager
def editorial_transaction() -> Generator[None]:
    """Serialize supported editorial writes before they acquire any row locks."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Stable signed 32-bit namespace/resource keys: ASCII "PTLG" / "EDIT".
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [0x50544C47, 0x45444954])
        yield


def invalidate_relationships(queryset):
    """Withdraw reviewed facts under the caller's editorial transaction lock."""
    relationships = list(queryset.select_for_update(of=("self",)).order_by("pk"))
    for relationship in relationships:
        if relationship.status != Relationship.Status.PUBLISHED:
            continue
        Relationship.objects.filter(pk=relationship.pk).update(
            status="draft", reviewed_by=None, reviewed_at=None
        )
        ReviewEvent.objects.create(relationship=relationship, action=ReviewEvent.Action.INVALIDATE)


class Entity(models.Model):
    class Kind(models.TextChoices):
        PERSON = "person", "Pessoa"
        COMPANY = "company", "Empresa"
        ORGANISATION = "organisation", "Organização"
        UNIVERSITY = "university", "Universidade"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=160, unique=True)
    name = models.CharField(max_length=240)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    is_public = models.BooleanField(default=False)
    description = models.TextField(blank=True, max_length=10000)
    private_notes = models.TextField(blank=True, max_length=20000)

    class Meta:
        ordering: ClassVar[list[str]] = ["name", "pk"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["is_public", "name"], name="entity_public_name_idx")
        ]
        verbose_name = "entidade"
        verbose_name_plural = "entidades"

    def __str__(self):
        return self.name

    @editorial_transaction()
    def save(self, *args, **kwargs):
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        self.full_clean()
        if previous and any(
            getattr(previous, field) != getattr(self, field)
            for field in ("slug", "name", "kind", "description", "is_public")
        ):
            invalidate_relationships(Relationship.objects.filter(Q(subject=self) | Q(object=self)))
        return super().save(*args, **kwargs)


class Source(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=300)
    url = models.URLField(max_length=2048, validators=[validate_source_url])
    publisher = models.CharField(max_length=240, blank=True)
    retrieved_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    is_public = models.BooleanField(default=False)
    private_notes = models.TextField(blank=True, max_length=20000)

    class Meta:
        ordering: ClassVar[list[str]] = ["title", "pk"]
        verbose_name = "fonte"
        verbose_name_plural = "fontes"

    def __str__(self):
        return self.title

    @editorial_transaction()
    def save(self, *args, **kwargs):
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        self.full_clean()
        if previous and any(
            getattr(previous, field) != getattr(self, field)
            for field in ("title", "url", "publisher", "retrieved_at", "published_at", "is_public")
        ):
            invalidate_relationships(
                Relationship.objects.filter(
                    pk__in=Evidence.objects.filter(source=self).values("relationship_id")
                )
            )
        return super().save(*args, **kwargs)


class Relationship(models.Model):
    class Kind(models.TextChoices):
        EMPLOYMENT = "employment", "Emprego"
        DIRECTORSHIP = "directorship", "Administração"
        SHAREHOLDING = "shareholding", "Participação societária"
        MEMBERSHIP = "membership", "Filiação"
        EDUCATION = "education", "Formação"
        FAMILY = "family", "Relação familiar"
        PUBLIC_OFFICE = "public_office", "Cargo público"

    class Status(models.TextChoices):
        DRAFT = "draft", "Rascunho"
        PUBLISHED = "published", "Publicado"
        REJECTED = "rejected", "Rejeitado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject = models.ForeignKey(
        Entity, on_delete=models.PROTECT, related_name="outgoing_relationships"
    )
    object = models.ForeignKey(
        Entity, on_delete=models.PROTECT, related_name="incoming_relationships"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    description = models.TextField(blank=True, max_length=10000)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_relationships",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    private_notes = models.TextField(blank=True, max_length=20000)
    # Populated only by the public selector's Prefetch(to_attr="public_evidence").
    public_evidence: list["Evidence"]

    class Meta:
        ordering: ClassVar[list[str]] = ["subject__name", "object__name", "pk"]
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("publish_relationship", "Pode rever e publicar relações")
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["status", "start_date", "end_date"], name="relation_status_dates_idx"
            )
        ]
        constraints: ClassVar[list[models.CheckConstraint]] = [
            models.CheckConstraint(
                condition=~Q(subject=F("object")), name="relationship_distinct_entities"
            ),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True)
                | Q(end_date__isnull=True)
                | Q(end_date__gte=F("start_date")),
                name="relationship_valid_dates",
            ),
            models.CheckConstraint(
                condition=~Q(status="published")
                | (Q(reviewed_by__isnull=False) & Q(reviewed_at__isnull=False)),
                name="published_relationship_reviewed",
            ),
        ]
        verbose_name = "relação"
        verbose_name_plural = "relações"

    def __str__(self):
        return f"{self.subject} — {self.get_kind_display()} — {self.object}"

    @editorial_transaction()
    def save(self, *args, **kwargs):
        if args:
            raise TypeError("Relationship.save() requires keyword arguments.")
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        if self.status == self.Status.PUBLISHED and (
            not previous or previous.status != self.Status.PUBLISHED
        ):
            raise ValidationError("A publicação exige o serviço de revisão autorizado.")
        changed = previous and any(
            getattr(previous, field) != getattr(self, field)
            for field in (
                "subject_id",
                "object_id",
                "kind",
                "description",
                "start_date",
                "end_date",
                "status",
            )
        )
        if previous and previous.status == self.Status.PUBLISHED and changed:
            self.status = self.Status.DRAFT
            self.reviewed_by = None
            self.reviewed_at = None
            ReviewEvent.objects.create(relationship=self, action=ReviewEvent.Action.INVALIDATE)
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                    "status",
                    "reviewed_by",
                    "reviewed_at",
                }
        elif previous and previous.status == self.Status.PUBLISHED:
            # Attribution is immutable outside the publication service.
            self.reviewed_by_id = previous.reviewed_by_id
            self.reviewed_at = previous.reviewed_at
        else:
            self.reviewed_by = None
            self.reviewed_at = None
        self.full_clean()
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.subject_id and self.subject_id == self.object_id:
            raise ValidationError("Uma relação tem de ligar duas entidades distintas.")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("A data final não pode anteceder a data inicial.")


class Evidence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    relationship = models.ForeignKey(
        Relationship, on_delete=models.CASCADE, related_name="evidence"
    )
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="evidence")
    excerpt = models.TextField(max_length=20000)
    page_reference = models.CharField(max_length=160, blank=True)
    is_public = models.BooleanField(default=False)

    class Meta:
        ordering: ClassVar[list[str]] = ["pk"]
        verbose_name = "evidência"
        verbose_name_plural = "evidências"

    def __str__(self):
        return f"{self.source}: {self.page_reference or 'sem referência de página'}"

    @editorial_transaction()
    def save(self, *args, **kwargs):
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        self.full_clean()
        if not previous or any(
            getattr(previous, field) != getattr(self, field)
            for field in ("relationship_id", "source_id", "excerpt", "page_reference", "is_public")
        ):
            ids = {self.relationship_id}
            if previous:
                ids.add(previous.relationship_id)
            invalidate_relationships(Relationship.objects.filter(pk__in=ids))
        return super().save(*args, **kwargs)

    @editorial_transaction()
    def delete(self, *args, **kwargs):
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        if previous:
            invalidate_relationships(Relationship.objects.filter(pk=previous.relationship_id))
        return super().delete(*args, **kwargs)


class ReviewEvent(models.Model):
    class Action(models.TextChoices):
        PUBLISH = "publish", "Publicação"
        INVALIDATE = "invalidate", "Revisão invalidada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    relationship = models.ForeignKey(
        Relationship, on_delete=models.PROTECT, related_name="review_events"
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="review_events",
    )
    reviewer_id: int | None
    action = models.CharField(max_length=16, choices=Action.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "pk"]
        verbose_name = "evento de revisão"
        verbose_name_plural = "eventos de revisão"

    def __str__(self):
        return f"{self.relationship_id}: {self.get_action_display()}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("O histórico de revisão é imutável.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("O histórico de revisão é imutável.")


class ParliamentImportState(models.Model):
    """Stable importer-owned references; no name-based identity reconciliation."""

    key = models.CharField(max_length=24, primary_key=True, default="assembly", editable=False)
    institution = models.OneToOneField(Entity, on_delete=models.PROTECT)
    roster_source = models.ForeignKey(
        Source, on_delete=models.PROTECT, related_name="parliament_roster_imports"
    )
    biography_source = models.ForeignKey(
        Source, on_delete=models.PROTECT, related_name="parliament_biography_imports"
    )
    as_of = models.DateField()

    class Meta:
        constraints: ClassVar[list[models.CheckConstraint]] = [
            models.CheckConstraint(condition=Q(key="assembly"), name="parliament_single_import")
        ]

    def __str__(self):
        return f"Assembleia da República / {self.as_of}"


class ParliamentMember(models.Model):
    cadastro_id = models.CharField(max_length=20, primary_key=True)
    entity = models.OneToOneField(Entity, on_delete=models.PROTECT)
    current_record = models.OneToOneField(
        "ParliamentRecord",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for",
    )
    is_current = models.BooleanField(default=True)
    as_of = models.DateField()

    class Meta:
        ordering: ClassVar[list[str]] = ["cadastro_id"]
        verbose_name = "deputado importado"
        verbose_name_plural = "deputados importados"

    def __str__(self):
        return f"AR {self.cadastro_id}: {self.entity}"


class ParliamentRecord(models.Model):
    """Private, minimised source revision; editorial claims remain separate."""

    member = models.ForeignKey(ParliamentMember, on_delete=models.PROTECT, related_name="records")
    fingerprint = models.CharField(max_length=64)
    legislature = models.CharField(max_length=12)
    as_of = models.DateField()
    retrieved_at = models.DateTimeField(default=timezone.now)
    data = models.JSONField()
    roster_url = models.URLField(max_length=2048)
    biography_url = models.URLField(max_length=2048)
    relationship = models.OneToOneField(Relationship, on_delete=models.PROTECT)
    evidence = models.OneToOneField(Evidence, on_delete=models.PROTECT)

    class Meta:
        ordering: ClassVar[list[str]] = ["-retrieved_at", "pk"]
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["member", "fingerprint"], name="parliament_member_revision_unique"
            )
        ]
        verbose_name = "observação parlamentar"
        verbose_name_plural = "observações parlamentares"

    def __str__(self):
        return f"AR {self.member_id} / {self.legislature} / {self.as_of}"


class ImportRun(models.Model):
    class Mode(models.TextChoices):
        DRY_RUN = "dry_run", "Validar sem aplicar"
        APPLY = "apply", "Aplicar rascunhos"

    class Status(models.TextChoices):
        QUEUED = "queued", "Em fila"
        RUNNING = "running", "Em execução"
        SUCCEEDED = "succeeded", "Concluída"
        FAILED = "failed", "Falhou"

    class Origin(models.TextChoices):
        ADMIN = "admin", "Administração"
        GITHUB = "github", "GitHub"

    id = models.UUIDField("identificador do pedido", primary_key=True, editable=False)
    mode = models.CharField("modo", max_length=8, choices=Mode.choices, default=Mode.DRY_RUN)
    status = models.CharField("estado", max_length=9, choices=Status.choices, default=Status.QUEUED)
    legislature = models.CharField("legislatura", max_length=12)
    as_of = models.DateField("data de referência")
    origin = models.CharField("origem", max_length=6, choices=Origin.choices)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="pedido por",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="import_runs",
    )
    requested_by_id: int | None
    created_at = models.DateTimeField("pedido em", auto_now_add=True)
    started_at = models.DateTimeField("iniciado em", null=True, blank=True)
    finished_at = models.DateTimeField("terminado em", null=True, blank=True)
    result = models.JSONField("resumo", default=dict, blank=True)
    error = models.CharField("erro", max_length=240, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "pk"]
        verbose_name = "importação parlamentar"
        verbose_name_plural = "importações parlamentares"
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("run_import", "Pode executar importações parlamentares")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                models.Value(1),
                condition=Q(status__in=["queued", "running"]),
                name="import_single_active",
            ),
            models.CheckConstraint(
                condition=Q(mode__in=["dry_run", "apply"]), name="import_run_mode_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["queued", "running", "succeeded", "failed"]),
                name="import_run_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(origin="admin", requested_by__isnull=False)
                    | Q(origin="github", requested_by__isnull=True)
                ),
                name="import_run_origin_actor",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="queued", started_at__isnull=True, finished_at__isnull=True, error="")
                    | Q(
                        status="running",
                        started_at__isnull=False,
                        finished_at__isnull=True,
                        error="",
                    )
                    | Q(
                        status="succeeded",
                        started_at__isnull=False,
                        finished_at__isnull=False,
                        error="",
                    )
                    | (
                        Q(status="failed", started_at__isnull=False, finished_at__isnull=False)
                        & ~Q(error="")
                    )
                ),
                name="import_run_lifecycle",
            ),
        ]

    def __str__(self):
        return f"{self.id} — {self.get_status_display()}"
