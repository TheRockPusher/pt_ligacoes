"""Official-identifier entity resolution. Names only ever produce editorial suggestions."""

import re
import unicodedata
import uuid
from itertools import batched

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    ANCHOR_SCHEMES,
    NON_PERSON_SCHEMES,
    Entity,
    IdentityScheme,
    IdentitySuggestion,
    ParliamentImportState,
    ParliamentMember,
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
_ACCENTS = {
    "a": "aáàâãäAÁÀÂÃÄ",
    "c": "cçCÇ",
    "e": "eéèêëEÉÈÊË",
    "i": "iíìîïIÍÌÎÏ",
    "n": "nñNÑ",
    "o": "oóòôõöOÓÒÔÕÖ",
    "u": "uúùûüUÚÙÛÜ",
}
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


def _name_pattern(token: str) -> str:
    return "".join(f"[{_ACCENTS[char]}]" if char in _ACCENTS else re.escape(char) for char in token)


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
    key = normalise_name(name)
    if key:
        people = Entity.objects.filter(kind=Entity.Kind.PERSON, is_public=True).filter(
            ~Exists(SourceIdentity.objects.filter(source=scheme, entity=OuterRef("pk"))),
            # Narrow in SQL by the last word, accent-insensitively; compare exactly below.
            name__iregex=rf"\m{_name_pattern(key.split()[-1])}\M",
        )
        if restrict_to is not None:
            people = people.filter(pk__in=restrict_to.values("pk"))
        known = set(
            IdentitySuggestion.objects.filter(scheme=scheme, external_id=external_id).values_list(
                "candidate_id", flat=True
            )
        )
        for person in people.only("pk", "name").order_by("pk"):
            if person.pk not in known and normalise_name(person.name) == key:
                IdentitySuggestion.objects.create(
                    scheme=scheme,
                    external_id=external_id,
                    name_as_published=name[:300],
                    candidate=person,
                    basis=basis[:2000],
                )
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


@editorial_transaction()
def resolve_person(
    scheme: str,
    external_id: str,
    *,
    name: str,
    basis: str,
    restrict_to: QuerySet[Entity] | None = None,
) -> Entity | None:
    """Existing identity, else wait for pending suggestions, else a new public person."""
    identity = (
        SourceIdentity.objects.select_related("entity")
        .filter(source=scheme, external_id=external_id)
        .first()
    )
    if identity is not None:
        _same_nature(identity.entity, Entity.Kind.PERSON)
        return identity.entity
    if suggest_person(scheme, external_id, name=name, basis=basis, restrict_to=restrict_to):
        return None
    return official_entity(
        scheme, external_id, name=name, kind=Entity.Kind.PERSON, classification=""
    )


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
    """Record the reviewed identity; every other pending candidate is rejected."""
    actor = authorised_reviewer(reviewer, "core.review_sourceidentity")
    current = _pending(suggestion)
    if SourceIdentity.objects.filter(
        source=current.scheme, external_id=current.external_id
    ).exists():
        raise ValidationError("Este identificador oficial já tem uma correspondência.")
    now = timezone.now()
    identity = SourceIdentity(
        source=current.scheme,
        external_id=current.external_id,
        entity=current.candidate,
        reviewed_by=actor,
        reviewed_at=now,
        review_notes=current.basis,
    )
    identity.save()
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
def ar_person(cadastro_id: str, name: str) -> Entity:
    """The single entity for an AR DepCadId; never matched by name."""
    if not re.fullmatch(r"[0-9]{1,20}", cadastro_id):
        raise ValidationError("Identificador parlamentar inválido.")
    member = ParliamentMember.objects.select_related("entity").filter(pk=cadastro_id).first()
    if member is not None:
        _attach(cadastro_id, member.entity)
        return member.entity
    return official_entity(
        PARLIAMENT,
        cadastro_id,
        name=name,
        kind=Entity.Kind.PERSON,
        classification="",
    )
