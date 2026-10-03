"""Resolve people by anchors or corroboration and organisations by official or declared names."""

import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from itertools import batched

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    ANCHOR_SCHEMES,
    NON_PERSON_SCHEMES,
    Entity,
    EntityAlias,
    IdentityScheme,
    IdentitySuggestion,
    ParliamentImportState,
    ParliamentMember,
    ParliamentStatusInterval,
    Relationship,
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
    """Return a locating signal for S1, S2 or symmetric S3, not just a namesake."""
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
        return state.institution
    return official_entity(
        PARLIAMENT,
        AR_INSTITUTION_ID,
        name="Assembleia da República",
        kind=Entity.Kind.ORGANISATION,
        classification=Entity.Classification.PARLIAMENT,
    )


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
