"""Private source observations and explicit editorial conversion; never publication."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .models import (
    EnrichmentSource,
    Entity,
    Evidence,
    ParliamentRecord,
    Relationship,
    Source,
    SourceApproval,
    SourceIdentity,
    SourceObservation,
    SourceSyncState,
    editorial_transaction,
    invalidate_relationships,
)
from .parliament_parse import canonical_json


@dataclass(frozen=True)
class ObservationInput:
    external_id: str
    revision: str
    identity: SourceIdentity
    category: str
    passage: str
    source_url: str
    publisher: str
    reference: str
    title: str
    effective_start: date | None = None
    effective_end: date | None = None
    declared_on: date | None = None
    object: Entity | None = None
    kind: str = ""
    retrieved_at: datetime | None = None


SOURCE_CATEGORIES: dict[str, str] = {
    EnrichmentSource.GOVERNMENT: SourceObservation.Category.GOVERNMENT_OFFICE,
    EnrichmentSource.EPT: SourceObservation.Category.DECLARED_INTEREST,
    EnrichmentSource.PARLIAMENT: SourceObservation.Category.BIOGRAPHY_ROLE,
}
PROFESSIONAL_KINDS = frozenset(
    {
        Relationship.Kind.EMPLOYMENT,
        Relationship.Kind.DIRECTORSHIP,
        Relationship.Kind.PROFESSIONAL_ACTIVITY,
        Relationship.Kind.PUBLIC_OFFICE,
    }
)


def _allowed_kinds(category: str) -> frozenset[str]:
    if category == SourceObservation.Category.GOVERNMENT_OFFICE:
        return frozenset({Relationship.Kind.PUBLIC_OFFICE})
    if category == SourceObservation.Category.DECLARED_INTEREST:
        return PROFESSIONAL_KINDS | {Relationship.Kind.SHAREHOLDING}
    return PROFESSIONAL_KINDS


def require_source_approval(source: str, *, scope: str) -> SourceApproval:
    """Live callers must check before each request, including validation-only requests."""
    now = timezone.now()
    approval = (
        SourceApproval.objects.select_related("approved_by")
        .filter(source=source, is_active=True, approved_at__lte=now, review_due_at__gt=now)
        .first()
    )
    if (
        approval is None
        or not approval.approved_by.is_active
        or not isinstance(approval.allowed_scopes, list)
        or scope not in approval.allowed_scopes
        or SOURCE_CATEGORIES.get(source) != scope
    ):
        raise PermissionDenied(
            "A recolha exige autorização ativa e específica para esta fonte e categoria."
        )
    return approval


def _reviewer(actor, permission: str) -> User:
    if not getattr(actor, "pk", None):
        raise PermissionDenied("A operação exige uma pessoa revisora autorizada.")
    current = User.objects.select_for_update().get(pk=actor.pk)
    if not current.is_active or not current.has_perm(permission):
        raise PermissionDenied("Não tem permissão para esta operação editorial.")
    return current


def _check_identity(identity: SourceIdentity, source: str, entity_kind: str) -> None:
    if identity.source != source or identity.entity.kind != entity_kind:
        raise ValidationError(
            "A identidade não corresponde à fonte e ao tipo de entidade exigidos."
        )
    reviewer = identity.reviewed_by
    if source == EnrichmentSource.EPT and (
        reviewer is None or not reviewer.is_active or not identity.reviewed_at
    ):
        raise ValidationError(
            "Reveja a correspondência da pessoa titular antes de consultar ou aplicar interesses."
        )


@editorial_transaction()
def get_source_identity(
    *,
    source: str,
    external_id: str,
    name: str | None = None,
    entity_kind: str = Entity.Kind.PERSON,
) -> SourceIdentity:
    identity = (
        SourceIdentity.objects.select_related("entity", "reviewed_by")
        .filter(source=source, external_id=external_id)
        .first()
    )
    if identity is None:
        if source != EnrichmentSource.GOVERNMENT or not name:
            raise ValidationError(
                "Não existe uma correspondência de identidade revista para este identificador."
            )
        if entity_kind not in {Entity.Kind.PERSON, Entity.Kind.ORGANISATION}:
            raise ValidationError(
                "O Governo só pode criar pessoas ou instituições oficiais privadas."
            )
        entity = Entity.objects.create(name=name, kind=entity_kind, slug=f"gov-{uuid.uuid4().hex}")
        identity = SourceIdentity.objects.create(
            source=source, external_id=external_id, entity=entity
        )
    _check_identity(identity, source, entity_kind)
    return identity


def _withdraw(observation: SourceObservation, *, as_of: date) -> None:
    if observation.relationship_id:
        invalidate_relationships(Relationship.objects.filter(pk=observation.relationship_id))
    evidence = observation.evidence
    if evidence is not None and evidence.is_public:
        evidence.is_public = False
        evidence.save()
    observation.is_current = False
    observation.as_of = as_of
    observation.reviewed_by = None
    observation.reviewed_at = None
    observation.save()


def _draft(
    observation: SourceObservation,
    *,
    object: Entity,
    kind: str,
    description: str,
    start_date: date | None,
    end_date: date | None,
) -> Relationship:
    """Only an explicit editorial conversion may update an existing draft's prose."""
    relationship = observation.relationship
    if relationship is None:
        relationship = Relationship.objects.create(
            subject=observation.identity.entity,
            object=object,
            kind=kind,
            description=description,
            start_date=start_date,
            end_date=end_date,
        )
        source = Source.objects.create(
            title=observation.title,
            publisher=observation.publisher,
            url=observation.source_url,
            retrieved_at=observation.retrieved_at,
        )
        evidence = Evidence.objects.create(
            relationship=relationship,
            source=source,
            excerpt=observation.passage,
            page_reference=observation.reference,
        )
        observation.relationship = relationship
        observation.evidence = evidence
    else:
        relationship.object = object
        relationship.kind = kind
        relationship.description = description
        relationship.start_date = start_date
        relationship.end_date = end_date
        relationship.status = Relationship.Status.DRAFT
        relationship.save()
        if observation.evidence and observation.evidence.is_public:
            observation.evidence.is_public = False
            observation.evidence.save()
    return relationship


def _source_values(item: ObservationInput) -> dict:
    return {
        "identity_id": item.identity.pk,
        "category": item.category,
        "passage": item.passage,
        "source_url": item.source_url,
        "publisher": item.publisher,
        "reference": item.reference,
        "title": item.title,
        "effective_start": item.effective_start,
        "effective_end": item.effective_end,
        "declared_on": item.declared_on,
        "object_id": item.object.pk if item.object else None,
        "kind": item.kind,
    }


@editorial_transaction()
def sync_observations(
    *,
    source: str,
    scope: str,
    observations: tuple[ObservationInput, ...],
    as_of: date,
) -> dict[str, int]:
    """Apply a complete scoped snapshot, including absence, without networking or publication."""
    if source not in SOURCE_CATEGORIES or not scope or len(scope) > 240:
        raise ValidationError("Fonte ou âmbito de observação inválido.")
    if len(observations) > 10000 or len({item.external_id for item in observations}) != len(
        observations
    ):
        raise ValidationError(
            "A observação completa contém demasiadas passagens ou identificadores repetidos."
        )
    state = SourceSyncState.objects.select_for_update().filter(source=source, scope=scope).first()
    if state and as_of < state.as_of:
        raise ValidationError(
            "Não é possível substituir uma observação mais recente por uma anterior."
        )
    result = {"created": 0, "changed": 0, "ceased": 0, "drafts": 0}
    seen = set()
    for item in observations:
        if item.category != SOURCE_CATEGORIES[source]:
            raise ValidationError("Categoria não autorizada para esta fonte.")
        identity = SourceIdentity.objects.select_related("entity", "reviewed_by").get(
            pk=item.identity.pk
        )
        if any(
            getattr(identity, field) != getattr(item.identity, field)
            for field in ("source", "external_id", "entity_id", "reviewed_by_id", "reviewed_at")
        ):
            raise ValidationError(
                "A correspondência mudou desde a recolha; consulte novamente a fonte."
            )
        _check_identity(identity, source, Entity.Kind.PERSON)
        seen.add(item.external_id)
        rows = SourceObservation.objects.filter(
            source=source, scope=scope, external_id=item.external_id
        )
        current = rows.filter(is_current=True).first()
        observation = rows.filter(revision=item.revision).first()
        values = _source_values(item)
        if observation and any(getattr(observation, key) != value for key, value in values.items()):
            raise ValidationError(
                "O mesmo identificador de revisão não pode conter passagens diferentes."
            )
        if current and current.pk != (observation.pk if observation else None):
            _withdraw(current, as_of=as_of)
            result["changed"] += 1
        if observation is None:
            observation = SourceObservation.objects.create(
                source=source,
                scope=scope,
                external_id=item.external_id,
                revision=item.revision,
                as_of=as_of,
                retrieved_at=item.retrieved_at if item.retrieved_at is not None else timezone.now(),
                **values,
            )
            result["created"] += 1
            if source == EnrichmentSource.GOVERNMENT:
                if item.object is None or item.kind != Relationship.Kind.PUBLIC_OFFICE:
                    raise ValidationError(
                        "Um cargo governamental exige uma instituição oficial e o tipo cargo público."
                    )
                portfolios = SourceIdentity.objects.filter(source=source, entity=item.object)
                if not portfolios.exists():
                    raise ValidationError(
                        "A instituição governamental exige um identificador oficial próprio."
                    )
                for portfolio in portfolios.filter(used_at__isnull=True):
                    portfolio.used_at = timezone.now()
                    portfolio.save()
                _draft(
                    observation,
                    object=item.object,
                    kind=item.kind,
                    description=item.passage,
                    start_date=item.effective_start,
                    end_date=item.effective_end,
                )
                observation.save()
                result["drafts"] += 1
        elif not observation.is_current:
            # A return never resurrects either source visibility or editorial approval.
            _withdraw(observation, as_of=as_of)
            observation.is_current = True
            observation.save()
        elif observation.as_of != as_of:
            observation.as_of = as_of
            observation.save(update_fields=["as_of"])
        if identity.used_at is None:
            identity.used_at = timezone.now()
            identity.save()
    for missing in SourceObservation.objects.filter(
        source=source, scope=scope, is_current=True
    ).exclude(external_id__in=seen):
        _withdraw(missing, as_of=as_of)
        result["ceased"] += 1
    SourceSyncState.objects.update_or_create(source=source, scope=scope, defaults={"as_of": as_of})
    return result


def validate_conversion(
    observation: SourceObservation,
    *,
    object: Entity,
    kind: str,
    description: str,
    start_date: date | None,
    end_date: date | None,
    review_notes: str,
) -> None:
    if not observation.is_current:
        raise ValidationError("Esta passagem deixou de constar da fonte; não pode ser convertida.")
    if object._state.adding or object.kind == Entity.Kind.PERSON:
        raise ValidationError("Selecione uma organização existente e verificada.")
    if kind not in _allowed_kinds(observation.category):
        raise ValidationError("O tipo escolhido não pertence ao âmbito profissional desta fonte.")
    if not description.strip() or len(description) > 10000:
        raise ValidationError("Escreva uma descrição limitada ao que a passagem documenta.")
    if not review_notes.strip() or len(review_notes) > 2000:
        raise ValidationError(
            "Documente como verificou a organização, o tipo de relação e as datas; mantenha datas incertas em branco."
        )
    if start_date and end_date and end_date < start_date:
        raise ValidationError("A data final não pode anteceder a data inicial.")


@editorial_transaction()
def convert_observation(
    observation: SourceObservation,
    reviewer,
    *,
    object: Entity,
    kind: str,
    description: str,
    review_notes: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> Relationship:
    actor = _reviewer(reviewer, "core.review_sourceobservation")
    current = SourceObservation.objects.select_related("identity__entity").get(pk=observation.pk)
    target = Entity.objects.get(pk=object.pk)
    validate_conversion(
        current,
        object=target,
        kind=kind,
        description=description,
        start_date=start_date,
        end_date=end_date,
        review_notes=review_notes,
    )
    relationship = _draft(
        current,
        object=target,
        kind=kind,
        description=description,
        start_date=start_date,
        end_date=end_date,
    )
    current.reviewed_by = actor
    current.reviewed_at = timezone.now()
    current.review_notes = review_notes
    current.save()
    return relationship


@editorial_transaction()
def sync_biography_roles(record: ParliamentRecord, *, as_of: date) -> dict[str, int]:
    """Use only the already-retained role allowlist; no extraction of organisation names."""
    member = record.member
    identity, _ = SourceIdentity.objects.get_or_create(
        source=EnrichmentSource.PARLIAMENT,
        external_id=member.cadastro_id,
        defaults={"entity": member.entity},
    )
    if identity.entity_id != member.entity_id:
        raise ValidationError("A identidade parlamentar não corresponde ao cadastro retido.")
    biography = record.data.get("biography", {})
    roles = biography.get("CadCargosFuncoes", [])
    observations = []
    for role in roles:
        # Retain the supplied previous/current marker as context, never as an effective date.
        marker = {"S": "Cargo anterior", "N": "Cargo indicado como atual"}.get(
            role.get("FunAntiga"), "Cargo sem indicação temporal"
        )
        passage = role["FunDes"].strip()
        revision = hashlib.sha256(
            f"{record.fingerprint}:{canonical_json(role)}".encode()
        ).hexdigest()
        observations.append(
            ObservationInput(
                external_id=f"role:{role['FunId']}",
                revision=revision,
                identity=identity,
                category=SourceObservation.Category.BIOGRAPHY_ROLE,
                passage=passage,
                source_url=record.biography_url,
                publisher="Assembleia da República",
                reference=f"CadId={member.cadastro_id}; FunId={role['FunId']}; {record.legislature}; {marker}",
                title=f"Assembleia da República — Registo Biográfico — {record.legislature}",
                retrieved_at=record.retrieved_at,
            )
        )
    return sync_observations(
        source=EnrichmentSource.PARLIAMENT,
        scope=f"member:{member.cadastro_id}",
        observations=tuple(observations),
        as_of=as_of,
    )


@editorial_transaction()
def backfill_biography_roles(records, reviewer) -> dict[str, int]:
    _reviewer(reviewer, "core.review_sourceobservation")
    result = {"created": 0, "changed": 0, "ceased": 0, "drafts": 0}
    for record in records:
        current = ParliamentRecord.objects.select_related("member__entity").get(pk=record.pk)
        if not current.member.is_current or current.member.current_record_id != current.pk:
            raise ValidationError(
                "Selecione apenas a última observação de deputados atualmente importados."
            )
        changes = sync_biography_roles(current, as_of=current.member.as_of)
        for key in result:
            result[key] += changes[key]
    return result


class ObservationReviewForm(forms.ModelForm):
    reviewed_object = forms.ModelChoiceField(
        label="Organização verificada",
        queryset=Entity.objects.exclude(kind=Entity.Kind.PERSON),
        help_text="Selecione uma entidade existente após verificar a identidade; o nome só não basta.",
    )
    reviewed_kind = forms.ChoiceField(
        label="Tipo de relação verificado", choices=Relationship.Kind.choices
    )
    reviewed_start = forms.DateField(
        label="Início documentado", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    reviewed_end = forms.DateField(
        label="Fim documentado", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    reviewed_description = forms.CharField(
        label="Descrição editorial", max_length=10000, widget=forms.Textarea
    )
    conversion_notes = forms.CharField(
        label="Fundamento da revisão",
        max_length=2000,
        widget=forms.Textarea,
        help_text="Justifique a organização, o tipo e as datas. Deixe limites desconhecidos em branco; a data da declaração não é uma data de atividade.",
    )

    class Meta:
        model = SourceObservation
        fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            kind_field = self.fields.get("reviewed_kind")
            if isinstance(kind_field, forms.ChoiceField):
                kind_field.choices = [
                    (value, label)
                    for value, label in Relationship.Kind.choices
                    if value in _allowed_kinds(self.instance.category)
                ]
            relationship = self.instance.relationship
            self.initial.update(
                reviewed_object=relationship.object_id if relationship else self.instance.object_id,
                reviewed_kind=relationship.kind if relationship else self.instance.kind,
                reviewed_start=relationship.start_date
                if relationship
                else self.instance.effective_start,
                reviewed_end=relationship.end_date if relationship else self.instance.effective_end,
                reviewed_description=relationship.description
                if relationship
                else self.instance.passage,
                conversion_notes=self.instance.review_notes,
            )

    def conversion_values(self) -> dict:
        return {
            "object": self.cleaned_data["reviewed_object"],
            "kind": self.cleaned_data["reviewed_kind"],
            "description": self.cleaned_data["reviewed_description"],
            "start_date": self.cleaned_data.get("reviewed_start"),
            "end_date": self.cleaned_data.get("reviewed_end"),
            "review_notes": self.cleaned_data["conversion_notes"],
        }

    def clean(self):
        cleaned = super().clean()
        if not self.errors:
            current = SourceObservation.objects.get(pk=self.instance.pk)
            validate_conversion(current, **self.conversion_values())
        return cleaned
