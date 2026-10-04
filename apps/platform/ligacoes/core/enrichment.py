"""Source observations, evidence-backed automatic publication and editorial conversion."""

import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .identity import (
    OfficeContext,
    anchor_schemes,
    authorised_reviewer,
    declared_organisation,
    normalise_name,
    scoped_person,
    scoped_person_id,
)
from .models import (
    NAME_ONLY_SCHEMES,
    EnrichmentSource,
    Entity,
    Evidence,
    ParliamentRecord,
    Relationship,
    Source,
    SourceIdentity,
    SourceObservation,
    SourceSyncState,
    Term,
    editorial_transaction,
    invalidate_relationships,
    validate_kind_matrix,
)
from .services import publish_imported


@dataclass(frozen=True)
class ObservationInput:
    external_id: str
    revision: str
    category: str
    passage: str
    source_url: str
    publisher: str
    reference: str
    title: str
    # Names without identifiers resolve to a person scoped to this source context.
    identity: SourceIdentity | None = None
    subject_name: str = ""
    subject_reference: str = ""
    effective_start: date | None = None
    effective_end: date | None = None
    declared_on: date | None = None
    object: Entity | None = None
    object_name: str = ""
    object_identifier: str = ""
    kind: str = ""
    dataset: str = ""
    role: str = ""
    role_class: str = ""
    term: Term | None = None
    start_precision: str = "day"
    end_precision: str = "day"
    temporal_status: str = "unknown"
    retrieved_at: datetime | None = None


GOVERNMENT_OFFICE = SourceObservation.Category.GOVERNMENT_OFFICE
BIOGRAPHY_ROLE = SourceObservation.Category.BIOGRAPHY_ROLE
DECLARED_INTEREST = SourceObservation.Category.DECLARED_INTEREST
PARLIAMENT_BODY = SourceObservation.Category.PARLIAMENT_BODY
OFFICE_HOLDING = SourceObservation.Category.OFFICE_HOLDING
ORGANISATION_STRUCTURE = SourceObservation.Category.ORGANISATION_STRUCTURE
SOURCE_CATEGORIES: dict[str, frozenset[str]] = {
    EnrichmentSource.GOVERNMENT: frozenset(
        {GOVERNMENT_OFFICE, OFFICE_HOLDING, ORGANISATION_STRUCTURE}
    ),
    EnrichmentSource.PARLIAMENT: frozenset(
        {
            BIOGRAPHY_ROLE,
            PARLIAMENT_BODY,
            DECLARED_INTEREST,
            OFFICE_HOLDING,
            ORGANISATION_STRUCTURE,
        }
    ),
    EnrichmentSource.EPT: frozenset({DECLARED_INTEREST, OFFICE_HOLDING}),
    EnrichmentSource.SIOE: frozenset({OFFICE_HOLDING, ORGANISATION_STRUCTURE}),
    EnrichmentSource.GLEIF: frozenset({ORGANISATION_STRUCTURE}),
    EnrichmentSource.EP: frozenset({PARLIAMENT_BODY, ORGANISATION_STRUCTURE}),
    EnrichmentSource.ETF: frozenset({OFFICE_HOLDING}),
}
OBSERVATION_LIMIT = 20000
PROFESSIONAL_KINDS = frozenset(
    {
        Relationship.Kind.EMPLOYMENT,
        Relationship.Kind.DIRECTORSHIP,
        Relationship.Kind.PROFESSIONAL_ACTIVITY,
        Relationship.Kind.PUBLIC_OFFICE,
    }
)
CATEGORY_KINDS: dict[str, frozenset[str]] = {
    GOVERNMENT_OFFICE: frozenset({Relationship.Kind.PUBLIC_OFFICE}),
    BIOGRAPHY_ROLE: PROFESSIONAL_KINDS,
    DECLARED_INTEREST: PROFESSIONAL_KINDS
    | {Relationship.Kind.SHAREHOLDING, Relationship.Kind.DECLARED_CLIENT},
    PARLIAMENT_BODY: frozenset({Relationship.Kind.MEMBERSHIP, Relationship.Kind.PUBLIC_OFFICE}),
    OFFICE_HOLDING: PROFESSIONAL_KINDS | {Relationship.Kind.MEMBERSHIP},
    ORGANISATION_STRUCTURE: frozenset(
        {
            Relationship.Kind.PART_OF,
            Relationship.Kind.SUCCESSION,
            Relationship.Kind.MEMBERSHIP,
            Relationship.Kind.SHAREHOLDING,
        }
    ),
}
EMPTY_RESULT = {
    "created": 0,
    "changed": 0,
    "ceased": 0,
    "drafts": 0,
    "published": 0,
    "skipped": 0,
}


def _allowed_kinds(category: str) -> frozenset[str]:
    return CATEGORY_KINDS.get(category, PROFESSIONAL_KINDS)


def _check_identity(identity: SourceIdentity, source: str, entity_kind: str) -> None:
    if identity.source != source or identity.entity.kind != entity_kind:
        raise ValidationError(
            "A identidade não corresponde à fonte e ao tipo de entidade exigidos."
        )


def _check_subject(identity: SourceIdentity, source: str) -> None:
    """Accept own-source, official and source-scoped identities."""
    if (
        identity.source != source
        and identity.source not in anchor_schemes
        and identity.source not in NAME_ONLY_SCHEMES
    ):
        raise ValidationError(
            "A identidade não corresponde à fonte nem a um identificador oficial."
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
            raise ValidationError("O Governo só pode criar pessoas ou instituições oficiais.")
        # Official Government identities are public; hide the entity to withdraw its claims.
        entity = Entity.objects.create(
            name=name, kind=entity_kind, slug=f"gov-{uuid.uuid4().hex}", is_public=True
        )
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


def _draft_source(observation: SourceObservation) -> Source:
    published_at = (
        timezone.make_aware(datetime.combine(observation.declared_on, datetime.min.time()))
        if observation.declared_on is not None
        else None
    )
    source = (
        Source.objects.filter(
            dataset=observation.dataset,
            url=observation.source_url,
            title=observation.title,
            publisher=observation.publisher,
            retrieved_at=observation.retrieved_at,
            published_at=published_at,
        )
        .order_by("pk")
        .first()
    )
    if source is None:
        source = Source.objects.create(
            title=observation.title,
            publisher=observation.publisher,
            url=observation.source_url,
            retrieved_at=observation.retrieved_at,
            dataset=observation.dataset,
            published_at=published_at,
        )
    return source


def _draft(
    observation: SourceObservation,
    *,
    subject: Entity,
    object: Entity,
    kind: str,
    description: str,
    start_date: date | None,
    end_date: date | None,
    sources: dict | None = None,
) -> Relationship:
    """Only an explicit editorial conversion may update an existing draft's prose."""
    # Source precision describes the source's dates, not an editor's replacement dates.
    start_precision = (
        observation.start_precision if start_date == observation.effective_start else "day"
    )
    end_precision = observation.end_precision if end_date == observation.effective_end else "day"
    relationship = observation.relationship
    if relationship is None:
        relationship = Relationship.objects.create(
            subject=subject,
            object=object,
            kind=kind,
            description=description,
            start_date=start_date,
            end_date=end_date,
            role=observation.role,
            role_class=observation.role_class,
            term=observation.term,
            start_precision=start_precision,
            end_precision=end_precision,
            temporal_status=observation.temporal_status,
        )
        source_key = (
            observation.dataset,
            observation.source_url,
            observation.title,
            observation.publisher,
            observation.retrieved_at,
            observation.declared_on,
        )
        if sources is None:
            source = _draft_source(observation)
        else:
            if source_key not in sources:
                sources[source_key] = _draft_source(observation)
            source = sources[source_key]
        evidence = Evidence.objects.create(
            relationship=relationship,
            source=source,
            excerpt=observation.passage,
            page_reference=observation.reference,
        )
        observation.relationship = relationship
        observation.evidence = evidence
    else:
        relationship.subject = subject
        relationship.object = object
        relationship.kind = kind
        relationship.description = description
        relationship.start_date = start_date
        relationship.end_date = end_date
        relationship.start_precision = start_precision
        relationship.end_precision = end_precision
        relationship.status = Relationship.Status.DRAFT
        relationship.save()
        if observation.evidence and observation.evidence.is_public:
            observation.evidence.is_public = False
            observation.evidence.save()
    return relationship


def _source_values(item: ObservationInput) -> dict:
    """Substantive fields fixed by a source revision (provenance metadata excluded)."""
    return {
        "identity_id": item.identity.pk if item.identity else None,
        "subject_name": item.subject_name,
        "subject_reference": item.subject_reference,
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
        "object_name": item.object_name,
        "object_identifier": item.object_identifier,
        "kind": item.kind,
        "role": item.role,
        "role_class": item.role_class,
        "term_id": item.term.pk if item.term else None,
        "start_precision": item.start_precision,
        "end_precision": item.end_precision,
        "temporal_status": item.temporal_status,
    }


def _make_public(observation: SourceObservation) -> None:
    evidence = observation.evidence
    if evidence is None:
        return
    if not evidence.source.is_public:
        evidence.source.is_public = True
        evidence.source.save()
    if not evidence.is_public:
        evidence.is_public = True
        evidence.save()


def _person_scope(observation: SourceObservation) -> str:
    scope = observation.scope
    if observation.dataset == "gov_nomeacoes":
        # Nomination pages and out-of-term rows belong to the same Government.
        scope = ":".join(scope.split(":")[:2])
    elif observation.dataset == "ar_atividades":
        scope = (
            observation.term.code
            if observation.term is not None
            else ":".join(scope.split(":")[:2])
        )
    scope = f"{observation.dataset or observation.source}:{scope}"
    if len(scope) > 160:
        digest = hashlib.sha256(scope.encode()).hexdigest()
        scope = f"{observation.dataset or observation.source}:sha256:{digest}"
    return scope


def _resolve_observation(observation: SourceObservation, people: dict, organisations: dict) -> bool:
    """Resolve only verifiable claims; incomplete or incompatible rows stay private."""
    if not observation.kind or observation.kind not in Relationship.Kind.values:
        return False
    unresolved = observation.identity_id is None or observation.object_id is None
    if observation.object is None:
        if not observation.object_name.strip():
            return False
        key = (observation.object_name, observation.object_identifier)
        if key not in organisations:
            organisations[key] = declared_organisation(
                observation.object_name, nipc=observation.object_identifier
            )
        observation.object = organisations[key]
    subject_kind = observation.identity.entity.kind if observation.identity else Entity.Kind.PERSON
    try:
        validate_kind_matrix(observation.kind, subject_kind, observation.object.kind)
    except ValidationError:
        return False
    if observation.identity is None:
        if not observation.subject_name.strip():
            return False
        scope = _person_scope(observation)
        key = (scope, observation.subject_name)
        if key not in people:
            scoped_person(
                scope,
                observation.subject_name,
                basis=observation.passage,
                offices=[
                    OfficeContext(
                        observation.object, observation.effective_start, observation.effective_end
                    )
                ],
            )
            people[key] = SourceIdentity.objects.get(
                source="scoped_name",
                external_id=scoped_person_id(scope, observation.subject_name),
            )
        observation.identity = people[key]
    if unresolved:
        observation.save(update_fields=["identity", "object"])
    return observation.identity is not None


def _publish_new(observation: SourceObservation, result: dict[str, int], sources: dict) -> None:
    identity = observation.identity
    target = observation.object
    if identity is None or target is None:
        raise ValidationError("A publicação exige uma pessoa e uma organização identificadas.")
    SourceIdentity.objects.filter(
        entity_id__in=[identity.entity_id, target.pk], used_at__isnull=True
    ).update(used_at=timezone.now())
    relationship = _draft(
        observation,
        subject=identity.entity,
        object=target,
        kind=observation.kind,
        description=observation.passage,
        start_date=observation.effective_start,
        end_date=observation.effective_end,
        sources=sources,
    )
    observation.save()
    _make_public(observation)
    result["drafts"] += 1
    if publish_imported(relationship):
        result["published"] += 1


def _fresh_identity(item: ObservationInput, source: str, identities: dict) -> SourceIdentity | None:
    if item.identity is None:
        if not item.subject_name.strip():
            raise ValidationError("Uma observação sem identidade oficial exige o nome publicado.")
        return None
    identity = identities[item.identity.pk]
    if any(
        getattr(identity, field) != getattr(item.identity, field)
        for field in ("source", "external_id", "entity_id", "reviewed_by_id", "reviewed_at")
    ):
        raise ValidationError(
            "A correspondência mudou desde a recolha; consulte novamente a fonte."
        )
    _check_subject(identity, source)
    return identity


def _sync_scope(
    *,
    source: str,
    scope: str,
    observations: tuple[ObservationInput, ...],
    as_of: date,
) -> dict[str, int]:
    if source not in SOURCE_CATEGORIES or not scope or len(scope) > 240:
        raise ValidationError("Fonte ou âmbito de observação inválido.")
    if len(observations) > OBSERVATION_LIMIT or len(
        {item.external_id for item in observations}
    ) != len(observations):
        raise ValidationError(
            "A observação completa contém demasiadas passagens ou identificadores repetidos."
        )
    state = SourceSyncState.objects.select_for_update().filter(source=source, scope=scope).first()
    if source == EnrichmentSource.PARLIAMENT and re.fullmatch(r"member:[^:]+:[^:]+", scope):
        # The pre-history importer used one shared biography scope per person.
        legacy_state = SourceSyncState.objects.filter(
            source=source, scope=":".join(scope.split(":")[:2])
        ).first()
        if legacy_state is not None and (state is None or legacy_state.as_of > state.as_of):
            state = legacy_state
    if state and as_of < state.as_of:
        raise ValidationError(
            "Não é possível substituir uma observação mais recente por uma anterior."
        )
    result = dict(EMPTY_RESULT)
    seen = set()
    existing = list(
        SourceObservation.objects.filter(source=source, scope=scope).select_related(
            "identity__entity", "object", "relationship", "evidence__source"
        )
    )
    revisions = {(row.external_id, row.revision): row for row in existing}
    currents = {row.external_id: row for row in existing if row.is_current}
    withdrawn = {
        row.external_id
        for row in existing
        if row.relationship and row.relationship.status == Relationship.Status.REJECTED
    }
    identities = {
        row.pk: row
        for row in SourceIdentity.objects.select_related("entity", "reviewed_by").filter(
            pk__in=[item.identity.pk for item in observations if item.identity is not None]
        )
    }
    biography_items = [
        item
        for item in observations
        if item.dataset == "ar_registo_biografico" and item.identity is not None
    ]
    withdrawn_biographies = (
        set(
            SourceObservation.objects.filter(
                source=EnrichmentSource.PARLIAMENT,
                dataset="ar_registo_biografico",
                relationship__status=Relationship.Status.REJECTED,
                identity__entity_id__in=[
                    identities[item.identity.pk].entity_id
                    for item in biography_items
                    if item.identity is not None
                ],
                external_id__in=[item.external_id for item in biography_items],
            ).values_list("identity__entity_id", "external_id")
        )
        if biography_items
        else set()
    )
    people: dict = {}
    organisations: dict = {}
    sources: dict = {}
    for item in observations:
        if item.category not in SOURCE_CATEGORIES[source]:
            raise ValidationError("Categoria não autorizada para esta fonte.")
        identity = _fresh_identity(item, source, identities)
        seen.add(item.external_id)
        current = currents.get(item.external_id)
        observation = revisions.get((item.external_id, item.revision))
        values = _source_values(item)
        # Resolution is derived from the retained names, not a source revision.
        compared = {
            key: value
            for key, value in values.items()
            if not (key == "identity_id" and item.identity is None)
            and not (key == "object_id" and item.object is None)
        }
        if observation and any(
            getattr(observation, key) != value for key, value in compared.items()
        ):
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
                dataset=item.dataset,
                **values,
            )
            result["created"] += 1
        elif not observation.is_current:
            # A return withdraws the old approval before automatic republication.
            _withdraw(observation, as_of=as_of)
            observation.is_current = True
            observation.save()
        else:
            if observation.as_of != as_of:
                observation.as_of = as_of
                observation.save(update_fields=["as_of"])
        biography_withdrawn = (
            item.dataset == "ar_registo_biografico"
            and identity is not None
            and (identity.entity_id, item.external_id) in withdrawn_biographies
        )
        if (
            item.external_id in withdrawn
            or biography_withdrawn
            or not _resolve_observation(observation, people, organisations)
        ):
            result["skipped"] += 1
        elif observation.relationship_id is None:
            _publish_new(observation, result, sources)
        elif (
            observation.relationship is not None
            and observation.relationship.status == Relationship.Status.DRAFT
        ):
            _make_public(observation)
            if publish_imported(observation.relationship):
                result["published"] += 1
        if identity is not None and identity.used_at is None:
            SourceIdentity.objects.filter(pk=identity.pk, used_at__isnull=True).update(
                used_at=timezone.now()
            )
            identity.used_at = timezone.now()
    for missing in SourceObservation.objects.filter(
        source=source, scope=scope, is_current=True
    ).exclude(external_id__in=seen):
        _withdraw(missing, as_of=as_of)
        result["ceased"] += 1
    SourceSyncState.objects.update_or_create(source=source, scope=scope, defaults={"as_of": as_of})
    return result


@editorial_transaction()
def sync_observations(
    *,
    source: str,
    scope: str,
    observations: tuple[ObservationInput, ...],
    as_of: date,
) -> dict[str, int]:
    """Apply a complete scoped snapshot, including absence, without networking.

    Every verifiable claim is auto-published; incomplete or incompatible rows stay private.
    """
    return _sync_scope(source=source, scope=scope, observations=observations, as_of=as_of)


@editorial_transaction()
def sync_scoped_snapshot(
    *,
    source: str,
    prefix: str,
    snapshots: dict[str, tuple[ObservationInput, ...]],
    as_of: date,
) -> dict[str, int]:
    """Apply a complete run over many scopes; every absent scope under ``prefix`` ceases."""
    if not prefix or any(not scope.startswith(prefix) for scope in snapshots):
        raise ValidationError("Todos os âmbitos têm de pertencer ao prefixo da observação.")
    result = dict(EMPTY_RESULT)
    for scope, observations in snapshots.items():
        for key, value in _sync_scope(
            source=source, scope=scope, observations=observations, as_of=as_of
        ).items():
            result[key] += value
    known = set(
        SourceSyncState.objects.filter(source=source, scope__startswith=prefix).values_list(
            "scope", flat=True
        )
    ) | set(
        SourceObservation.objects.filter(
            source=source, scope__startswith=prefix, is_current=True
        ).values_list("scope", flat=True)
    )
    for scope in sorted(known - set(snapshots)):
        for key, value in _sync_scope(
            source=source, scope=scope, observations=(), as_of=as_of
        ).items():
            result[key] += value
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
    subject: Entity | None = None,
) -> None:
    if not observation.is_current:
        raise ValidationError("Esta passagem deixou de constar da fonte; não pode ser convertida.")
    identity = observation.identity
    if identity is None:
        if subject is None or subject._state.adding or subject.kind != Entity.Kind.PERSON:
            raise ValidationError(
                "A fonte não identifica a pessoa: selecione uma pessoa existente e verificada."
            )
    elif subject is not None and subject.pk != identity.entity_id:
        raise ValidationError("A pessoa já está identificada pelo identificador oficial.")
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
    subject: Entity | None = None,
) -> Relationship:
    actor = authorised_reviewer(reviewer, "core.review_sourceobservation")
    current = SourceObservation.objects.select_related("identity__entity").get(pk=observation.pk)
    target = Entity.objects.get(pk=object.pk)
    person = Entity.objects.get(pk=subject.pk) if subject is not None else None
    validate_conversion(
        current,
        subject=person,
        object=target,
        kind=kind,
        description=description,
        start_date=start_date,
        end_date=end_date,
        review_notes=review_notes,
    )
    identity = current.identity
    if identity is not None:
        subject_entity = identity.entity
    elif person is not None:
        subject_entity = person
    else:
        raise ValidationError("Selecione a pessoa verificada.")
    relationship = _draft(
        current,
        subject=subject_entity,
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


_BIOGRAPHY_PROJECTION_VERSION = 2
_BIOGRAPHY_ROLE = re.compile(
    r"^(?P<role>(?:s[oó]cio[- ]gerente|administrador(?:a)?|gerente|dire(?:c)?tor(?:a)?"
    r"|presidente|vogal|consultor(?:a)?|assessor(?:a)?|trabalhador(?:a)?"
    r"|professor(?:a)?|docente)(?:\s+(?!de\b|da\b|do\b|em\b|na\b|no\b)[\w-]+){0,5})"
    r"\s+(?:de|da|do|em|na|no)\s+(?P<object>.+)$",
    re.IGNORECASE,
)
_BIOGRAPHY_EXCLUDED = re.compile(
    r"\b(?:partid\w*|juventud\w*|politic\w*|associac\w*|associativ\w*"
    r"|sindicat\w*|confederac\w*|macon\w*|mason\w*|loja\w*"
    r"|jsd|js|jcp|jp|jps|jfp|jpp|ppd|psd|ps|pcp|cds|be|chega|il|pan|livre|cgtp|ugt)\b"
    r"|\bsecretariado nacional\b|\bgrande oriente\b"
)


def _biography_role(passage: str) -> tuple[str, str, str]:
    """Extract explicit role/organisation assertions, never bare profession labels."""
    match = _BIOGRAPHY_ROLE.fullmatch(passage.strip().rstrip("."))
    if match is None:
        return "", "", ""
    name = match["object"].strip()
    if not name or not name[0].isupper() or ";" in name:
        return "", "", ""
    role = match["role"].strip()
    folded = role.casefold()
    if folded.startswith(
        ("sócio", "socio", "administrador", "gerente", "diretor", "director", "presidente", "vogal")
    ):
        kind = Relationship.Kind.DIRECTORSHIP
    elif folded.startswith(("consultor", "assessor")):
        kind = Relationship.Kind.PROFESSIONAL_ACTIVITY
    else:
        kind = Relationship.Kind.EMPLOYMENT
    return kind, role, name


@editorial_transaction()
def sync_biography_roles(record: ParliamentRecord, *, as_of: date) -> dict[str, int]:
    """Publish explicit professional assertions from the retained role allowlist."""
    # Government's shared revision helper imports ObservationInput from this module.
    from .government import revised

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
        if _BIOGRAPHY_EXCLUDED.search(normalise_name(passage)):
            continue
        kind, role_name, object_name = _biography_role(passage)
        item = ObservationInput(
            external_id=f"role:{role['FunId']}",
            revision="",
            identity=identity,
            category=BIOGRAPHY_ROLE,
            passage=passage,
            source_url=record.biography_url,
            publisher="Assembleia da República",
            reference=f"CadId={member.cadastro_id}; FunId={role['FunId']}; {record.legislature}; {marker}",
            title=f"Assembleia da República — Registo Biográfico — {record.legislature}",
            dataset="ar_registo_biografico",
            retrieved_at=record.retrieved_at,
            kind=kind,
            role=role_name,
            object_name=object_name,
        )
        projection = revised(item).revision
        revision = hashlib.sha256(
            f"biography:{_BIOGRAPHY_PROJECTION_VERSION}:{record.fingerprint}:{projection}".encode()
        ).hexdigest()
        observations.append(replace(item, revision=revision))
    return sync_observations(
        source=EnrichmentSource.PARLIAMENT,
        scope=f"member:{member.cadastro_id}:{record.legislature}",
        observations=tuple(observations),
        as_of=as_of,
    )


@editorial_transaction()
def backfill_biography_roles(records, reviewer) -> dict[str, int]:
    authorised_reviewer(reviewer, "core.review_sourceobservation")
    result = dict(EMPTY_RESULT)
    for record in records:
        current = ParliamentRecord.objects.select_related("member__entity").get(pk=record.pk)
        if not current.is_current:
            raise ValidationError(
                "Selecione apenas a última observação de deputados atualmente importados."
            )
        changes = sync_biography_roles(current, as_of=current.as_of)
        for key in result:
            result[key] += changes[key]
    return result


class ObservationReviewForm(forms.ModelForm):
    reviewed_subject = forms.ModelChoiceField(
        label="Pessoa verificada",
        queryset=Entity.objects.filter(kind=Entity.Kind.PERSON),
        required=False,
        help_text="A fonte não traz um identificador oficial da pessoa. Selecione uma pessoa existente após verificar a identidade; o nome só não basta.",
    )
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
        if self.instance.pk and self.instance.identity_id:
            self.fields.pop("reviewed_subject", None)
        elif "reviewed_subject" in self.fields:
            self.fields["reviewed_subject"].required = True
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
            if relationship and "reviewed_subject" in self.fields:
                self.initial["reviewed_subject"] = relationship.subject_id

    def conversion_values(self) -> dict:
        return {
            "subject": self.cleaned_data.get("reviewed_subject"),
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
            current = SourceObservation.objects.select_related("identity").get(pk=self.instance.pk)
            validate_conversion(current, **self.conversion_values())
        return cleaned
