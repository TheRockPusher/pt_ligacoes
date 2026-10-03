"""EpT public holder list ("Lista de titulares por entidade e cargo") as linked office claims.

One complete, id-ordered dump of ``POST /publicquery``. Holders use the same
official ids as the declarations importer. Party organs and candidacies are skipped.
AR and Government rows corroborate holder identities against the shared institutions;
their mandates remain owned by the AR/Government importers, not duplicated here.
No natural-person NIF is read, kept or logged.
"""

import json
import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import batched
from typing import cast

from django.core.exceptions import ValidationError
from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import EMPTY_RESULT, ObservationInput, _source_values
from .government import revised
from .identity import (
    AR_INSTITUTION_ID,
    OfficeContext,
    anchor_schemes,
    normalise_name,
    official_entities_bulk,
    official_entity,
    resolve_person,
)
from .models import (
    Entity,
    EntityAlias,
    Evidence,
    IdentityScheme,
    Relationship,
    ReviewEvent,
    Source,
    SourceIdentity,
    SourceObservation,
    SourceSyncState,
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
# These national mandates belong to the AR/Government importers.
PARLIAMENT_ENTITIES = frozenset({510, 4508})
GOVERNMENT_ENTITIES = frozenset({4216, 4509})
GOVERNMENT_LABEL = re.compile(r"^([IVXLCDM]+) Governo Constitucional$")

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
    # AR/Government mandates: corroboration only, never duplicate office claims.
    crosswalk: tuple[HolderRow, ...]
    total: int
    skipped: int
    # Complete declarant enumeration, even where their listed offices are excluded.
    holders: tuple[tuple[str, str], ...] = ()


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
            holder = _label(row["holder"])
            if holders.setdefault(holder_id, holder) != holder:
                raise EptOfficesError("O mesmo titular EpT tem designações diferentes.")
            if entity_id in PARTY_ENTITIES or (
                isinstance(row["entity"], str) and PARTY_LABEL.search(row["entity"])
            ):
                skipped += 1
                continue
            parsed = _holder_row(row, holder_id=holder_id, entity_id=entity_id, role_id=role_id)
            if CANDIDACY_ROLE.search(parsed.role):
                skipped += 1
                continue
            if holders[holder_id] != parsed.holder:
                raise EptOfficesError("O mesmo titular EpT tem designações diferentes.")
            if entities.setdefault(entity_id, parsed.entity) != parsed.entity:
                raise EptOfficesError("A mesma entidade EpT tem designações diferentes.")
            if (
                entity_id in PARLIAMENT_ENTITIES
                or entity_id in GOVERNMENT_ENTITIES
                or GOVERNMENT_LABEL.fullmatch(parsed.entity)
                or parsed.entity.startswith("Assembleia da República")
            ):
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
        holders=tuple(sorted(holders.items(), key=lambda item: int(item[0]))),
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


def fetch_holders(*, as_of: date) -> dict[str, str]:
    """Distinct holder ids and public names from every page of the complete listing."""
    return dict(fetch_snapshot(as_of=as_of).holders)


def _institutions(rows: tuple[HolderRow, ...]) -> dict[int, Entity]:
    """Use shared national institutions, with EpT anchors only for other entities."""
    result = {
        int(key.removeprefix("entity:")): entity
        for key, entity in official_entities_bulk(
            EPT,
            {
                f"entity:{row.entity_id}": (row.entity, *classify(row.entity))
                for row in rows
                if row.entity_id not in PARLIAMENT_ENTITIES
                and not row.entity.startswith("Assembleia da República")
                and not GOVERNMENT_LABEL.fullmatch(row.entity)
            },
        ).items()
    }
    for row in rows:
        if row.entity_id in result:
            continue
        match = GOVERNMENT_LABEL.fullmatch(row.entity)
        if match:
            roman = match[1]
            values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
            number = sum(
                -values[char]
                if index + 1 < len(roman) and values[char] < values[roman[index + 1]]
                else values[char]
                for index, char in enumerate(roman)
            )
            scheme, key, classification = (
                IdentityScheme.GOVERNMENT,
                f"government:gc{number:02d}",
                Entity.Classification.GOVERNMENT,
            )
        else:
            scheme, key, classification = (
                IdentityScheme.PARLIAMENT,
                AR_INSTITUTION_ID,
                Entity.Classification.PARLIAMENT,
            )
        result[row.entity_id] = official_entity(
            scheme,
            key,
            name=row.entity,
            kind=Entity.Kind.ORGANISATION,
            classification=classification,
        )
    return result


def holder_office_contexts(rows: tuple[HolderRow, ...]) -> tuple[OfficeContext, ...]:
    """Resolve safe listing offices to shared institutions inside the caller's transaction."""
    institutions = _institutions(rows)
    return tuple(OfficeContext(institutions[row.entity_id], row.start, row.end) for row in rows)


def _identities(holder_ids: list[str]) -> dict[str, SourceIdentity]:
    found: dict[str, SourceIdentity] = {}
    for chunk in batched(holder_ids, BATCH, strict=False):
        for identity in SourceIdentity.objects.select_related("entity", "reviewed_by").filter(
            source=EPT, external_id__in=chunk
        ):
            found[identity.external_id] = identity
    return found


def _resolve_holders(
    rows: dict[str, list[HolderRow]], institutions: dict[int, Entity]
) -> dict[str, SourceIdentity]:
    """Resolve namesakes with dated office corroboration; create the rest in batches."""
    holder_ids = list(rows)
    existing = _identities(holder_ids)
    people = Entity.objects.filter(kind=Entity.Kind.PERSON, is_public=True)
    namesakes = {normalise_name(name) for name in people.values_list("name", flat=True)}
    namesakes.update(
        EntityAlias.objects.filter(entity__in=people).values_list("normalised", flat=True)
    )
    bulk: dict[str, tuple[str, str, str]] = {}
    for holder_id, offices in rows.items():
        if holder_id in existing:
            continue
        first = offices[0]
        if normalise_name(first.holder) in namesakes:
            resolve_person(
                EPT,
                holder_id,
                name=first.holder,
                basis="; ".join(_describe(row) for row in offices),
                offices=[
                    OfficeContext(institutions[row.entity_id], row.start, row.end)
                    for row in offices
                ],
            )
        else:
            bulk[holder_id] = (first.holder, Entity.Kind.PERSON, "")
    official_entities_bulk(EPT, bulk)
    identities = _identities(holder_ids)
    if any(identity.entity.kind != Entity.Kind.PERSON for identity in identities.values()):
        raise ValidationError("O identificador de titular EpT não corresponde a uma pessoa.")
    EntityAlias.objects.bulk_create(
        [
            EntityAlias(
                entity=identities[holder_id].entity,
                name=offices[0].holder,
                normalised=normalise_name(offices[0].holder),
                scheme=EPT,
                external_id=holder_id,
            )
            for holder_id, offices in rows.items()
        ],
        batch_size=1000,
        ignore_conflicts=True,
    )
    return identities


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
    identity: SourceIdentity,
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
    item = ObservationInput(
        external_id=f"row:{row.row_id}",
        revision="",
        category="office_holding",
        passage=passage,
        source_url=DATASET.url,
        publisher=DATASET.publisher,
        reference=reference,
        title=DATASET.title,
        identity=identity,
        subject_name="",
        subject_reference="",
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
    return revised(item)


def _sync_offices(
    scopes: dict[str, tuple[ObservationInput, ...]], snapshot: OfficesSnapshot
) -> dict[str, int]:
    """Bulk equivalent of scoped observation sync, restricted to anchored EpT offices.

    The caller holds the editorial/import lock. Model validation runs without per-row
    FK/uniqueness queries; cached endpoints and database constraints cover those checks.
    Revisions stay immutable, absence invalidates approval, and rejection is permanent.
    """
    as_of = snapshot.as_of
    states = {
        state.scope: state
        for state in SourceSyncState.objects.filter(source=EPT, scope__startswith="offices:")
    }
    if any(as_of < state.as_of for state in states.values()):
        raise ValidationError("Não é possível substituir uma observação mais recente.")
    prior = list(
        SourceObservation.objects.filter(source=EPT, scope__startswith="offices:")
        .select_related("relationship", "evidence__source")
        .order_by("pk")
    )
    revisions = {(row.scope, row.external_id, row.revision): row for row in prior}
    current = {(row.scope, row.external_id): row for row in prior if row.is_current}
    incoming = {
        (scope, item.external_id): item for scope, items in scopes.items() for item in items
    }
    if len(incoming) != sum(map(len, scopes.values())):
        raise ValidationError("Identificadores de cargos EpT repetidos.")
    result = dict(EMPTY_RESULT)
    withdrawn = []
    updates = []
    new = []
    publish = []
    rejected = {
        (row.scope, row.external_id)
        for row in prior
        if row.relationship is not None and row.relationship.status == Relationship.Status.REJECTED
    }
    for key, old in current.items():
        item = incoming.get(key)
        if item is None or item.revision != old.revision:
            old.is_current = False
            old.as_of = as_of
            old.reviewed_by_id = None
            old.reviewed_at = None
            withdrawn.append(old)
            result["ceased" if item is None else "changed"] += 1
    for (scope, external_id), item in incoming.items():
        values = _source_values(item)
        observation = revisions.get((scope, external_id, item.revision))
        if observation is not None:
            if any(getattr(observation, key) != value for key, value in values.items()):
                raise ValidationError("A mesma revisão EpT contém passagens diferentes.")
            returning = not observation.is_current
            observation.is_current = True
            observation.as_of = as_of
            if returning:
                observation.reviewed_by_id = None
                observation.reviewed_at = None
            updates.append(observation)
            if returning or observation.relationship_id is None:
                publish.append(observation)
        else:
            observation = SourceObservation(
                source=EPT,
                scope=scope,
                external_id=external_id,
                revision=item.revision,
                as_of=as_of,
                retrieved_at=snapshot.retrieved_at,
                dataset=item.dataset,
                **values,
            )
            observation.identity = item.identity
            observation.object = item.object
            observation.full_clean(
                exclude=["identity", "object", "term", "relationship", "evidence", "reviewed_by"],
                validate_unique=False,
                validate_constraints=False,
            )
            new.append(observation)
            publish.append(observation)
            result["created"] += 1
    # Invalidate before activating new revisions (partial unique current-row constraint).
    invalidate = {
        row.relationship_id
        for row in withdrawn
        if row.relationship is not None and row.relationship.status == Relationship.Status.PUBLISHED
    }
    for row in publish:
        if (
            row.relationship is not None
            and row.relationship.status == Relationship.Status.PUBLISHED
        ):
            invalidate.add(row.relationship_id)
    for chunk in batched(invalidate, 1000, strict=False):
        Relationship.objects.filter(pk__in=chunk).update(
            status=Relationship.Status.DRAFT, reviewed_by=None, reviewed_at=None
        )
    audits = [
        ReviewEvent(relationship_id=pk, action=ReviewEvent.Action.INVALIDATE) for pk in invalidate
    ]
    for chunk in batched(
        [row.evidence_id for row in withdrawn if row.evidence_id], 1000, strict=False
    ):
        Evidence.objects.filter(pk__in=chunk).update(is_public=False)
    fields = ["is_current", "as_of", "reviewed_by", "reviewed_at"]
    SourceObservation.objects.bulk_update(withdrawn, fields, batch_size=1000)
    SourceObservation.objects.bulk_update(updates, fields, batch_size=1000)
    SourceObservation.objects.bulk_create(new, batch_size=1000)
    drafts = []
    evidence = []
    links = []
    public_evidence = []
    publications = []
    source = None
    for observation in publish:
        key = (observation.scope, observation.external_id)
        item = incoming[key]
        identity, organisation = item.identity, item.object
        if identity is None or organisation is None:
            raise ValidationError("Um cargo EpT exige titular e instituição identificados.")
        relationship = observation.relationship
        if relationship is None:
            if source is None:
                source = Source(
                    dataset=DATASET.key,
                    title=DATASET.title,
                    url=DATASET.url,
                    publisher=DATASET.publisher,
                    retrieved_at=snapshot.retrieved_at,
                    is_public=True,
                )
                source.full_clean(validate_unique=False, validate_constraints=False)
                Source.objects.bulk_create([source])
            relationship = Relationship(
                subject=identity.entity,
                object=organisation,
                kind=item.kind,
                description=item.passage,
                start_date=item.effective_start,
                end_date=item.effective_end,
                role=item.role,
                role_class=item.role_class,
                start_precision=item.start_precision,
                end_precision=item.end_precision,
                temporal_status=item.temporal_status,
                status=Relationship.Status.REJECTED
                if key in rejected
                else Relationship.Status.DRAFT,
            )
            relationship.full_clean(
                exclude=["subject", "object", "term", "reviewed_by"],
                validate_unique=False,
                validate_constraints=False,
            )
            citation = Evidence(
                relationship=relationship,
                source=source,
                excerpt=item.passage,
                page_reference=item.reference,
                is_public=True,
            )
            citation.full_clean(
                exclude=["relationship", "source"],
                validate_unique=False,
                validate_constraints=False,
            )
            drafts.append(relationship)
            evidence.append(citation)
            observation.relationship = relationship
            observation.evidence = citation
            links.append(observation)
            result["drafts"] += 1
        else:
            citation = observation.evidence
            if citation is None:
                raise ValidationError("Cargo EpT sem evidência editorial.")
            citation.is_public = True
            public_evidence.append(citation)
            if relationship.pk in invalidate:
                relationship.status = Relationship.Status.DRAFT
        if (
            key not in rejected
            and relationship.status == Relationship.Status.DRAFT
            and identity.entity.is_public
            and organisation.is_public
            and citation.source.is_public
        ):
            publications.append(relationship.pk)
            audits.append(
                ReviewEvent(relationship=relationship, action=ReviewEvent.Action.AUTO_PUBLISH)
            )
    Relationship.objects.bulk_create(drafts, batch_size=1000)
    Evidence.objects.bulk_create(evidence, batch_size=1000)
    Evidence.objects.bulk_update(public_evidence, ["is_public"], batch_size=1000)
    SourceObservation.objects.bulk_update(links, ["relationship", "evidence"], batch_size=1000)
    now = timezone.now()
    for chunk in batched(publications, 1000, strict=False):
        Relationship.objects.filter(pk__in=chunk, status=Relationship.Status.DRAFT).update(
            status=Relationship.Status.PUBLISHED, reviewed_by=None, reviewed_at=now
        )
    result["published"] = len(publications)
    ReviewEvent.objects.bulk_create(audits, batch_size=1000)
    identity_ids = {item.identity.pk for item in incoming.values() if item.identity}
    entity_ids = {item.object.pk for item in incoming.values() if item.object}
    for chunk in batched(identity_ids, 1000, strict=False):
        SourceIdentity.objects.filter(pk__in=chunk, used_at__isnull=True).update(used_at=now)
    for chunk in batched(entity_ids, 1000, strict=False):
        SourceIdentity.objects.filter(
            entity_id__in=chunk, source__in=anchor_schemes, used_at__isnull=True
        ).update(used_at=now)
    for scope in scopes:
        states.setdefault(scope, SourceSyncState(source=EPT, scope=scope, as_of=as_of))
    SourceSyncState.objects.bulk_create(
        [state for state in states.values() if state.pk is None], batch_size=1000
    )
    for state in states.values():
        state.as_of = as_of
    SourceSyncState.objects.bulk_update(list(states.values()), ["as_of"], batch_size=1000)
    return result


def apply_snapshot(snapshot: OfficesSnapshot) -> dict[str, int]:
    """Apply the complete listing atomically, with national mandates as crosswalks only."""
    with import_transaction():
        all_rows = snapshot.crosswalk + snapshot.offices
        organisations = _institutions(all_rows)
        holders: dict[str, list[HolderRow]] = defaultdict(list)
        for row in all_rows:
            holders[row.holder_id].append(row)
        identities = _resolve_holders(holders, organisations)
        office_holders: dict[str, list[HolderRow]] = defaultdict(list)
        for row in snapshot.offices:
            office_holders[row.holder_id].append(row)
        scopes = {
            f"offices:{holder_id}": tuple(
                _observation(
                    row,
                    identity=identities[holder_id],
                    organisation=organisations[row.entity_id],
                    snapshot=snapshot,
                )
                for row in rows
            )
            for holder_id, rows in office_holders.items()
        }
        result = _sync_offices(scopes, snapshot)
        return result
