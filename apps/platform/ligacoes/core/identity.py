"""Resolve people by anchors or corroboration and organisations by official or declared names."""

import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from itertools import batched
from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Exists, Manager, Model, OuterRef, Q, QuerySet
from django.db.models.fields.reverse_related import ManyToOneRel
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    ANCHOR_SCHEMES,
    NON_PERSON_SCHEMES,
    Entity,
    EntityAlias,
    EntityRedirect,
    IdentityDecision,
    IdentityMerge,
    IdentityScheme,
    IdentitySuggestion,
    ParliamentImportState,
    ParliamentMember,
    ParliamentStatusInterval,
    Relationship,
    ReviewEvent,
    SourceIdentity,
    editorial_transaction,
)
from .validators import NIPC_LEGAL_PREFIXES, valid_nipc

# Every official scheme except Wikidata, which is only ever a hint.
anchor_schemes: frozenset[str] = ANCHOR_SCHEMES
PARLIAMENT = IdentityScheme.PARLIAMENT
PENDING = IdentitySuggestion.Status.PENDING
REJECTED = IdentitySuggestion.Status.REJECTED
AR_INSTITUTION_ID = "institution:assembleia-da-republica"
AR_NIPC = "600054128"
GOVERNMENT_CLASSIFICATIONS = (
    Entity.Classification.GOVERNMENT,
    Entity.Classification.GOVERNMENT_DEPARTMENT,
    Entity.Classification.GOVERNMENT_OFFICE,
)


@dataclass(frozen=True)
class OfficeContext:
    """An incoming office's institution and source-published date bounds."""

    institution: Entity
    start: date | None
    end: date | None


HONORIFICS = frozenset(
    {
        "arq",
        "arqa",
        "arquiteta",
        "arquiteto",
        "doutor",
        "doutora",
        "dr",
        "dra",
        "eng",
        "enga",
        "engo",
        "engenheira",
        "engenheiro",
        "exma",
        "exmo",
        "juiz",
        "juiza",
        "mestre",
        "prof",
        "profa",
        "professor",
        "professora",
        "sr",
        "sra",
    }
)
IDENTITY_BATCH = 5000


def authorised_reviewer(actor: object, permission: str) -> User:
    """Recheck an editor's permission inside the caller's transaction."""
    pk = getattr(actor, "pk", None)
    if not pk:
        raise PermissionDenied("A operação exige uma pessoa revisora autorizada.")
    current = User.objects.select_for_update().get(pk=pk)
    if not current.is_active or not current.has_perm(permission):
        raise PermissionDenied("Não tem permissão para esta operação editorial.")
    return current


def normalise_name(name: str) -> str:
    """Casefolded, accent-free tokens without leading honorifics or punctuation."""
    # "Dr.ª", "Eng.º": keep the ordinal with its abbreviation so the title is recognised.
    joined = re.sub(r"\.\s*([ªº])", r"\1", name)
    decomposed = unicodedata.normalize("NFKD", joined)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    tokens = re.sub(r"[\W_]+", " ", plain.casefold()).split()
    while tokens and tokens[0] in HONORIFICS:
        # Leading only: "Mestre" is also a Portuguese surname.
        tokens.pop(0)
    return " ".join(tokens)


def record_alias(entity: Entity, name: str, *, scheme: str, external_id: str) -> None:
    """Remember a name an identifier-anchored source publishes for ``entity``."""
    key = normalise_name(name)
    if key:
        EntityAlias.objects.get_or_create(
            entity=entity,
            normalised=key,
            scheme=scheme,
            external_id=external_id,
            defaults={"name": name[:300]},
        )


__all__ = ["NIPC_LEGAL_PREFIXES", "valid_nipc"]


def _check_anchor(scheme: str, external_id: str, kind: str) -> None:
    if scheme not in anchor_schemes:
        raise ValidationError("Só identificadores oficiais podem criar ou ligar entidades.")
    if not external_id or len(external_id) > 240:
        raise ValidationError("Identificador oficial inválido.")
    if kind not in Entity.Kind.values:
        raise ValidationError("Tipo de entidade inválido.")
    if scheme in NON_PERSON_SCHEMES and kind == Entity.Kind.PERSON:
        raise ValidationError("Este identificador oficial só identifica organizações.")
    if scheme == IdentityScheme.NIPC and not valid_nipc(external_id):
        raise ValidationError("NIPC inválido ou de pessoa singular.")


def _slug(scheme: str, external_id: str) -> str:
    readable = slugify(re.sub(r"[\W_]+", " ", external_id))[:120].strip("-")
    return f"{scheme}-{readable}-{uuid.uuid4().hex[:8]}"


def _same_nature(entity: Entity, kind: str) -> None:
    if (entity.kind == Entity.Kind.PERSON) != (kind == Entity.Kind.PERSON):
        raise ValidationError("O identificador oficial já pertence a outro tipo de entidade.")


@editorial_transaction()
def official_entity(
    scheme: str,
    external_id: str,
    *,
    name: str,
    kind: str,
    classification: str,
    public: bool = True,
) -> Entity:
    """Get or create the entity an anchor identifies; never matches by name."""
    _check_anchor(scheme, external_id, kind)
    identity = (
        SourceIdentity.objects.select_related("entity")
        .filter(source=scheme, external_id=external_id)
        .first()
    )
    if identity is not None:
        _same_nature(identity.entity, kind)
        return identity.entity
    entity = Entity(
        name=name,
        kind=kind,
        classification=classification,
        is_public=public,
        slug=_slug(scheme, external_id),
    )
    entity.save()
    SourceIdentity(source=scheme, external_id=external_id, entity=entity).save()
    return entity


def official_entities_bulk(scheme: str, rows: dict[str, tuple[str, str, str]]) -> dict[str, Entity]:
    """Bulk official-ID resolution for large datasets; validates every new row first."""
    with editorial_transaction():
        resolved: dict[str, Entity] = {}
        for chunk in batched(rows, IDENTITY_BATCH, strict=False):
            for identity in SourceIdentity.objects.select_related("entity").filter(
                source=scheme, external_id__in=chunk
            ):
                resolved[identity.external_id] = identity.entity
        entities: list[Entity] = []
        identities: list[SourceIdentity] = []
        for external_id, (name, kind, classification) in rows.items():
            _check_anchor(scheme, external_id, kind)
            if external_id in resolved:
                _same_nature(resolved[external_id], kind)
                continue
            entity = Entity(
                name=name,
                normalised_name=normalise_name(name),
                kind=kind,
                classification=classification,
                is_public=True,
                slug=_slug(scheme, external_id),
            )
            # Constraint and FK checks would query per row; the database enforces them.
            entity.full_clean(validate_unique=False, validate_constraints=False)
            identity = SourceIdentity(source=scheme, external_id=external_id, entity=entity)
            identity.full_clean(
                exclude=["entity"], validate_unique=False, validate_constraints=False
            )
            entities.append(entity)
            identities.append(identity)
            resolved[external_id] = entity
        Entity.objects.bulk_create(entities, batch_size=1000)
        SourceIdentity.objects.bulk_create(identities, batch_size=1000)
        return resolved


def _named_entities(names: Iterable[str], entities: QuerySet[Entity]) -> QuerySet[Entity]:
    """Exact indexed canonical-name and official-alias matches, without table scans."""
    keys = {key for name in names if (key := normalise_name(name))}
    aliases = EntityAlias.objects.filter(normalised__in=keys).values("entity_id")
    return entities.filter(Q(normalised_name__in=keys) | Q(pk__in=aliases)).order_by("pk")


def _person_candidates(
    names: Iterable[str], restrict_to: QuerySet[Entity] | None = None
) -> QuerySet[Entity]:
    people = Entity.objects.filter(kind=Entity.Kind.PERSON, is_public=True)
    if restrict_to is not None:
        people = people.filter(pk__in=restrict_to.values("pk"))
    return _named_entities(names, people)


def _suggest_candidates(
    scheme: str, external_id: str, name: str, basis: str, candidates: Iterable[Entity]
) -> None:
    """Keep namesakes advisory, including when a separate identity was just created."""
    known = set(
        IdentitySuggestion.objects.filter(scheme=scheme, external_id=external_id).values_list(
            "candidate_id", flat=True
        )
    )
    IdentitySuggestion.objects.bulk_create(
        [
            IdentitySuggestion(
                scheme=scheme,
                external_id=external_id,
                name_as_published=name[:300],
                candidate=candidate,
                basis=basis[:2000],
            )
            for candidate in candidates
            if candidate.pk not in known
        ]
    )


@editorial_transaction()
def suggest_person(
    scheme: str,
    external_id: str,
    *,
    name: str,
    basis: str,
    restrict_to: QuerySet[Entity] | None = None,
) -> list[IdentitySuggestion]:
    """Record exact normalised namesakes as pending suggestions; never links them."""
    if SourceIdentity.objects.filter(source=scheme, external_id=external_id).exists():
        return []
    _check_anchor(scheme, external_id, Entity.Kind.PERSON)
    _suggest_candidates(scheme, external_id, name, basis, _person_candidates((name,), restrict_to))
    return list(
        IdentitySuggestion.objects.filter(
            scheme=scheme, external_id=external_id, status=PENDING
        ).order_by("pk")
    )


@editorial_transaction()
def suggest_identity(
    scheme: str,
    external_id: str,
    *,
    candidate: Entity,
    name: str,
    basis: str,
) -> IdentitySuggestion | None:
    """Pending suggestion for an explicit candidate; ``None`` when mapped or already decided."""
    if scheme not in IdentityScheme.values or not external_id or len(external_id) > 240:
        raise ValidationError("Identificador oficial inválido.")
    if scheme in NON_PERSON_SCHEMES and candidate.kind == Entity.Kind.PERSON:
        raise ValidationError("Este identificador oficial só identifica organizações.")
    if scheme == IdentityScheme.SCOPED_NAME:
        _same_nature(candidate, Entity.Kind.PERSON)
    if SourceIdentity.objects.filter(source=scheme, external_id=external_id).exists():
        return None
    existing = IdentitySuggestion.objects.filter(
        scheme=scheme, external_id=external_id, candidate=candidate
    ).first()
    if existing is not None:
        return existing if existing.status == PENDING else None
    return IdentitySuggestion.objects.create(
        scheme=scheme,
        external_id=external_id,
        name_as_published=name[:300],
        candidate=candidate,
        basis=basis[:2000],
    )


def _corroboration(
    candidate: Entity,
    scheme: str,
    external_id: str,
    offices: tuple[OfficeContext, ...],
    suspensions: tuple[date, ...],
) -> str:
    """Return a locating signal for S1, S2, symmetric S3 or AR-status S4."""
    if IdentitySuggestion.objects.filter(
        scheme=scheme,
        external_id=external_id,
        candidate=candidate,
        status=REJECTED,
    ).exists():
        # An editor's distinct-person decision overrides every automatic signal.
        return ""
    same_register = SourceIdentity.objects.filter(entity=candidate, source=scheme)
    if scheme == IdentityScheme.SCOPED_NAME:
        # These keys identify within a scope, not across source scopes. The suffix
        # is either one normalised-name token string or "sha256:<digest>".
        scope = external_id.rsplit(":", 1)[0]
        if ":sha256:" in external_id:
            scope = external_id.rsplit(":sha256:", 1)[0]
        same_register = same_register.filter(external_id__startswith=f"{scope}:")
        for key in same_register.values_list("external_id", flat=True):
            candidate_scope = key.rsplit(":", 1)[0]
            if ":sha256:" in key:
                candidate_scope = key.rsplit(":sha256:", 1)[0]
            if candidate_scope == scope:
                return ""
    elif scheme != IdentityScheme.EPT and same_register.exists():
        # Distinct IDs in one official register identify distinct people. EpT's
        # holder IDs are the exception: one person can hold several of them.
        return ""
    relationships = Relationship.objects.filter(
        subject=candidate, status=Relationship.Status.PUBLISHED, object__is_public=True
    )
    for office in offices:
        # Only immediate structural neighbours, never siblings sharing a parent.
        parts = Relationship.objects.filter(
            kind=Relationship.Kind.PART_OF,
            status=Relationship.Status.PUBLISHED,
            subject__is_public=True,
            object__is_public=True,
        )
        institutions = Entity.objects.filter(
            Q(pk=office.institution.pk)
            | Q(pk__in=parts.filter(object=office.institution).values("subject_id"))
            | Q(pk__in=parts.filter(subject=office.institution).values("object_id"))
        )
        overlapping = relationships.filter(object__in=institutions)
        if office.start is not None:
            overlapping = overlapping.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=office.start)
            )
        if office.end is not None:
            overlapping = overlapping.filter(
                Q(start_date__isnull=True) | Q(start_date__lte=office.end)
            )
        match = overlapping.order_by("pk").first()
        if match is not None:
            return (
                f"S1, relação {match.pk} na instituição {match.object_id}; "
                f"intervalos {match.start_date} - {match.end_date} e {office.start} - {office.end}"
            )
    # Local import avoids the crosswalk importer's dependency on suggest_identity.
    from .wikidata import corroborating_qid

    qid = corroborating_qid(scheme, external_id, candidate)
    if qid:
        return (
            f"S2, Wikidata {qid} associa {scheme}/{external_id} "
            "aos identificadores oficiais da pessoa"
        )
    for office in offices:
        if (
            office.start is None
            or office.institution.classification not in GOVERNMENT_CLASSIFICATIONS
        ):
            continue
        suspension = (
            ParliamentStatusInterval.objects.filter(
                entity=candidate,
                status__startswith="Suspenso",
                start__range=(office.start - timedelta(days=15), office.start + timedelta(days=15)),
            )
            .order_by("start", "pk")
            .first()
        )
        if suspension is not None:
            return (
                f"S3(a), AR {suspension.cadastro_id}/{suspension.legislature}: "
                f"{suspension.status} em {suspension.start}; cargo em {office.start}"
            )
    if scheme == PARLIAMENT:
        for start in suspensions:
            match = (
                relationships.filter(
                    object__classification__in=GOVERNMENT_CLASSIFICATIONS,
                    start_date__range=(start - timedelta(days=15), start + timedelta(days=15)),
                )
                .order_by("start_date", "pk")
                .first()
            )
            if match is not None:
                return (
                    f"S3(b), suspensão AR em {start}; "
                    f"relação governamental {match.pk} em {match.start_date}"
                )
    for office in offices:
        if (
            not SourceIdentity.objects.filter(entity=office.institution)
            .filter(
                Q(source=PARLIAMENT, external_id=AR_INSTITUTION_ID)
                | Q(source=IdentityScheme.NIPC, external_id=AR_NIPC)
            )
            .exists()
        ):
            continue
        intervals = ParliamentStatusInterval.objects.filter(entity=candidate)
        if office.start is not None:
            intervals = intervals.filter(Q(end__isnull=True) | Q(end__gte=office.start))
        if office.end is not None:
            intervals = intervals.filter(Q(start__isnull=True) | Q(start__lte=office.end))
        interval = intervals.order_by("pk").first()
        if interval is not None:
            return (
                f"S4, cargo AR {office.start} - {office.end}; "
                f"AR {interval.cadastro_id}/{interval.legislature}: "
                f"{interval.status}, {interval.start} - {interval.end}"
            )
    return ""


@editorial_transaction()
def resolve_person(
    scheme: str,
    external_id: str,
    *,
    name: str,
    basis: str,
    aliases: Iterable[str] = (),
    offices: Iterable[OfficeContext] = (),
    suspensions: Iterable[date] = (),
    restrict_to: QuerySet[Entity] | None = None,
) -> Entity:
    """Reuse an identity or exactly one corroborated namesake; otherwise create separately."""
    if scheme == IdentityScheme.SCOPED_NAME:
        if not external_id or len(external_id) > 240:
            raise ValidationError("Identificador de pessoa na fonte inválido.")
    else:
        _check_anchor(scheme, external_id, Entity.Kind.PERSON)
    names = (name, *aliases)
    identity = (
        SourceIdentity.objects.select_related("entity")
        .filter(source=scheme, external_id=external_id)
        .first()
    )
    if identity is not None:
        _same_nature(identity.entity, Entity.Kind.PERSON)
        entity = identity.entity
    else:
        candidates = list(_person_candidates(names, restrict_to))
        office_contexts, suspension_dates = tuple(offices), tuple(suspensions)
        matches = [
            (candidate, signal)
            for candidate in candidates
            if (
                signal := _corroboration(
                    candidate, scheme, external_id, office_contexts, suspension_dates
                )
            )
        ]
        if len(matches) == 1:
            entity, signal = matches[0]
            SourceIdentity(
                source=scheme,
                external_id=external_id,
                entity=entity,
                review_notes=f"Correspondência automática: {signal}"[:2000],
            ).save()
        else:
            entity = Entity(
                name=name,
                kind=Entity.Kind.PERSON,
                is_public=True,
                slug=_slug(scheme, external_id),
            )
            entity.save()
            SourceIdentity(source=scheme, external_id=external_id, entity=entity).save()
            _suggest_candidates(scheme, external_id, name, basis, candidates)
    for alias in names:
        record_alias(entity, alias, scheme=scheme, external_id=external_id)
    return entity


def scoped_person_id(scope: str, name: str) -> str:
    """The deterministic scoped key, hashing an oversized normalised name."""
    key = normalise_name(name)
    if not scope.strip() or not key:
        raise ValidationError("Âmbito e nome da pessoa são obrigatórios.")
    external_id = f"{scope}:{key}"
    if len(external_id) > 240:
        external_id = f"{scope}:sha256:{sha256(key.encode()).hexdigest()}"
    if len(external_id) > 240:
        raise ValidationError("Âmbito de pessoa na fonte demasiado longo.")
    return external_id


def scoped_person(
    scope: str, name: str, *, basis: str, offices: Iterable[OfficeContext] = ()
) -> Entity:
    """A name identifies a person only inside its source scope unless corroborated."""
    return resolve_person(
        IdentityScheme.SCOPED_NAME,
        scoped_person_id(scope, name),
        name=name,
        basis=basis,
        offices=offices,
    )


@editorial_transaction()
def declared_organisation(name: str, *, nipc: str = "") -> Entity:
    """Legal NIPC, unique anchored namesake, else a reusable declared-name organisation."""
    if valid_nipc(nipc):
        entity = official_entity(
            IdentityScheme.NIPC, nipc, name=name, kind=Entity.Kind.COMPANY, classification=""
        )
        record_alias(entity, name, scheme=IdentityScheme.NIPC, external_id=nipc)
        return entity
    key = normalise_name(name)
    if not key:
        raise ValidationError("Nome da organização inválido.")
    external_id = key if len(key) <= 240 else f"sha256:{sha256(key.encode()).hexdigest()}"
    organisations = Entity.objects.exclude(kind=Entity.Kind.PERSON).filter(
        Exists(SourceIdentity.objects.filter(entity=OuterRef("pk"), source__in=anchor_schemes))
    )
    candidates = list(_named_entities((name,), organisations)[:2])
    if len(candidates) == 1:
        return candidates[0]
    identity = (
        SourceIdentity.objects.select_related("entity")
        .filter(source=IdentityScheme.DECLARED_NAME, external_id=external_id)
        .first()
    )
    if identity is not None:
        _same_nature(identity.entity, Entity.Kind.ORGANISATION)
        return identity.entity
    entity = Entity(
        name=name,
        kind=Entity.Kind.ORGANISATION,
        is_public=True,
        slug=_slug(IdentityScheme.DECLARED_NAME, external_id),
    )
    entity.save()
    SourceIdentity(
        source=IdentityScheme.DECLARED_NAME, external_id=external_id, entity=entity
    ).save()
    return entity


def _pending(suggestion: IdentitySuggestion) -> IdentitySuggestion:
    current = (
        IdentitySuggestion.objects.select_for_update()
        .select_related("candidate")
        .get(pk=suggestion.pk)
    )
    if current.status != PENDING:
        raise ValidationError("Esta sugestão já foi decidida.")
    return current


@editorial_transaction()
def accept_suggestion(suggestion: IdentitySuggestion, reviewer: object) -> SourceIdentity:
    """Review a mapping (redirecting only unused ones); reject other pending candidates."""
    actor = authorised_reviewer(reviewer, "core.review_sourceidentity")
    current = _pending(suggestion)
    identity = (
        SourceIdentity.objects.select_for_update()
        .filter(source=current.scheme, external_id=current.external_id)
        .first()
    )
    if identity is not None and identity.used_at and identity.entity_id != current.candidate_id:
        raise ValidationError("Uma identidade já utilizada é imutável; não mova as afirmações.")
    old_entity_id = identity.entity_id if identity is not None else None
    now = timezone.now()
    if identity is None:
        identity = SourceIdentity(source=current.scheme, external_id=current.external_id)
    identity.entity = current.candidate
    identity.reviewed_by = actor
    identity.reviewed_at = now
    identity.review_notes = current.basis
    identity.save()
    if old_entity_id is not None and old_entity_id != current.candidate_id:
        aliases = EntityAlias.objects.filter(
            entity_id=old_entity_id, scheme=current.scheme, external_id=current.external_id
        )
        for alias in aliases:
            record_alias(
                current.candidate,
                alias.name,
                scheme=current.scheme,
                external_id=current.external_id,
            )
        aliases.delete()
    record_alias(
        current.candidate,
        current.name_as_published,
        scheme=current.scheme,
        external_id=current.external_id,
    )
    IdentitySuggestion.objects.filter(pk=current.pk).update(
        status=IdentitySuggestion.Status.ACCEPTED, reviewed_by=actor, reviewed_at=now
    )
    IdentitySuggestion.objects.filter(
        scheme=current.scheme, external_id=current.external_id, status=PENDING
    ).exclude(pk=current.pk).update(status=REJECTED, reviewed_by=actor, reviewed_at=now)
    return identity


@editorial_transaction()
def reject_suggestion(suggestion: IdentitySuggestion, reviewer: object) -> IdentitySuggestion:
    actor = authorised_reviewer(reviewer, "core.review_sourceidentity")
    current = _pending(suggestion)
    IdentitySuggestion.objects.filter(pk=current.pk).update(
        status=REJECTED, reviewed_by=actor, reviewed_at=timezone.now()
    )
    current.refresh_from_db()
    return current


def _attach(external_id: str, entity: Entity) -> None:
    identity = SourceIdentity.objects.filter(source=PARLIAMENT, external_id=external_id).first()
    if identity is None:
        SourceIdentity(source=PARLIAMENT, external_id=external_id, entity=entity).save()
    elif identity.entity_id != entity.pk:
        raise ValidationError("A identidade parlamentar não corresponde à entidade retida.")


@editorial_transaction()
def ar_institution() -> Entity:
    """The single Assembleia da República entity."""
    state = (
        ParliamentImportState.objects.select_related("institution").filter(key="assembly").first()
    )
    if state is not None:
        _attach(AR_INSTITUTION_ID, state.institution)
        institution = state.institution
    else:
        institution = official_entity(
            PARLIAMENT,
            AR_INSTITUTION_ID,
            name="Assembleia da República",
            kind=Entity.Kind.ORGANISATION,
            classification=Entity.Classification.PARLIAMENT,
        )
    if not SourceIdentity.objects.filter(source=IdentityScheme.NIPC, external_id=AR_NIPC).exists():
        SourceIdentity(source=IdentityScheme.NIPC, external_id=AR_NIPC, entity=institution).save()
    return institution


@editorial_transaction()
def ar_person(
    cadastro_id: str,
    name: str,
    *,
    aliases: Iterable[str] = (),
    offices: Iterable[OfficeContext] = (),
    suspensions: Iterable[date] = (),
) -> Entity:
    """Resolve a stable AR DepCadId with the same corroboration as other person sources."""
    if not re.fullmatch(r"[0-9]{1,20}", cadastro_id):
        raise ValidationError("Identificador parlamentar inválido.")
    member = ParliamentMember.objects.select_related("entity").filter(pk=cadastro_id).first()
    if member is not None:
        _same_nature(member.entity, Entity.Kind.PERSON)
        _attach(cadastro_id, member.entity)
    return resolve_person(
        PARLIAMENT,
        cadastro_id,
        name=name,
        basis=f"Identificador parlamentar AR {cadastro_id}.",
        aliases=aliases,
        offices=offices,
        suspensions=suspensions,
    )


def _entity_references() -> list[tuple[Manager[Model], str]]:
    """Include hidden reverse FKs (e.g. distinct decisions), not just exposed accessors."""
    references = []
    for field in Entity._meta.get_fields(include_hidden=True):
        if isinstance(field, ManyToOneRel) and isinstance(field.related_model, type):
            references.append(
                (
                    cast(Manager[Model], field.related_model._default_manager),
                    field.field.name,
                )
            )
    return references


@dataclass(frozen=True)
class IdentityMatch:
    from_entity: Entity
    to_entity: Entity
    basis: str


def _exclusive_registers(identities: Iterable[SourceIdentity]) -> set[tuple[str, str]]:
    registers = set()
    for identity in identities:
        if identity.source == IdentityScheme.SCOPED_NAME:
            key = identity.external_id
            scope = key.rsplit(":sha256:", 1)[0] if ":sha256:" in key else key.rsplit(":", 1)[0]
            registers.add((identity.source, scope))
        elif identity.source in anchor_schemes and identity.source != IdentityScheme.EPT:
            registers.add((identity.source, ""))
    return registers


def _person_name_pairs() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Indexed SQL joins return only public-person namesake pairs, not all names."""
    entity = connection.ops.quote_name(Entity._meta.db_table)
    alias = connection.ops.quote_name(EntityAlias._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT pairs.first_id, pairs.second_id FROM (
                SELECT a.id AS first_id, b.id AS second_id
                FROM {entity} a JOIN {entity} b ON a.normalised_name = b.normalised_name
                WHERE a.id < b.id AND a.normalised_name <> ''
                UNION
                SELECT LEAST(a.entity_id, b.id), GREATEST(a.entity_id, b.id)
                FROM {alias} a JOIN {entity} b ON a.normalised = b.normalised_name
                WHERE a.entity_id <> b.id AND a.normalised <> ''
                UNION
                SELECT a.entity_id, b.entity_id
                FROM {alias} a JOIN {alias} b ON a.normalised = b.normalised
                WHERE a.entity_id < b.entity_id AND a.normalised <> ''
            ) pairs
            JOIN {entity} a ON a.id = pairs.first_id
            JOIN {entity} b ON b.id = pairs.second_id
            WHERE a.kind = 'person' AND b.kind = 'person' AND a.is_public AND b.is_public
            ORDER BY pairs.first_id, pairs.second_id
            """  # noqa: S608 -- quoted model tables, no user-provided SQL
        )
        return cursor.fetchall()


def _pair_blocked(first: Entity, second: Entity, identities: dict) -> bool:
    if _exclusive_registers(identities[first.pk]) & _exclusive_registers(identities[second.pk]):
        return True
    if ParliamentMember.objects.filter(entity__in=(first, second)).count() == 2:
        return True
    if (
        IdentityDecision.objects.filter(decision="distinct")
        .filter(Q(first=first, second=second) | Q(first=second, second=first))
        .exists()
    ):
        return True
    rejected = Q()
    for candidate, other in ((first, second), (second, first)):
        for identity in identities[other.pk]:
            rejected |= Q(
                candidate=candidate,
                scheme=identity.source,
                external_id=identity.external_id,
            )
    return (
        bool(rejected)
        and IdentitySuggestion.objects.filter(
            rejected,
            status=REJECTED,
        ).exists()
    )


def _pair_basis(first: Entity, second: Entity, identities: dict) -> str:
    if _pair_blocked(first, second, identities):
        return ""
    for candidate, incoming in ((first, second), (second, first)):
        offices = tuple(
            OfficeContext(row.object, row.start_date, row.end_date)
            for row in Relationship.objects.filter(
                subject=incoming,
                status=Relationship.Status.PUBLISHED,
                kind=Relationship.Kind.PUBLIC_OFFICE,
                object__is_public=True,
            )
            .select_related("object")
            .order_by("pk")
        )
        suspensions = tuple(
            ParliamentStatusInterval.objects.filter(
                entity=incoming,
                status__startswith="Suspenso",
                start__isnull=False,
            ).values_list("start", flat=True)
        )
        incoming_ids = [
            identity
            for identity in identities[incoming.pk]
            if identity.source in anchor_schemes or identity.source == IdentityScheme.SCOPED_NAME
        ]
        # Manually entered people can also have corroborated public offices.
        for identity in incoming_ids or [
            SourceIdentity(source=IdentityScheme.SCOPED_NAME, external_id=f"entity:{incoming.pk}")
        ]:
            signal = _corroboration(
                candidate,
                identity.source,
                identity.external_id,
                offices,
                suspensions,
            )
            if signal:
                return f"Correspondência automática: {signal}"[:2000]
    return ""


def _anchor_order(entity: Entity, identities: dict) -> tuple[int, int, str]:
    ranks = {
        PARLIAMENT: 0,
        IdentityScheme.GOVERNMENT: 1,
        IdentityScheme.EPT: 2,
        IdentityScheme.EP: 3,
        IdentityScheme.SCOPED_NAME: 4,
    }
    known = identities[entity.pk]
    return (
        min((ranks.get(row.source, 5) for row in known), default=5),
        min((row.pk for row in known), default=2**63),
        str(entity.pk),
    )


def _namesake_component(
    pairs: list[tuple[uuid.UUID, uuid.UUID]], seeds: tuple[uuid.UUID, uuid.UUID]
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Keep every current namesake connected to either proposed endpoint."""
    neighbours: dict[uuid.UUID, set[uuid.UUID]] = {}
    for first, second in pairs:
        neighbours.setdefault(first, set()).add(second)
        neighbours.setdefault(second, set()).add(first)
    component = set(seeds)
    pending = list(seeds)
    while pending:
        for neighbour in neighbours.get(pending.pop(), ()):
            if neighbour not in component:
                component.add(neighbour)
                pending.append(neighbour)
    return [pair for pair in pairs if pair[0] in component]


def _reconciliation_plan(
    *, pairs: list[tuple[uuid.UUID, uuid.UUID]] | None = None
) -> list[IdentityMatch]:
    if pairs is None:
        pairs = _person_name_pairs()
    entity_ids = {entity_id for pair in pairs for entity_id in pair}
    people = Entity.objects.in_bulk(entity_ids)
    identities = {entity_id: [] for entity_id in entity_ids}
    for identity in SourceIdentity.objects.filter(entity_id__in=entity_ids).order_by("pk"):
        identities[identity.entity_id].append(identity)
    edges = []
    neighbours = {entity_id: set() for entity_id in entity_ids}
    register_matches = {entity_id: {} for entity_id in entity_ids}
    for first_id, second_id in pairs:
        first, second = people[first_id], people[second_id]
        basis = _pair_basis(first, second, identities)
        if not basis:
            continue
        edges.append((first, second, basis))
        for own, other in ((first_id, second_id), (second_id, first_id)):
            neighbours[own].add(other)
            for register in _exclusive_registers(identities[other]):
                register_matches[own].setdefault(register, set()).add(other)
    matches = []
    for first, second, basis in edges:
        unique = True
        for own, other in ((first.pk, second.pk), (second.pk, first.pk)):
            registers = _exclusive_registers(identities[other])
            if any(len(register_matches[own][register]) != 1 for register in registers):
                unique = False
            if (
                not registers
                and not _exclusive_registers(identities[own])
                and len(neighbours[own]) != 1
            ):
                unique = False
        if unique:
            retained, removed = sorted(
                (first, second),
                key=lambda entity: _anchor_order(entity, identities),
            )
            matches.append(IdentityMatch(removed, retained, basis))
    matches.sort(
        key=lambda match: (
            _anchor_order(match.to_entity, identities),
            _anchor_order(match.from_entity, identities),
        )
    )
    # A dry run names each discarded profile only once, preferring its strongest target.
    seen = set()
    result = []
    for match in matches:
        if match.from_entity.pk not in seen:
            result.append(match)
            seen.add(match.from_entity.pk)
    ar = (
        SourceIdentity.objects.select_related("entity")
        .filter(
            source=PARLIAMENT,
            external_id=AR_INSTITUTION_ID,
        )
        .first()
    )
    nipc = (
        SourceIdentity.objects.select_related("entity")
        .filter(
            source=IdentityScheme.NIPC,
            external_id=AR_NIPC,
        )
        .first()
    )
    if (
        ar is not None
        and nipc is not None
        and ar.entity_id != nipc.entity_id
        and ar.entity.is_public
        and nipc.entity.is_public
        and nipc.entity.kind != Entity.Kind.PERSON
    ):
        result.insert(
            0,
            IdentityMatch(
                nipc.entity,
                ar.entity,
                f"Correspondência automática: NIPC oficial da AR {AR_NIPC}",
            ),
        )
    return result


def _merge_identity(match: IdentityMatch) -> None:
    """Move source references without rewriting claims, inside the editorial lock only."""
    from .event_summaries import co_party_entities, rebuild_event_summaries
    from .models import EventEntitySummary, EventPairSummary

    removed, retained = match.from_entity, match.to_entity
    affected = co_party_entities([removed.pk, retained.pk])
    moved_ids = list(SourceIdentity.objects.filter(entity=removed))
    # This is the only deliberate bypass of the used-identity mapping guard; the
    # IdentityMerge audit, preserved claims and redirect are committed atomically.
    SourceIdentity.objects.filter(entity=removed).update(entity=retained)
    for alias in EntityAlias.objects.filter(entity=removed):
        duplicate = EntityAlias.objects.filter(
            entity=retained,
            normalised=alias.normalised,
            scheme=alias.scheme,
            external_id=alias.external_id,
        ).exists()
        if duplicate:
            alias.delete()
        else:
            EntityAlias.objects.filter(pk=alias.pk).update(entity=retained)
    for identity in moved_ids:
        record_alias(
            retained,
            removed.name,
            scheme=identity.source,
            external_id=identity.external_id,
        )
    for suggestion in IdentitySuggestion.objects.filter(candidate=removed):
        duplicate = IdentitySuggestion.objects.filter(
            candidate=retained,
            scheme=suggestion.scheme,
            external_id=suggestion.external_id,
        ).first()
        if duplicate is None:
            IdentitySuggestion.objects.filter(pk=suggestion.pk).update(candidate=retained)
        else:
            if suggestion.status == REJECTED and duplicate.status != REJECTED:
                IdentitySuggestion.objects.filter(pk=duplicate.pk).update(
                    status=REJECTED,
                    reviewed_by=suggestion.reviewed_by,
                    reviewed_at=suggestion.reviewed_at,
                )
            suggestion.delete()
    relationships = Relationship.objects.filter(Q(subject=removed) | Q(object=removed))
    self_links = relationships.filter(
        Q(subject=removed, object=retained) | Q(subject=retained, object=removed)
    )
    self_ids = list(self_links.values_list("pk", flat=True))
    for row in self_links.exclude(status=Relationship.Status.REJECTED):
        ReviewEvent.objects.create(relationship=row, action=ReviewEvent.Action.WITHDRAW)
    self_links.update(status=Relationship.Status.REJECTED)
    # Preserve each source-owned claim/evidence; independent observations are not duplicates.
    relationships.exclude(pk__in=self_ids).filter(subject=removed).update(subject=retained)
    relationships.exclude(pk__in=self_ids).filter(object=removed).update(object=retained)
    skipped = {
        SourceIdentity,
        EntityAlias,
        IdentitySuggestion,
        IdentityMerge,
        Relationship,
        EventEntitySummary,
        EventPairSummary,
    }
    for manager, field_name in _entity_references():
        if manager.model in skipped:
            continue
        manager.filter(**{field_name: removed}).update(**{field_name: retained})
    Entity.objects.filter(pk=removed.pk).update(is_public=False)
    EntityRedirect.objects.update_or_create(old_slug=removed.slug, defaults={"entity": retained})
    IdentityMerge.objects.create(
        from_slug=removed.slug,
        from_name=removed.name,
        to_entity=retained,
        basis=match.basis,
    )
    rebuild_event_summaries(entities=affected)
    # Original audit targets, self-links and protected references retain a hidden shell.
    if not any(
        manager.filter(**{field_name: removed}).exists()
        for manager, field_name in _entity_references()
    ):
        Entity.objects.filter(pk=removed.pk).delete()


def reconcile_identities(*, apply: bool = False) -> list[IdentityMatch]:
    """Plan or apply uniquely corroborated merges, recomputing to a bounded fixpoint.

    Each merge rechecks its complete current namesake component in its own bulk-
    import transaction. Global scans detect newly enabled matches between passes.
    Equal anchor ranks keep the oldest SourceIdentity creation-order key.
    """
    if not apply:
        with editorial_transaction(long_running=True):
            return _reconciliation_plan()
    result = []
    # Every merge removes one public candidate; this bounds the fixpoint iterations.
    limit = Entity.objects.filter(is_public=True).count()
    while limit:
        with editorial_transaction(long_running=True):
            plan = _reconciliation_plan()
        if not plan:
            break
        changed = False
        for proposed in plan:
            with editorial_transaction(long_running=True):
                pairs = _namesake_component(
                    _person_name_pairs(), (proposed.from_entity.pk, proposed.to_entity.pk)
                )
                current = _reconciliation_plan(pairs=pairs)
                if not current:
                    continue
                match = current[0]
                _merge_identity(match)
            result.append(match)
            changed = True
            limit -= 1
            if not limit:
                break
        if not changed:
            break
    return result
