"""EpT public holder list ("Lista de titulares por entidade e cargo") as linked office claims.

One complete, id-ordered dump of ``POST /publicquery`` (verified 2026-09-28: 17 123 rows,
four pages of 5000). Holders are EpT holder ids (the same plain ids the declarations
importer uses) and entities are ``ept`` ``entity:<id>`` anchors. Party organs and
candidacies are skipped (maintainer decision: no party affiliation). Assembleia da
República and Government rows only propose editorial identity links: their offices come
from the AR and Government importers. No NIF is read, kept or logged.
"""

import hashlib
import json
import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import batched
from typing import cast

from django.db.models import Q, QuerySet
from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_scoped_snapshot
from .identity import (
    AR_INSTITUTION_ID,
    normalise_name,
    official_entities_bulk,
    resolve_person,
    suggest_person,
)
from .models import (
    Entity,
    IdentityScheme,
    IdentitySuggestion,
    Relationship,
    SourceIdentity,
    TemporalStatus,
    import_transaction,
)
from .official_http import OfficialHTTPError, download
from .parliament_parse import JSONObject, JSONValue, canonical_json

API_URL = "https://api.entidadetransparencia.pt/publicquery"
DATASET = DATASETS["ept_titulares"]
EPT = IdentityScheme.EPT
PAGE_SIZE = 5000
MAX_PAGES = 20
MAX_BYTES = 8 * 1024 * 1024
TOTAL_TIMEOUT = 600
BATCH = 5000
ROW_KEYS = frozenset(
    {"id", "entityId", "roleId", "holderId", "entity", "role", "holder", "beginDate", "endDate"}
)
# Never read: tolerated only so its presence cannot leak through an error or a projection.
IGNORED_KEYS = frozenset({"nif"})

# Party organs (maintainer decision: no party affiliation). The API has no entity type,
# so this list is hand-kept. Source of each id: POST /publicquery/getallentities with
# {"BoardId": n} (party-organ boards 51 Comissão Política Nacional, 54 Secretariado
# Nacional, 113 Comissão Executiva Nacional, 118 Comissão Nacional de Auditoria
# Financeira, 146 Presidente Regional do Partido, 151 Comissão Nacional) or, where no
# party-only board exists, the entity label in the holder list; verified 2026-09-28.
PARTY_ENTITIES: dict[int, str] = {
    4284: "Partido Social Democrata — órgãos 51, 118, 151",
    4289: "Partido Socialista — órgãos 54, 151",
    4301: "CDS - Partido Popular — designação na lista de titulares",
    4343: "LIVRE - Partido Político — designação na lista de titulares",
    4349: "Partido Pessoas - Animais - Natureza (PAN) — órgão 51",
    4411: "Volt Portugal — órgão 51",
    4416: "Bloco de Esquerda — órgãos 51, 54",
    4450: 'Partido Ecologista "Os Verdes" — órgão 113',
    4462: "Partido Comunista Português — designação na lista de titulares",
    4503: "CDS-PP Açores — órgão 146",
    4561: "Partido Trabalhista Português — órgão 51",
    4948: "Partido Socialista-Açores (PS-A) — designação na lista de titulares",
}
# Safety net for party entities added after the list was curated.
PARTY_LABEL = re.compile(r"\bPartido\b")
# Candidacies are political competition, not office (maintainer decision).
CANDIDACY_ROLE = re.compile(r"^Candidat[oa]\b")
# Offices loaded by the AR and Government importers; EpT rows only propose identity links.
PARLIAMENT_ENTITIES = frozenset({510, 4508})
GOVERNMENT_ENTITIES = frozenset({4216, 4509})

# Classification heuristics over EpT entity labels (set only when an entity is created).
STATE_COMPANY = re.compile(
    r"(?:^|[\s,])(?:S\.\s?A\.?|E\.\s?M\.(?:\s?T\.)?|E\.\s?E\.\s?M\.|E\.\s?I\.\s?M\.|"
    r"E\.\s?P\.\s?E\.(?:\s?R\.\s?A\.\s?M\.)?|EPERAM|EPE|EEM|EIM|Lda\.?)(?=$|[\s,])"
)
COOPERATIVE = re.compile(r"(?:^|[\s,])(?:C\.?R\.?L\.?|CIPRL)(?=$|[\s,])")
MUNICIPALITY = re.compile(r"^(?:Câmara|Assembleia) Municipal\b")
PARISH = re.compile(r"^(?:(?:Junta|Assembleia) de Freguesia|União d[ae]s Freguesias)\b")
GOVERNMENT_OFFICE = re.compile(
    r"\bGabinete\b.*\b(?:Primeiro-Ministro|Ministr[oa]|Secretári[oa] de Estado|"
    r"Subsecretári[oa]|Subsecretaria|Secretári[oa] Regional|Secretaria Regional|Governo)\b"
)
REGULATOR = re.compile(
    r"^(?:Entidade Reguladora\b|Autoridade da Concorrência$|Autoridade Nacional de "
    r"Comunicações$|Autoridade Nacional de Aviação Civil$|Autoridade da Mobilidade e dos "
    r"Transportes$|Autoridade de Supervisão de Seguros e Fundos de Pensões$|Comissão do "
    r"Mercado de Valores Mobiliários$|Banco de Portugal$)"
)
HIGHER_EDUCATION = re.compile(
    r"\b(?:Universidade|Politécnico)\b|^(?:Faculdade|Escola Superior|Instituto Superior)\b"
)
REGIONAL_PARLIAMENT = re.compile(r"^Assembleia Legislativa\b")
REGIONAL_GOVERNMENT = re.compile(r"^(?:(?:Vice-)?Presidência do )?Governo Regional\b")

# Role classes over accent-free, casefolded role labels; first match wins.
ROLE_CLASSES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsuplente\b"), Relationship.RoleClass.SUBSTITUTE),
    (
        re.compile(
            r"^(?:chefe de gabinete|adjunt[oa](?: de\b|$)|tecnic[oa] especialista|assessor)"
        ),
        Relationship.RoleClass.STAFF,
    ),
    (
        re.compile(
            r"^(?:primeir[oa]-ministr|ministr|presidente|chair|governador|reitor|bastonari|"
            r"provedor|procurador-geral|chefe do estado-maior|representante da republica)"
        ),
        Relationship.RoleClass.LEADERSHIP,
    ),
    (
        re.compile(r"^(?:vice|pro-|sub|coordenador|secretari[oa] de estado)|\badjunt[oa]\b"),
        Relationship.RoleClass.DEPUTY_LEADERSHIP,
    ),
    (
        re.compile(
            r"^(?:diretor|inspetor|secretari[oa]- ?geral|comandante|gerente|gestor|dirigente)"
        ),
        Relationship.RoleClass.LEADERSHIP,
    ),
    (
        re.compile(
            r"^(?:membro|vogal|deputad|efetiv|secretari[oa] da mesa|primeir[oa]-secretari|"
            r"vereador|administrador|tesoureir|secretari[oa]\b|juiz)"
        ),
        Relationship.RoleClass.MEMBER,
    ),
)

LISTING_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d{1,6})?")
# Lisbon midnight stored as UTC without an offset (summer/winter): the next civil day.
LISBON_MIDNIGHT = frozenset({"22:00:00", "23:00:00"})


class EptOfficesError(ValueError):
    """Safe, payload-free failure for an incomplete or ambiguous holder list."""


@dataclass(frozen=True)
class HolderRow:
    row_id: int
    holder_id: str
    holder: str
    entity_id: int
    entity: str
    role_id: int
    role: str
    start: date | None
    start_literal: str
    end: date | None
    end_literal: str
    # The published end preceded the start and was not used.
    end_dropped: bool


@dataclass(frozen=True)
class OfficesSnapshot:
    as_of: date
    retrieved_at: datetime
    # Rows loaded as office claims.
    offices: tuple[HolderRow, ...]
    # Assembleia da República and Government rows: identity suggestions only.
    crosswalk: tuple[HolderRow, ...]
    total: int
    skipped: int


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise EptOfficesError("Estrutura pública EpT inesperada.")
    return value


def _integer(value: JSONValue, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise EptOfficesError("Número público EpT inválido.")
    return value


def _label(value: JSONValue) -> str:
    if not isinstance(value, str):
        raise EptOfficesError("Designação pública EpT inválida.")
    text = " ".join(value.split())
    if not text or len(text) > 240:
        raise EptOfficesError("Designação pública EpT vazia ou excessiva.")
    # A label must never carry a tax number into editorial storage.
    if re.search(r"(?<!\d)\d{9}(?!\d)", text):
        raise EptOfficesError("Designação EpT contém um identificador não permitido.")
    return text


def _date(value: JSONValue) -> tuple[date | None, str]:
    if value is None:
        return None, ""
    match = LISTING_DATE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise EptOfficesError("Data pública EpT inválida.")
    try:
        parsed = datetime.fromisoformat(match[0])
    except ValueError as exc:
        raise EptOfficesError("Data pública EpT inválida.") from exc
    day = parsed.date()
    if match[2] in LISBON_MIDNIGHT:
        day += timedelta(days=1)
    return day, match[0]


def _envelope(value: JSONValue) -> JSONValue:
    response = _object(value)
    if type(response.get("code")) is not int or response["code"] != 0 or "data" not in response:
        raise EptOfficesError("A consulta pública EpT não foi concluída.")
    return response["data"]


def role_class(label: str) -> str:
    plain = "".join(
        char
        for char in unicodedata.normalize("NFKD", label.casefold())
        if not unicodedata.combining(char)
    )
    for pattern, value in ROLE_CLASSES:
        if pattern.search(plain):
            return value
    return Relationship.RoleClass.OTHER


def classify(label: str) -> tuple[str, str]:
    """(kind, classification) for a new EpT entity, from its published label only."""
    if STATE_COMPANY.search(label):
        return Entity.Kind.COMPANY, Entity.Classification.STATE_COMPANY
    if COOPERATIVE.search(label):
        return Entity.Kind.ORGANISATION, Entity.Classification.COOPERATIVE
    if MUNICIPALITY.search(label):
        return Entity.Kind.ORGANISATION, Entity.Classification.MUNICIPALITY
    if PARISH.search(label):
        return Entity.Kind.ORGANISATION, Entity.Classification.PARISH
    if GOVERNMENT_OFFICE.search(label):
        return Entity.Kind.ORGANISATION, Entity.Classification.GOVERNMENT_OFFICE
    if REGULATOR.search(label):
        return Entity.Kind.ORGANISATION, Entity.Classification.REGULATOR
    if HIGHER_EDUCATION.search(label):
        return Entity.Kind.UNIVERSITY, Entity.Classification.HIGHER_EDUCATION
    if REGIONAL_PARLIAMENT.search(label):
        return Entity.Kind.ORGANISATION, Entity.Classification.PARLIAMENT
    if REGIONAL_GOVERNMENT.search(label):
        return Entity.Kind.ORGANISATION, Entity.Classification.GOVERNMENT
    return Entity.Kind.ORGANISATION, Entity.Classification.PUBLIC_BODY


def _holder_row(row: JSONObject, *, holder_id: str, entity_id: int, role_id: int) -> HolderRow:
    start, start_literal = _date(row["beginDate"])
    end, end_literal = _date(row["endDate"])
    dropped = start is not None and end is not None and end < start
    return HolderRow(
        row_id=_integer(row["id"], minimum=1),
        holder_id=holder_id,
        holder=_label(row["holder"]),
        entity_id=entity_id,
        entity=_label(row["entity"]),
        role_id=role_id,
        role=_label(row["role"]),
        start=start,
        start_literal=start_literal,
        end=None if dropped else end,
        end_literal=end_literal,
        end_dropped=dropped,
    )


def parse_listing(
    pages: list[JSONValue], *, as_of: date, retrieved_at: datetime
) -> OfficesSnapshot:
    """Offline projection of the complete list; party rows are dropped unread."""
    if not pages or len(pages) > MAX_PAGES:
        raise EptOfficesError("Lista de titulares EpT incompleta.")
    expected: tuple[int, int] | None = None
    last_id = 0
    count = 0
    offices: list[HolderRow] = []
    crosswalk: list[HolderRow] = []
    skipped = 0
    holders: dict[str, str] = {}
    entities: dict[int, str] = {}
    for number, payload in enumerate(pages, 1):
        page = _object(_envelope(payload))
        total = _integer(page.get("total"))
        page_count = _integer(page.get("pageCount"))
        if _integer(page.get("pageSize"), minimum=1) != PAGE_SIZE:
            raise EptOfficesError("A fonte EpT não respeitou a dimensão de página pedida.")
        if page_count > MAX_PAGES or page_count != (total + PAGE_SIZE - 1) // PAGE_SIZE:
            raise EptOfficesError("Paginação EpT inconsistente ou excessiva.")
        if expected is None:
            expected = (total, page_count)
        if expected != (total, page_count) or _integer(page.get("pageNumber")) != number:
            raise EptOfficesError("A lista EpT mudou durante a consulta; nada foi aplicado.")
        items = page.get("items")
        if not isinstance(items, list) or len(items) != min(
            PAGE_SIZE, max(0, total - (number - 1) * PAGE_SIZE)
        ):
            raise EptOfficesError("Página EpT truncada.")
        for item in items:
            row = _object(item)
            if set(row) - IGNORED_KEYS != ROW_KEYS:
                raise EptOfficesError("Os campos da lista de titulares EpT mudaram.")
            row_id = _integer(row["id"], minimum=1)
            # Sorted by id: a repeated or earlier id means the pages shifted.
            if row_id <= last_id:
                raise EptOfficesError("A lista EpT mudou durante a consulta; nada foi aplicado.")
            last_id = row_id
            count += 1
            entity_id = _integer(row["entityId"], minimum=1)
            role_id = _integer(row["roleId"], minimum=1)
            holder_id = str(_integer(row["holderId"], minimum=1))
            if entity_id in PARTY_ENTITIES or (
                isinstance(row["entity"], str) and PARTY_LABEL.search(row["entity"])
            ):
                skipped += 1
                continue
            parsed = _holder_row(row, holder_id=holder_id, entity_id=entity_id, role_id=role_id)
            if CANDIDACY_ROLE.search(parsed.role):
                skipped += 1
                continue
            if holders.setdefault(holder_id, parsed.holder) != parsed.holder:
                raise EptOfficesError("O mesmo titular EpT tem designações diferentes.")
            if entities.setdefault(entity_id, parsed.entity) != parsed.entity:
                raise EptOfficesError("A mesma entidade EpT tem designações diferentes.")
            if entity_id in PARLIAMENT_ENTITIES or entity_id in GOVERNMENT_ENTITIES:
                crosswalk.append(parsed)
            else:
                offices.append(parsed)
    if expected is None or len(pages) != max(1, expected[1]) or count != expected[0]:
        raise EptOfficesError("Lista de titulares EpT incompleta.")
    return OfficesSnapshot(
        as_of=as_of,
        retrieved_at=retrieved_at,
        offices=tuple(offices),
        crosswalk=tuple(crosswalk),
        total=count,
        skipped=skipped,
    )


def _allowed(url: str) -> bool:
    return url == API_URL


def _post_page(number: int, *, deadline: float) -> JSONValue:
    body: JSONObject = {"sortBy": "Id asc", "pageSize": PAGE_SIZE, "pageNumber": number}
    try:
        content = download(
            API_URL,
            allowed=_allowed,
            max_bytes=MAX_BYTES,
            deadline=deadline,
            method="POST",
            body=canonical_json(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    except OfficialHTTPError as exc:
        raise EptOfficesError("Não foi possível consultar a fonte EpT em segurança.") from exc
    try:
        return cast(JSONValue, json.loads(content))
    except (ValueError, RecursionError) as exc:
        raise EptOfficesError("JSON público EpT inválido.") from exc


def fetch_snapshot(*, as_of: date) -> OfficesSnapshot:
    """The complete public holder list, id-ordered; no name or cohort filters."""
    deadline = time.monotonic() + TOTAL_TIMEOUT
    retrieved_at = timezone.now()
    pages = [_post_page(1, deadline=deadline)]
    first = _object(_envelope(pages[0]))
    count = _integer(first.get("pageCount"))
    if count > MAX_PAGES:
        raise EptOfficesError("Lista de titulares EpT excessiva; não foi aplicada.")
    pages.extend(_post_page(number, deadline=deadline) for number in range(2, count + 1))
    return parse_listing(pages, as_of=as_of, retrieved_at=retrieved_at)


def _describe(row: HolderRow) -> str:
    return f"EpT: {row.role} em {row.entity} (registo {row.row_id}, titular {row.holder_id})"


def _office_holders() -> QuerySet[Entity]:
    """Public persons with a published office at the AR or a Government-scheme organisation."""
    institutions = SourceIdentity.objects.filter(
        Q(source=IdentityScheme.PARLIAMENT, external_id=AR_INSTITUTION_ID)
        | (Q(source=IdentityScheme.GOVERNMENT) & ~Q(entity__kind=Entity.Kind.PERSON))
    ).values("entity_id")
    subjects = Relationship.objects.filter(
        status=Relationship.Status.PUBLISHED,
        kind=Relationship.Kind.PUBLIC_OFFICE,
        object_id__in=institutions,
    ).values("subject_id")
    return Entity.objects.filter(kind=Entity.Kind.PERSON, is_public=True, pk__in=subjects)


def _identities(holder_ids: list[str]) -> dict[str, SourceIdentity]:
    found: dict[str, SourceIdentity] = {}
    for chunk in batched(holder_ids, BATCH, strict=False):
        for identity in SourceIdentity.objects.select_related("entity", "reviewed_by").filter(
            source=EPT, external_id__in=chunk
        ):
            found[identity.external_id] = identity
    return found


def _resolve_holders(rows: dict[str, list[HolderRow]]) -> tuple[dict[str, SourceIdentity], int]:
    """Existing identity, else suggest-before-create; bulk-create holders nobody resembles."""
    holder_ids = list(rows)
    existing = _identities(holder_ids)
    missing = [holder_id for holder_id in holder_ids if holder_id not in existing]
    # A superset of suggest_person's candidates: only these holders can produce suggestions.
    namesakes = {
        normalise_name(name)
        for name in Entity.objects.filter(kind=Entity.Kind.PERSON, is_public=True)
        .values_list("name", flat=True)
        .iterator()
    }
    suggested: set[str] = set()
    for chunk in batched(missing, BATCH, strict=False):
        suggested.update(
            IdentitySuggestion.objects.filter(scheme=EPT, external_id__in=chunk).values_list(
                "external_id", flat=True
            )
        )
    bulk: dict[str, tuple[str, str, str]] = {}
    pending = 0
    for holder_id in missing:
        first = rows[holder_id][0]
        if holder_id in suggested or normalise_name(first.holder) in namesakes:
            basis = "; ".join(_describe(row) for row in rows[holder_id])
            if resolve_person(EPT, holder_id, name=first.holder, basis=basis) is None:
                pending += 1
        else:
            bulk[holder_id] = (first.holder, Entity.Kind.PERSON, "")
    official_entities_bulk(EPT, bulk)
    return _identities(holder_ids), pending


def _temporal_status(row: HolderRow, as_of: date) -> str:
    if row.end_dropped or (row.start is not None and row.start > as_of):
        return TemporalStatus.UNKNOWN
    if row.end is not None and row.end < as_of:
        return TemporalStatus.ENDED
    return TemporalStatus.CURRENT


def _date_passage(label: str, day: date | None, literal: str) -> str:
    if day is None:
        return ""
    if literal[11:19] in LISBON_MIDNIGHT:
        return (
            f" {label}: {day.isoformat()} (valor da fonte {literal}, meia-noite de Lisboa "
            "registada em UTC)."
        )
    return f" {label}: {day.isoformat()} (valor da fonte {literal})."


def _observation(
    row: HolderRow,
    *,
    identity: SourceIdentity | None,
    organisation: Entity,
    snapshot: OfficesSnapshot,
) -> ObservationInput:
    kind = (
        Relationship.Kind.DIRECTORSHIP
        if organisation.classification == Entity.Classification.STATE_COMPANY
        else Relationship.Kind.PUBLIC_OFFICE
    )
    status = _temporal_status(row, snapshot.as_of)
    role_group = role_class(row.role)
    passage = f"{row.holder} — {row.role} em {row.entity} (lista pública de titulares da EpT)."
    passage += _date_passage("Início", row.start, row.start_literal)
    passage += _date_passage("Termo", row.end, row.end_literal)
    if row.end_dropped:
        passage += (
            f" A data de termo publicada ({row.end_literal}) antecede o início e não foi usada."
        )
    reference = (
        f"EpT titulares; registo {row.row_id}; titular {row.holder_id}; "
        f"entidade {row.entity_id}; cargo {row.role_id}"
    )
    projection: JSONObject = {
        "passage": passage,
        "reference": reference,
        "identity": identity.pk if identity is not None else None,
        "organisation": str(organisation.pk),
        "kind": kind,
        "role_class": role_group,
        "temporal_status": status,
    }
    return ObservationInput(
        external_id=f"row:{row.row_id}",
        revision=hashlib.sha256(canonical_json(projection).encode()).hexdigest(),
        category="office_holding",
        passage=passage,
        source_url=DATASET.url,
        publisher=DATASET.publisher,
        reference=reference,
        title=DATASET.title,
        identity=identity,
        subject_name="" if identity is not None else row.holder,
        subject_reference="" if identity is not None else f"ept:{row.holder_id}",
        effective_start=row.start,
        effective_end=row.end,
        object=organisation,
        object_name=row.entity,
        object_identifier=f"ept:entity:{row.entity_id}",
        kind=kind,
        dataset=DATASET.key,
        role=row.role,
        role_class=role_group,
        temporal_status=status,
        retrieved_at=snapshot.retrieved_at,
    )


def apply_snapshot(snapshot: OfficesSnapshot) -> dict[str, int]:
    """Atomic: identity suggestions, anchored entities and one scope per holder."""
    with import_transaction():
        crosswalk: dict[str, list[HolderRow]] = defaultdict(list)
        for row in snapshot.crosswalk:
            crosswalk[row.holder_id].append(row)
        restrict = _office_holders()
        suggestions = 0
        for holder_id, rows in crosswalk.items():
            basis = "; ".join(_describe(row) for row in rows) + (
                ". Mesmo nome de pessoa com cargo público publicado na Assembleia da "
                "República ou no Governo."
            )
            suggestions += len(
                suggest_person(
                    EPT, holder_id, name=rows[0].holder, basis=basis, restrict_to=restrict
                )
            )
        holders: dict[str, list[HolderRow]] = defaultdict(list)
        for row in snapshot.offices:
            holders[row.holder_id].append(row)
        organisations = official_entities_bulk(
            EPT,
            {
                f"entity:{row.entity_id}": (row.entity, *classify(row.entity))
                for row in snapshot.offices
            },
        )
        identities, pending = _resolve_holders(holders)
        scopes = {
            f"offices:{holder_id}": tuple(
                _observation(
                    row,
                    identity=identities.get(holder_id),
                    organisation=organisations[f"entity:{row.entity_id}"],
                    snapshot=snapshot,
                )
                for row in rows
            )
            for holder_id, rows in holders.items()
        }
        result = sync_scoped_snapshot(
            source="ept", prefix="offices:", snapshots=scopes, as_of=snapshot.as_of
        )
        result["suggestions"] = suggestions
        result["pending_holders"] = pending
        return result
