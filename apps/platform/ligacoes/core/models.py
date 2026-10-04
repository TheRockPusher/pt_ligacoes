import calendar
import datetime
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import F, OrderBy, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .validators import valid_nipc, validate_source_url

IMPORT_TIMEOUT = "15min"
IMPORT_LOCK_TIMEOUT = "30min"


@dataclass(slots=True)
class _BulkEditorialScope:
    connection: object
    active: bool = True


_bulk_editorial_scope: ContextVar[_BulkEditorialScope | None] = ContextVar(
    "bulk_editorial_scope", default=None
)


@contextmanager
def editorial_transaction(*, long_running: bool = False) -> Generator[None]:
    """Serialize supported editorial writes before they acquire any row locks.

    ``long_running`` lifts the web-request timeouts for this transaction only.
    Nested bulk writes on the same connection join the import without subtransactions.
    Any nested write failure invalidates that import, even if its caller catches it.
    """
    scope = _bulk_editorial_scope.get()
    nested_bulk = (
        scope is not None
        and scope.active
        and connection.in_atomic_block
        and scope.connection is connection.connection
    )
    with transaction.atomic(savepoint=not nested_bulk):
        if not nested_bulk:
            with connection.cursor() as cursor:
                if long_running:
                    # Waiting for the advisory lock is bounded by lock_timeout, not by the
                    # (shorter) per-statement import timeout set once the lock is held.
                    cursor.execute(f"SET LOCAL statement_timeout = '{IMPORT_LOCK_TIMEOUT}'")
                    cursor.execute(
                        f"SET LOCAL idle_in_transaction_session_timeout = '{IMPORT_TIMEOUT}'"
                    )
                    cursor.execute(f"SET LOCAL lock_timeout = '{IMPORT_LOCK_TIMEOUT}'")
                # Stable signed 32-bit namespace/resource keys: ASCII "PTLG" / "EDIT".
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [0x50544C47, 0x45444954])
                if long_running:
                    cursor.execute(f"SET LOCAL statement_timeout = '{IMPORT_TIMEOUT}'")
        owned_scope = (
            _BulkEditorialScope(connection.connection) if long_running and not nested_bulk else None
        )
        token = _bulk_editorial_scope.set(owned_scope) if owned_scope is not None else None
        try:
            yield
            if token is not None and connection.needs_rollback:
                raise transaction.TransactionManagementError(
                    "Uma escrita falhada invalida a importação completa."
                )
        finally:
            if owned_scope is not None:
                owned_scope.active = False
            if token is not None:
                _bulk_editorial_scope.reset(token)


def import_transaction():
    """Editorial transaction for bulk official-source imports (long timeouts)."""
    return editorial_transaction(long_running=True)


def invalidate_relationships(queryset):
    """Withdraw published facts under the caller's editorial transaction lock."""
    relationships = list(queryset.select_for_update(of=("self",)).order_by("pk"))
    for relationship in relationships:
        if relationship.status != Relationship.Status.PUBLISHED:
            continue
        Relationship.objects.filter(pk=relationship.pk).update(
            status="draft", reviewed_by=None, reviewed_at=None
        )
        ReviewEvent.objects.create(relationship=relationship, action=ReviewEvent.Action.INVALIDATE)


class DatePrecision(models.TextChoices):
    DAY = "day", "Dia"
    MONTH = "month", "Mês"
    YEAR = "year", "Ano"


class TemporalStatus(models.TextChoices):
    CURRENT = "current", "Em curso"
    ENDED = "ended", "Terminada"
    UNKNOWN = "unknown", "Desconhecida"


def validate_date_precision(
    start: datetime.date | None,
    start_precision: str,
    end: datetime.date | None,
    end_precision: str,
    *,
    start_field: str,
    end_field: str,
) -> None:
    """Month/year starts are the first day and ends the last day of their period."""
    errors: dict[str, str] = {}
    if start is not None:
        if start_precision == DatePrecision.MONTH and start.day != 1:
            errors[start_field] = "Com precisão de mês, a data inicial deve ser o dia 1."
        elif start_precision == DatePrecision.YEAR and (start.month, start.day) != (1, 1):
            errors[start_field] = "Com precisão de ano, a data inicial deve ser 1 de janeiro."
    if end is not None:
        last_day = calendar.monthrange(end.year, end.month)[1]
        if end_precision == DatePrecision.MONTH and end.day != last_day:
            errors[end_field] = "Com precisão de mês, a data final deve ser o último dia do mês."
        elif end_precision == DatePrecision.YEAR and (end.month, end.day) != (12, 31):
            errors[end_field] = "Com precisão de ano, a data final deve ser 31 de dezembro."
    if errors:
        raise ValidationError(errors)


class Entity(models.Model):
    class Kind(models.TextChoices):
        PERSON = "person", "Pessoa"
        COMPANY = "company", "Empresa"
        ORGANISATION = "organisation", "Organização"
        UNIVERSITY = "university", "Universidade"

    class Classification(models.TextChoices):
        PARLIAMENT = "parliament", "Parlamento"
        PARLIAMENTARY_GROUP = "parliamentary_group", "Grupo parlamentar"
        PARLIAMENTARY_COMMITTEE = "parliamentary_committee", "Comissão parlamentar"
        PARLIAMENTARY_BODY = "parliamentary_body", "Órgão parlamentar"
        PARLIAMENTARY_DELEGATION = "parliamentary_delegation", "Delegação parlamentar"
        FRIENDSHIP_GROUP = "friendship_group", "Grupo parlamentar de amizade"
        GOVERNMENT = "government", "Governo"
        GOVERNMENT_DEPARTMENT = "government_department", "Área governativa"
        GOVERNMENT_OFFICE = "government_office", "Gabinete governamental"
        PUBLIC_BODY = "public_body", "Organismo público"
        REGULATOR = "regulator", "Entidade reguladora"
        STATE_COMPANY = "state_company", "Empresa pública"
        MUNICIPALITY = "municipality", "Município"
        PARISH = "parish", "Freguesia"
        EU_INSTITUTION = "eu_institution", "Instituição europeia"
        COMPANY = "company", "Empresa"
        FOUNDATION = "foundation", "Fundação"
        ASSOCIATION = "association", "Associação"
        COOPERATIVE = "cooperative", "Cooperativa"
        HIGHER_EDUCATION = "higher_education", "Ensino superior"
        INTERNATIONAL_ORGANISATION = "international_organisation", "Organização internacional"
        OTHER = "other", "Outra"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=160, unique=True)
    name = models.CharField(max_length=240)
    normalised_name = models.CharField(max_length=300, db_index=True, editable=False, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    classification = models.CharField(
        "classificação", max_length=32, choices=Classification.choices, blank=True
    )
    founding_date = models.DateField("data de fundação", null=True, blank=True)
    dissolution_date = models.DateField("data de extinção", null=True, blank=True)
    is_public = models.BooleanField(default=False)
    description = models.TextField(blank=True, max_length=10000)
    private_notes = models.TextField(blank=True, max_length=20000)

    class Meta:
        ordering: ClassVar[list[str]] = ["name", "pk"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["is_public", "name"], name="entity_public_name_idx")
        ]
        constraints: ClassVar[list[models.CheckConstraint]] = [
            # Mirrors KIND_CLASSIFICATIONS; added after the 0009 backfill.
            models.CheckConstraint(
                condition=Q(kind="person", classification="")
                | Q(kind="company", classification__in=["company", "state_company"])
                | Q(kind="university", classification="higher_education")
                | (
                    Q(kind="organisation")
                    & ~Q(
                        classification__in=[
                            "",
                            "company",
                            "state_company",
                            "higher_education",
                        ]
                    )
                ),
                name="entity_kind_classification",
            )
        ]
        verbose_name = "entidade"
        verbose_name_plural = "entidades"

    def __str__(self):
        return self.name

    @editorial_transaction()
    def save(self, *args, **kwargs):
        from .identity import normalise_name

        self.normalised_name = normalise_name(self.name)
        if kwargs.get("update_fields") is not None and "name" in kwargs["update_fields"]:
            kwargs["update_fields"] = {*kwargs["update_fields"], "normalised_name"}
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        self.fill_default_classification()
        self.full_clean()
        if previous and any(
            getattr(previous, field) != getattr(self, field)
            for field in ("slug", "name", "kind", "classification", "description", "is_public")
        ):
            invalidate_relationships(Relationship.objects.filter(Q(subject=self) | Q(object=self)))
        saved = super().save(*args, **kwargs)
        if previous and previous.is_public != self.is_public:
            # Hiding or showing an entity moves every event it takes part in, and so the
            # summaries of everyone who shares one.
            from .event_summaries import co_party_entities, rebuild_event_summaries

            rebuild_event_summaries(entities=co_party_entities([self.pk]))
        return saved

    def fill_default_classification(self) -> None:
        """Give a non-person without a classification the default for its kind."""
        if not self.classification:
            self.classification = DEFAULT_CLASSIFICATIONS.get(self.kind, "")

    def clean(self):
        super().clean()
        self.fill_default_classification()
        allowed = KIND_CLASSIFICATIONS.get(self.kind)
        if allowed is not None and self.classification not in allowed:
            raise ValidationError(
                {
                    "classification": "Uma pessoa não tem classificação."
                    if self.kind == self.Kind.PERSON
                    else "Esta classificação não é compatível com o tipo de entidade."
                }
            )
        if (
            self.founding_date
            and self.dissolution_date
            and self.dissolution_date < self.founding_date
        ):
            raise ValidationError({"dissolution_date": "A extinção não pode anteceder a fundação."})


class EntityRedirect(models.Model):
    """Permanent profile URL preservation after an audited identity merge."""

    old_slug = models.SlugField(max_length=160, unique=True)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="redirects")

    def __str__(self):
        return f"{self.old_slug} -> {self.entity_id}"


class IdentityMerge(models.Model):
    """Immutable provenance of an automatically corroborated identity reconciliation."""

    from_slug = models.SlugField(max_length=160)
    from_name = models.CharField(max_length=240)
    to_entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="identity_merges")
    basis = models.TextField(max_length=2000)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return f"{self.from_slug} -> {self.to_entity_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("O histórico de reconciliação é imutável.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("O histórico de reconciliação é imutável.")


class IdentityDecision(models.Model):
    """An explicit distinct-person decision that automatic reconciliation must respect."""

    first = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    second = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    decision = models.CharField(max_length=16, choices=[("distinct", "Pessoas distintas")])
    basis = models.TextField(max_length=2000)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=~Q(first=F("second")), name="identity_decision_distinct"
            ),
        ]

    def __str__(self):
        return f"{self.first_id} / {self.second_id}: {self.decision}"


DEFAULT_CLASSIFICATIONS: dict[str, str] = {
    Entity.Kind.COMPANY: Entity.Classification.COMPANY,
    Entity.Kind.UNIVERSITY: Entity.Classification.HIGHER_EDUCATION,
    Entity.Kind.ORGANISATION: Entity.Classification.OTHER,
}
KIND_CLASSIFICATIONS: dict[str, frozenset[str]] = {
    Entity.Kind.PERSON: frozenset({""}),
    Entity.Kind.COMPANY: frozenset(
        {Entity.Classification.COMPANY, Entity.Classification.STATE_COMPANY}
    ),
    Entity.Kind.UNIVERSITY: frozenset({Entity.Classification.HIGHER_EDUCATION}),
    Entity.Kind.ORGANISATION: frozenset(Entity.Classification)
    - {
        Entity.Classification.COMPANY,
        Entity.Classification.STATE_COMPANY,
        Entity.Classification.HIGHER_EDUCATION,
    },
}


class Term(models.Model):
    class Kind(models.TextChoices):
        LEGISLATURE = "legislature", "Legislatura"
        GOVERNMENT = "government", "Governo"
        EP_TERM = "ep_term", "Legislatura europeia"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField("tipo", max_length=16, choices=Kind.choices)
    code = models.CharField("código", max_length=24)
    label = models.CharField("designação", max_length=160)
    institution = models.ForeignKey(
        Entity, verbose_name="instituição", on_delete=models.PROTECT, related_name="terms"
    )
    start_date = models.DateField("início")
    end_date = models.DateField("fim", null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["kind", "start_date"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["kind", "code"], name="term_kind_code_unique"),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="term_valid_dates",
            ),
        ]
        verbose_name = "mandato"
        verbose_name_plural = "mandatos"

    def __str__(self):
        return self.label

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("A data final não pode anteceder a data inicial.")


class Source(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=300)
    url = models.URLField(max_length=2048, validators=[validate_source_url])
    publisher = models.CharField(max_length=240, blank=True)
    retrieved_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    is_public = models.BooleanField(default=False)
    private_notes = models.TextField(blank=True, max_length=20000)
    dataset = models.CharField("conjunto de dados", max_length=64, blank=True, db_index=True)

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
        saved = super().save(*args, **kwargs)
        if previous and previous.is_public != self.is_public:
            from .event_summaries import rebuild_event_summaries

            datasets = Event.objects.filter(source=self).values_list("dataset", flat=True)
            for dataset in datasets.order_by().distinct():
                rebuild_event_summaries(dataset=dataset)
        return saved


class Relationship(models.Model):
    class Kind(models.TextChoices):
        EMPLOYMENT = "employment", "Emprego"
        DIRECTORSHIP = "directorship", "Administração"
        SHAREHOLDING = "shareholding", "Participação societária"
        MEMBERSHIP = "membership", "Filiação"
        EDUCATION = "education", "Formação"
        FAMILY = "family", "Relação familiar"
        PUBLIC_OFFICE = "public_office", "Cargo público"
        PROFESSIONAL_ACTIVITY = "professional_activity", "Atividade profissional"
        PART_OF = "part_of", "Integra"
        SUCCESSION = "succession", "Sucede a"
        # A client named in a person's own declaration of interests (e.g. EpT "Outras situações").
        DECLARED_CLIENT = "declared_client", "Cliente declarado"

    class RoleClass(models.TextChoices):
        LEADERSHIP = "leadership", "Presidência ou direção"
        DEPUTY_LEADERSHIP = "deputy_leadership", "Vice-presidência ou coordenação"
        MEMBER = "member", "Membro"
        SUBSTITUTE = "substitute", "Suplente"
        STAFF = "staff", "Gabinete ou apoio"
        OTHER = "other", "Outra função"

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
    role = models.CharField("cargo ou função (fonte)", max_length=240, blank=True)
    role_class = models.CharField(
        "classe da função", max_length=24, choices=RoleClass.choices, blank=True
    )
    term = models.ForeignKey(
        Term,
        verbose_name="mandato",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="relationships",
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    start_precision = models.CharField(
        "precisão da data inicial",
        max_length=5,
        choices=DatePrecision.choices,
        default=DatePrecision.DAY,
    )
    end_precision = models.CharField(
        "precisão da data final",
        max_length=5,
        choices=DatePrecision.choices,
        default=DatePrecision.DAY,
    )
    temporal_status = models.CharField(
        "estado temporal",
        max_length=8,
        choices=TemporalStatus.choices,
        default=TemporalStatus.UNKNOWN,
    )
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
            # A null reviewer with a timestamp records automatic official-source publication.
            models.CheckConstraint(
                condition=~Q(status="published") | Q(reviewed_at__isnull=False),
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
                "role",
                "role_class",
                "term_id",
                "start_date",
                "end_date",
                "start_precision",
                "end_precision",
                "temporal_status",
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
        validate_date_precision(
            self.start_date,
            self.start_precision,
            self.end_date,
            self.end_precision,
            start_field="start_date",
            end_field="end_date",
        )
        if self.kind and self.subject_id and self.object_id:
            validate_kind_matrix(self.kind, self.subject.kind, self.object.kind)


def validate_kind_matrix(kind: str, subject_kind: str, object_kind: str) -> None:
    """Which ends each relationship kind may connect."""
    subject_person = subject_kind == Entity.Kind.PERSON
    object_person = object_kind == Entity.Kind.PERSON
    if kind == Relationship.Kind.FAMILY:
        valid = subject_person and object_person
        message = "Uma relação familiar liga duas pessoas."
    elif kind in {Relationship.Kind.PART_OF, Relationship.Kind.SUCCESSION}:
        valid = not subject_person and not object_person
        message = "Esta relação liga duas organizações."
    elif kind in {Relationship.Kind.MEMBERSHIP, Relationship.Kind.SHAREHOLDING}:
        valid = not object_person
        message = "O destino desta relação deve ser uma organização."
    else:
        valid = subject_person and not object_person
        message = "Esta relação liga uma pessoa a uma organização."
    if not valid:
        raise ValidationError({"kind": message})


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
        AUTO_PUBLISH = "auto_publish", "Publicação automática"
        WITHDRAW = "withdraw", "Publicação retirada"
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
    """Complete snapshot currency and sources, independently scoped by legislature."""

    key = models.CharField(max_length=24, primary_key=True, editable=False)
    institution = models.ForeignKey(Entity, on_delete=models.PROTECT)
    roster_source = models.ForeignKey(
        Source, on_delete=models.PROTECT, related_name="parliament_roster_imports"
    )
    biography_source = models.ForeignKey(
        Source, on_delete=models.PROTECT, related_name="parliament_biography_imports"
    )
    as_of = models.DateField()

    def __str__(self):
        return f"Assembleia da República / {self.as_of}"


class ParliamentMember(models.Model):
    cadastro_id = models.CharField(max_length=20, primary_key=True)
    entity = models.OneToOneField(Entity, on_delete=models.PROTECT)
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
    period_start = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=True)
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
                fields=["member", "legislature", "period_start"], name="parliament_period_unique"
            )
        ]
        verbose_name = "observação parlamentar"
        verbose_name_plural = "observações parlamentares"

    def __str__(self):
        return f"AR {self.member_id} / {self.legislature} / {self.as_of}"


class ImportRun(models.Model):
    class Mode(models.TextChoices):
        DRY_RUN = "dry_run", "Validar sem aplicar"
        APPLY = "apply", "Aplicar e publicar"

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


class EnrichmentSource(models.TextChoices):
    GOVERNMENT = "government", "Governo"
    EPT = "ept", "Entidade para a Transparência"
    PARLIAMENT = "parliament", "Assembleia da República"
    SIOE = "sioe", "SIOE+ (DGAEP)"
    GLEIF = "gleif", "GLEIF"
    EP = "ep", "Parlamento Europeu"
    ETF = "etf", "Entidade do Tesouro e Finanças"


class IdentityScheme(models.TextChoices):
    GOVERNMENT = "government", "Governo"
    EPT = "ept", "Entidade para a Transparência"
    PARLIAMENT = "parliament", "Assembleia da República"
    SIOE = "sioe", "SIOE+"
    EP = "ep", "Parlamento Europeu"
    NIPC = "nipc", "NIPC"
    LEI = "lei", "LEI (GLEIF)"
    EU_TR = "eu_tr", "Registo de Transparência da UE"
    EC = "ec", "Comissão Europeia"
    WIKIDATA = "wikidata", "Wikidata (pista)"
    # Name-only identities: never anchors, never matched to other entities by name.
    DECLARED_NAME = "declared_name", "Organização sem identificador (nome declarado)"
    SCOPED_NAME = "scoped_name", "Pessoa sem identificador (nome na fonte)"


# Organisation registers never identify natural persons.
NON_PERSON_SCHEMES: frozenset[str] = frozenset(
    {
        IdentityScheme.NIPC,
        IdentityScheme.SIOE,
        IdentityScheme.LEI,
        IdentityScheme.EU_TR,
        IdentityScheme.EC,
        IdentityScheme.DECLARED_NAME,
    }
)
NAME_ONLY_SCHEMES: frozenset[str] = frozenset(
    {IdentityScheme.DECLARED_NAME, IdentityScheme.SCOPED_NAME}
)
# Official identifiers that link automatically; Wikidata is only a hint.
ANCHOR_SCHEMES: frozenset[str] = (
    frozenset(IdentityScheme) - {IdentityScheme.WIKIDATA} - NAME_ONLY_SCHEMES
)


class EntityAlias(models.Model):
    """A name an identifier-anchored source publishes for an entity (e.g. AR full and
    parliamentary names). Used only to corroborate identity, never to merge by name."""

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField("nome", max_length=300)
    normalised = models.CharField("nome normalizado", max_length=300, db_index=True)
    scheme = models.CharField("esquema", max_length=16, choices=IdentityScheme.choices)
    external_id = models.CharField("identificador oficial", max_length=240)

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["entity", "normalised", "scheme", "external_id"],
                name="entity_alias_unique",
            )
        ]
        verbose_name = "nome alternativo"
        verbose_name_plural = "nomes alternativos"

    def __str__(self):
        return f"{self.name} ({self.get_scheme_display()} {self.external_id})"


class ParliamentStatusInterval(models.Model):
    """Every published AR mandate status interval (``DepSituacao``), including suspensions.

    Private source data: corroborates cross-source identity (a deputy suspends the mandate
    on taking Government office); never displayed or published as a claim."""

    cadastro_id = models.CharField(max_length=20, db_index=True)
    entity = models.ForeignKey(
        Entity, on_delete=models.PROTECT, related_name="parliament_status_intervals"
    )
    legislature = models.CharField(max_length=12)
    status = models.CharField("situação publicada", max_length=80)
    start = models.DateField(null=True, blank=True)
    end = models.DateField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["cadastro_id", "legislature", "status", "start", "end"],
                name="parliament_status_interval_unique",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["status", "start"], name="parliament_status_start_idx")
        ]

    def __str__(self):
        return f"AR {self.cadastro_id} / {self.legislature} / {self.status} / {self.start}"


class SourceIdentity(models.Model):
    source = models.CharField("esquema", max_length=16, choices=IdentityScheme.choices)
    external_id = models.CharField("identificador oficial", max_length=240)
    entity = models.ForeignKey(Entity, verbose_name="entidade verificada", on_delete=models.PROTECT)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="identidade revista por",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    reviewed_by_id: int | None
    reviewed_at = models.DateTimeField("identidade revista em", null=True, blank=True)
    review_notes = models.TextField("fundamento da correspondência", max_length=2000, blank=True)
    used_at = models.DateTimeField("primeira utilização", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "correspondência de identidade"
        verbose_name_plural = "correspondências de identidade"
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(fields=["source", "external_id"], name="source_identity_unique")
        ]
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("review_sourceidentity", "Pode rever correspondências de identidade")
        ]

    def __str__(self):
        return f"{self.get_source_display()} / {self.external_id}: {self.entity}"

    @editorial_transaction()
    def save(self, *args, **kwargs):
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        if (
            previous
            and previous.used_at
            and any(
                getattr(previous, field) != getattr(self, field)
                for field in (
                    "source",
                    "external_id",
                    "entity_id",
                    "used_at",
                )
            )
        ):
            raise ValidationError("Uma identidade já utilizada é imutável; não mova as afirmações.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if (
            self.entity_id
            and self.source in NON_PERSON_SCHEMES
            and self.entity.kind == Entity.Kind.PERSON
        ):
            raise ValidationError(
                {"entity": "Este identificador só identifica organizações, não pessoas."}
            )
        if (
            self.entity_id
            and self.source == IdentityScheme.SCOPED_NAME
            and self.entity.kind != Entity.Kind.PERSON
        ):
            raise ValidationError({"entity": "Este identificador na fonte só identifica pessoas."})
        if self.source == IdentityScheme.NIPC and not valid_nipc(self.external_id):
            raise ValidationError({"external_id": "NIPC inválido ou de pessoa singular."})
        if bool(self.reviewed_by_id) != bool(self.reviewed_at):
            raise ValidationError("A revisão da identidade exige autor e data.")
        if self.reviewed_by_id and not self.review_notes.strip():
            raise ValidationError(
                {"review_notes": "Documente a correspondência para além do nome."}
            )

    @editorial_transaction()
    def delete(self, *args, **kwargs):
        if type(self).objects.filter(pk=self.pk, used_at__isnull=False).exists():
            raise ValidationError("Uma identidade já utilizada não pode ser eliminada.")
        return super().delete(*args, **kwargs)


class IdentitySuggestion(models.Model):
    """A name-based hint awaiting an editor; never links claims by itself."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pendente"
        ACCEPTED = "accepted", "Aceite"
        REJECTED = "rejected", "Rejeitada"

    scheme = models.CharField("esquema", max_length=16, choices=IdentityScheme.choices)
    external_id = models.CharField("identificador oficial", max_length=240)
    name_as_published = models.CharField("nome publicado pela fonte", max_length=300)
    candidate = models.ForeignKey(
        Entity,
        verbose_name="entidade candidata",
        on_delete=models.PROTECT,
        related_name="identity_suggestions",
    )
    basis = models.TextField("fundamento da sugestão", max_length=2000)
    status = models.CharField(
        "estado", max_length=8, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField("sugerida em", auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="revista por",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="identity_suggestion_reviews",
    )
    reviewed_by_id: int | None
    reviewed_at = models.DateTimeField("revista em", null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["status", "-created_at"]
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["scheme", "external_id", "candidate"],
                name="identity_suggestion_unique",
            )
        ]
        verbose_name = "sugestão de identidade"
        verbose_name_plural = "sugestões de identidade"

    def __str__(self):
        return f"{self.get_scheme_display()} / {self.external_id} → {self.candidate}"


class SourceSyncState(models.Model):
    source = models.CharField(max_length=16, choices=EnrichmentSource.choices)
    scope = models.CharField(max_length=240)
    as_of = models.DateField()

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(fields=["source", "scope"], name="source_sync_scope_unique")
        ]

    def __str__(self):
        return f"{self.get_source_display()} / {self.scope} / {self.as_of}"


class SourceObservation(models.Model):
    class Category(models.TextChoices):
        GOVERNMENT_OFFICE = "government_office", "Cargo governamental"
        BIOGRAPHY_ROLE = "biography_role", "Passagem profissional biográfica"
        DECLARED_INTEREST = "declared_interest", "Interesse ou atividade profissional declarada"
        PARLIAMENT_BODY = "parliament_body", "Composição de órgão parlamentar"
        OFFICE_HOLDING = "office_holding", "Titularidade de cargo"
        ORGANISATION_STRUCTURE = "organisation_structure", "Estrutura organizacional"

    source = models.CharField("origem", max_length=16, choices=EnrichmentSource.choices)
    scope = models.CharField("âmbito da observação", max_length=240)
    external_id = models.CharField("identificador da passagem", max_length=240)
    revision = models.CharField("revisão da fonte", max_length=128)
    identity = models.ForeignKey(
        SourceIdentity,
        verbose_name="identidade oficial",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    subject_name = models.CharField("titular como publicado", max_length=300, blank=True)
    subject_reference = models.CharField(
        "referência oficial do titular", max_length=240, blank=True
    )
    dataset = models.CharField("conjunto de dados", max_length=64, blank=True)
    category = models.CharField("categoria", max_length=24, choices=Category.choices)
    passage = models.TextField("passagem mínima", max_length=10000)
    source_url = models.URLField("URL da fonte", max_length=2048, validators=[validate_source_url])
    publisher = models.CharField("editor da fonte", max_length=240)
    reference = models.CharField("referência da passagem", max_length=160)
    title = models.CharField("título da fonte", max_length=300)
    effective_start = models.DateField("início indicado pela fonte", null=True, blank=True)
    effective_end = models.DateField("fim indicado pela fonte", null=True, blank=True)
    declared_on = models.DateField("data da declaração, não da atividade", null=True, blank=True)
    object = models.ForeignKey(
        Entity,
        verbose_name="organização identificada pela fonte",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    object_name = models.CharField("organização como publicada", max_length=300, blank=True)
    object_identifier = models.CharField(
        "identificador da organização na fonte", max_length=80, blank=True
    )
    kind = models.CharField(
        "tipo indicado pela fonte", max_length=24, choices=Relationship.Kind.choices, blank=True
    )
    role = models.CharField("cargo ou função (fonte)", max_length=240, blank=True)
    role_class = models.CharField(
        "classe da função", max_length=24, choices=Relationship.RoleClass.choices, blank=True
    )
    term = models.ForeignKey(
        Term,
        verbose_name="mandato",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    start_precision = models.CharField(
        "precisão do início",
        max_length=5,
        choices=DatePrecision.choices,
        default=DatePrecision.DAY,
    )
    end_precision = models.CharField(
        "precisão do fim",
        max_length=5,
        choices=DatePrecision.choices,
        default=DatePrecision.DAY,
    )
    temporal_status = models.CharField(
        "estado temporal",
        max_length=8,
        choices=TemporalStatus.choices,
        default=TemporalStatus.UNKNOWN,
    )
    as_of = models.DateField("última data de referência")
    retrieved_at = models.DateTimeField("recolhida em", default=timezone.now)
    is_current = models.BooleanField("presente na última observação", default=True)
    relationship = models.OneToOneField(
        Relationship,
        verbose_name="relação editorial",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    evidence = models.OneToOneField(
        Evidence,
        verbose_name="evidência editorial",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="convertida por",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    reviewed_by_id: int | None
    reviewed_at = models.DateTimeField("convertida em", null=True, blank=True)
    review_notes = models.TextField("fundamento editorial", max_length=2000, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-as_of", "pk"]
        verbose_name = "candidata de enriquecimento"
        verbose_name_plural = "candidatas de enriquecimento"
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["source", "scope", "external_id", "revision"],
                name="source_observation_revision",
            ),
            models.UniqueConstraint(
                fields=["source", "scope", "external_id"],
                condition=Q(is_current=True),
                name="source_observation_current",
            ),
        ]
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("review_sourceobservation", "Pode converter candidatas em relações de rascunho")
        ]

    def __str__(self):
        subject = self.identity.entity if self.identity is not None else self.subject_name
        return f"{subject} / {self.get_category_display()} / {self.reference}"

    @editorial_transaction()
    def save(self, *args, **kwargs):
        previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
        if previous and any(
            getattr(previous, field) != getattr(self, field)
            and not (field in {"identity_id", "object_id"} and getattr(previous, field) is None)
            for field in (
                "source",
                "scope",
                "external_id",
                "revision",
                "identity_id",
                "subject_name",
                "subject_reference",
                "dataset",
                "category",
                "passage",
                "source_url",
                "publisher",
                "reference",
                "title",
                "effective_start",
                "effective_end",
                "declared_on",
                "object_id",
                "object_name",
                "object_identifier",
                "kind",
                "role",
                "role_class",
                "term_id",
                "start_precision",
                "end_precision",
                "temporal_status",
                "retrieved_at",
            )
        ):
            raise ValidationError("A passagem de origem é imutável; importe uma nova revisão.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        identity = self.identity if self.identity_id is not None else None
        if identity is None:
            # Unresolved observations retain the source's published name.
            if not self.subject_name.strip():
                raise ValidationError(
                    {"subject_name": "Sem identidade oficial, indique o titular como publicado."}
                )
        else:
            if (
                identity.source != self.source
                and identity.source not in ANCHOR_SCHEMES
                and identity.source not in NAME_ONLY_SCHEMES
            ):
                raise ValidationError(
                    "A observação exige uma identidade oficial da mesma fonte ou de um registo oficial."
                )
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValidationError("A data final não pode anteceder a data inicial.")
        validate_date_precision(
            self.effective_start,
            self.start_precision,
            self.effective_end,
            self.end_precision,
            start_field="effective_start",
            end_field="effective_end",
        )


class Event(models.Model):
    """A dated official record between parties; not a reviewed Relationship."""

    class Kind(models.TextChoices):
        CONTRACT = "contract", "Contrato público"
        SUBSIDY = "subsidy", "Apoio público"
        EU_FUNDING = "eu_funding", "Fundos europeus"
        MEETING = "meeting", "Reunião"
        HEARING = "hearing", "Audição ou audiência parlamentar"
        GIFT = "gift", "Oferta"
        HOSPITALITY = "hospitality", "Hospitalidade"
        TRAVEL = "travel", "Deslocação"

    class Status(models.TextChoices):
        DRAFT = "draft", "Rascunho"
        PUBLISHED = "published", "Publicado"
        CEASED = "ceased", "Ausente da fonte"
        WITHDRAWN = "withdrawn", "Retirado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.CharField("conjunto de dados", max_length=64, db_index=True)
    scope = models.CharField("âmbito", max_length=64)
    record_id = models.CharField("identificador do registo", max_length=240)
    kind = models.CharField("tipo", max_length=16, choices=Kind.choices)
    title = models.CharField("título", max_length=500)
    date = models.DateField("data", null=True, blank=True)
    start_date = models.DateField("início", null=True, blank=True)
    end_date = models.DateField("fim", null=True, blank=True)
    amount = models.DecimalField("montante", max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField("moeda", max_length=3, default="EUR")
    amount_label = models.CharField("designação do montante", max_length=80, blank=True)
    record_url = models.URLField(
        "URL do registo", max_length=2048, blank=True, validators=[validate_source_url]
    )
    details = models.JSONField("detalhes", default=dict, blank=True)
    source = models.ForeignKey(
        Source, verbose_name="fonte", on_delete=models.PROTECT, related_name="events"
    )
    status = models.CharField("estado", max_length=10, choices=Status.choices, default=Status.DRAFT)
    fingerprint = models.CharField("impressão digital", max_length=64)
    as_of = models.DateField("última data de referência")
    retrieved_at = models.DateTimeField("recolhido em")
    published_at = models.DateTimeField("publicado em", null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="retirado por",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="withdrawn_events",
    )
    withdrawn_by_id: int | None
    withdrawn_at = models.DateTimeField("retirado em", null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-date", "pk"]
        verbose_name = "evento"
        verbose_name_plural = "eventos"
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("withdraw_event", "Pode retirar eventos publicados")
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["status", "kind"], name="event_status_kind_idx"),
            models.Index(fields=["date"], name="event_date_idx"),
            # Newest-first listing of one profile's events reads this in order and stops
            # after a page instead of sorting every event of a large hub.
            models.Index(
                OrderBy(Coalesce("date", "start_date"), descending=True, nulls_last=True),
                "id",
                name="event_listing_idx",
                condition=Q(status="published"),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["dataset", "record_id"], name="event_record_unique"),
            models.CheckConstraint(
                condition=Q(amount__isnull=True) | Q(amount__gte=0),
                name="event_amount_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True)
                | Q(end_date__isnull=True)
                | Q(end_date__gte=F("start_date")),
                name="event_valid_dates",
            ),
            models.CheckConstraint(
                condition=Q(status="withdrawn", withdrawn_at__isnull=False)
                | (~Q(status="withdrawn") & Q(withdrawn_at__isnull=True)),
                name="event_withdrawal_recorded",
            ),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("A data final não pode anteceder a data inicial.")


class EventParty(models.Model):
    class Role(models.TextChoices):
        BUYER = "buyer", "Entidade adjudicante"
        SUPPLIER = "supplier", "Adjudicatário"
        BIDDER = "bidder", "Concorrente"
        GRANTOR = "grantor", "Entidade concedente"
        BENEFICIARY = "beneficiary", "Beneficiário"
        INTERMEDIARY = "intermediary", "Intermediário"
        ATTENDEE = "attendee", "Participante"
        HOST = "host", "Órgão ou anfitrião"
        PROVIDER = "provider", "Ofertante"
        RECIPIENT = "recipient", "Destinatário"

    event = models.ForeignKey(
        Event, verbose_name="evento", on_delete=models.CASCADE, related_name="parties"
    )
    entity = models.ForeignKey(
        Entity,
        verbose_name="entidade",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="event_parties",
    )
    role = models.CharField("papel", max_length=16, choices=Role.choices)
    name = models.CharField("nome como publicado", max_length=300)
    identifier = models.CharField("identificador na fonte", max_length=80, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["pk"]
        verbose_name = "interveniente em evento"
        verbose_name_plural = "intervenientes em eventos"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "event"], name="event_party_entity_idx")
        ]
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["event", "role", "name", "identifier"], name="event_party_unique"
            )
        ]

    def __str__(self):
        return f"{self.get_role_display()}: {self.name}"


class EventEntitySummary(models.Model):
    """Derived, never edited: what ``public_events()`` yields per entity, kind and dataset.

    Rebuilt by ``event_summaries`` whenever an event, a party, an entity's visibility or a
    dataset source's visibility changes; the public pages still filter counterparts by
    ``is_public`` when they read it.
    """

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="+", db_index=False)
    dataset = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Event.Kind.choices)
    event_count = models.PositiveIntegerField()
    amount_eur_sum = models.DecimalField(max_digits=22, decimal_places=2, null=True)
    period_start = models.DateField(null=True)
    period_end = models.DateField(null=True)

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["dataset"], name="event_entity_dataset_idx")
        ]
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["entity", "kind", "dataset"], name="event_entity_summary_unique"
            )
        ]

    def __str__(self):
        return f"{self.entity_id} {self.kind} {self.dataset}"


class EventPairSummary(models.Model):
    """Derived, never edited: public events per pair of entities, kind, dataset and roles.

    Both directions of a pair are stored, so one profile reads only its own rows.
    """

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="+", db_index=False)
    counterpart = models.ForeignKey(
        Entity, on_delete=models.CASCADE, related_name="+", db_index=False
    )
    dataset = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Event.Kind.choices)
    entity_role = models.CharField(max_length=16, choices=EventParty.Role.choices)
    counterpart_role = models.CharField(max_length=16, choices=EventParty.Role.choices)
    event_count = models.PositiveIntegerField()
    amount_eur_sum = models.DecimalField(max_digits=22, decimal_places=2, null=True)
    period_start = models.DateField(null=True)
    period_end = models.DateField(null=True)

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["dataset"], name="event_pair_dataset_idx")
        ]
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=[
                    "entity",
                    "kind",
                    "counterpart",
                    "dataset",
                    "entity_role",
                    "counterpart_role",
                ],
                name="event_pair_summary_unique",
            )
        ]

    def __str__(self):
        return f"{self.entity_id} {self.kind} {self.counterpart_id}"


class RefreshState(models.Model):
    """Last successful applied snapshot for interval-limited refresh scopes."""

    step = models.CharField(max_length=100, primary_key=True)
    last_success = models.DateTimeField()

    def __str__(self):
        return self.step
