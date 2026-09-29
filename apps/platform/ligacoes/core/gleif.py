"""GLEIF LEI records for Portuguese legal entities: an identifier crosswalk and parents.

Records come from the LEI API with cursor paging (legal address PT and registered at the
Registo Comercial, RA000487; verified 2026-09-28: 16 983 records). ``registeredAs`` is
kept only when it is a legal-person NIPC; every other record (natural persons, sole
traders, malformed, duplicate or annulled registrations) is dropped with its number
unread beyond validation. Organisations resolve by ``lei``, then by ``nipc``, then are
created with both identities. Accounting-consolidation parents come from the Level 2
relationship golden copy (RR CSV, resolved through the golden-copy metadata API); parents
outside the Portuguese set are fetched by LEI and keyed only by it. Reporting exceptions
are not relationships and are not imported.
"""

import csv
import hashlib
import io
import json
import re
import time
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from itertools import batched
from typing import cast
from urllib.parse import urlencode, urlsplit

from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_observations, sync_scoped_snapshot
from .identity import official_entities_bulk, valid_nipc
from .models import (
    EnrichmentSource,
    Entity,
    IdentityScheme,
    Relationship,
    SourceIdentity,
    SourceObservation,
    TemporalStatus,
    import_transaction,
)
from .official_http import OfficialHTTPError, download
from .parliament_parse import JSONObject, JSONValue, canonical_json

DATASET = DATASETS["gleif_lei"]
GLEIF = EnrichmentSource.GLEIF
LEI = IdentityScheme.LEI
NIPC = IdentityScheme.NIPC
SCOPE_PREFIX = "gleif-parents:"

API_HOST = "api.gleif.org"
API_PATH = "/api/v1/lei-records"
API_URL = f"https://{API_HOST}{API_PATH}"
GOLDEN_HOST = "goldencopy.gleif.org"
GOLDEN_PATH = "/api/v2/golden-copies/publishes/latest"
GOLDEN_URL = f"https://{GOLDEN_HOST}{GOLDEN_PATH}"
GOLDEN_FILES = "/storage/golden-copy-files/"
RR_SUFFIX = "-gleif-goldencopy-rr-golden-copy.csv.zip"
RECORD_URL = "https://search.gleif.org/#/record/{lei}"
JSON_API = "application/vnd.api+json"

COUNTRY = "PT"
REGISTRY = "RA000487"
PAGE_SIZE = 200
MAX_PAGES = 250
MAX_RECORDS = 40000
PARENT_BATCH = 100
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_RR_BYTES = 96 * 1024 * 1024
MAX_RR_CSV_BYTES = 2 * 1024 * 1024 * 1024
TOTAL_TIMEOUT = 1800
# GLEIF documents 60 requests per minute for the API.
REQUEST_INTERVAL = 1.0
NAME_LENGTH = 240
OBJECT_NAME_LENGTH = 300

LEI_PATTERN = re.compile(r"[A-Z0-9]{18}[0-9]{2}")
DAY = re.compile(r"(\d{4}-\d{2}-\d{2})(?:T|$)")
CONSOLIDATION: dict[str, str] = {
    "IS_DIRECTLY_CONSOLIDATED_BY": "direct",
    "IS_ULTIMATELY_CONSOLIDATED_BY": "ultimate",
}
ROLES: dict[str, str] = {
    "direct": "Consolidação contabilística direta",
    "ultimate": "Consolidação contabilística final",
}
PASSAGE_LEVEL: dict[str, str] = {"direct": "diretamente", "ultimate": "em última instância"}
# Relationship records still asserted by their reporter (LAPSED = not renewed).
RR_REGISTRATIONS = frozenset({"PUBLISHED", "LAPSED", "PENDING_TRANSFER", "PENDING_ARCHIVAL"})
DROPPED_REGISTRATIONS = frozenset({"DUPLICATE", "ANNULLED"})
# Natural persons' businesses: never read their registration number or name further.
PERSON_CATEGORIES = frozenset({"SOLE_PROPRIETOR"})
PERSON_LEGAL_FORMS = frozenset({"VALH", "ZILA"})  # ENI, EIRL
RR_COLUMNS = (
    "Relationship.StartNode.NodeID",
    "Relationship.StartNode.NodeIDType",
    "Relationship.EndNode.NodeID",
    "Relationship.EndNode.NodeIDType",
    "Relationship.RelationshipType",
    "Relationship.RelationshipStatus",
    "Registration.RegistrationStatus",
)
RR_PERIODS = 5

COMPANY = (Entity.Kind.COMPANY, Entity.Classification.COMPANY)
STATE_COMPANY = (Entity.Kind.COMPANY, Entity.Classification.STATE_COMPANY)
PUBLIC_BODY = (Entity.Kind.ORGANISATION, Entity.Classification.PUBLIC_BODY)
OTHER = (Entity.Kind.ORGANISATION, Entity.Classification.OTHER)
# Portuguese ELF codes (GLEIF entity-legal-forms, country PT, verified 2026-09-28).
LEGAL_FORMS: dict[str, tuple[str, str]] = {
    "USOG": COMPANY,  # Sociedade por Quotas
    "VF4C": COMPANY,  # Sociedade Unipessoal por Quotas
    "DFE5": COMPANY,  # Sociedade Anónima
    "MFHR": COMPANY,  # Sociedade Anónima Europeia
    "W9W3": COMPANY,  # Sociedade Anónima Desportiva
    "N66B": COMPANY,  # Sociedade em Nome Coletivo
    "OXUC": COMPANY,  # Sociedade em Comandita
    "A8CT": STATE_COMPANY,  # Entidade Pública Empresarial
    "D7OA": STATE_COMPANY,  # Entidade Empresarial Municipal
    "6IK8": STATE_COMPANY,  # Entidade Empresarial Intermunicipal
    "NIQY": STATE_COMPANY,  # Entidade Empresarial Metropolitana
    "PIDC": STATE_COMPANY,  # Empresa Municipal
    "IX01": STATE_COMPANY,  # Empresa Intermunicipal
    "XD16": STATE_COMPANY,  # Empresa Metropolitana
    "ZSWE": STATE_COMPANY,  # Empresa Regional
    "1HGD": (Entity.Kind.ORGANISATION, Entity.Classification.COOPERATIVE),
    "5KVH": (Entity.Kind.ORGANISATION, Entity.Classification.COOPERATIVE),
    "V6YL": (Entity.Kind.ORGANISATION, Entity.Classification.COOPERATIVE),
    "ALPT": (Entity.Kind.ORGANISATION, Entity.Classification.ASSOCIATION),
    "Z0NE": (Entity.Kind.ORGANISATION, Entity.Classification.FOUNDATION),
    "KUUV": PUBLIC_BODY,  # Organismo da Administração Pública
    "P5S3": PUBLIC_BODY,  # Pessoa Coletiva de Direito Público
    "QFXD": (Entity.Kind.ORGANISATION, Entity.Classification.INTERNATIONAL_ORGANISATION),
}


class GleifError(ValueError):
    """Safe, payload-free failure for an incomplete or unexpected GLEIF collection."""


@dataclass(frozen=True)
class LeiRecord:
    lei: str
    name: str
    # Legal-person NIPC; empty for parents keyed by LEI only.
    nipc: str
    kind: str
    classification: str


@dataclass(frozen=True)
class ParentLink:
    child: str
    parent: str
    level: str
    relationship_type: str
    start: date | None
    end: date | None
    end_dropped: bool


@dataclass(frozen=True)
class GleifSnapshot:
    as_of: date
    retrieved_at: datetime
    # Portuguese records with a legal-person NIPC, in LEI order.
    records: tuple[LeiRecord, ...]
    # Parents outside ``records``, keyed by LEI only.
    parents: tuple[LeiRecord, ...]
    links: tuple[ParentLink, ...]
    skipped: int
    unresolved_links: int
    # False for ``--limit`` runs: absent children are not ceased.
    complete: bool


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise GleifError("Estrutura GLEIF inesperada.")
    return value


def _optional_object(value: JSONValue) -> JSONObject:
    return {} if value is None else _object(value)


def _text(value: JSONValue) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GleifError("Texto GLEIF inválido.")
    return value.strip()


def _lei(value: JSONValue) -> str:
    text = _text(value)
    if not LEI_PATTERN.fullmatch(text):
        raise GleifError("LEI inválido na fonte GLEIF.")
    return text


def _name(value: JSONValue) -> str:
    name = " ".join(_text(value).split())
    if not name:
        raise GleifError("Registo GLEIF sem denominação legal.")
    return name if len(name) <= NAME_LENGTH else name[: NAME_LENGTH - 3].rstrip() + "..."


def _nipc(value: JSONValue) -> str:
    """The legal-person NIPC or ""; any other number is discarded here, never kept."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().upper().removeprefix(COUNTRY)
    return candidate if valid_nipc(candidate) else ""


def classify(legal_form: str, category: str) -> tuple[str, str]:
    """(kind, classification) for a new organisation, from its ELF code and category."""
    known = LEGAL_FORMS.get(legal_form)
    if category == "RESIDENT_GOVERNMENT_ENTITY":
        company = known is not None and known[0] == Entity.Kind.COMPANY
        return STATE_COMPANY if company else PUBLIC_BODY
    return known if known is not None else OTHER


def _entity_fields(item: JSONValue) -> tuple[str, JSONObject] | None:
    """(LEI, entity) of a usable record; None for natural persons and void registrations."""
    attributes = _object(_object(item).get("attributes"))
    lei = _lei(attributes.get("lei"))
    entity = _object(attributes.get("entity"))
    registration = _optional_object(attributes.get("registration"))
    if _text(registration.get("status")) in DROPPED_REGISTRATIONS:
        return None
    legal_form = _text(_optional_object(entity.get("legalForm")).get("id"))
    if _text(entity.get("category")) in PERSON_CATEGORIES or legal_form in PERSON_LEGAL_FORMS:
        return None
    return lei, entity


def _registry(entity: JSONObject) -> str:
    return _text(_optional_object(entity.get("registeredAt")).get("id"))


def _country(entity: JSONObject) -> str:
    return _text(_optional_object(entity.get("legalAddress")).get("country"))


def _classified(lei: str, entity: JSONObject, *, nipc: str) -> LeiRecord:
    kind, classification = classify(
        _text(_optional_object(entity.get("legalForm")).get("id")), _text(entity.get("category"))
    )
    return LeiRecord(
        lei=lei,
        name=_name(_optional_object(entity.get("legalName")).get("name")),
        nipc=nipc,
        kind=kind,
        classification=classification,
    )


def parse_record(item: JSONValue) -> tuple[str, LeiRecord | None]:
    """A Portuguese Registo Comercial record; None unless it carries a legal-person NIPC."""
    fields = _entity_fields(item)
    if fields is None:
        return _lei(_object(_object(item).get("attributes")).get("lei")), None
    lei, entity = fields
    nipc = _nipc(entity.get("registeredAs"))
    if _registry(entity) != REGISTRY or _country(entity) != COUNTRY or not nipc:
        return lei, None
    return lei, _classified(lei, entity, nipc=nipc)


def parse_parent(item: JSONValue) -> tuple[str, LeiRecord | None]:
    """A parent outside the Portuguese set, keyed by LEI only; foreign parents are companies."""
    fields = _entity_fields(item)
    if fields is None:
        return _lei(_object(_object(item).get("attributes")).get("lei")), None
    lei, entity = fields
    # A Registo Comercial number that is not a legal-person NIPC may identify a person.
    if _registry(entity) == REGISTRY and not _nipc(entity.get("registeredAs")):
        return lei, None
    if _country(entity) == COUNTRY:
        return lei, _classified(lei, entity, nipc="")
    name = _name(_optional_object(entity.get("legalName")).get("name"))
    return lei, LeiRecord(lei=lei, name=name, nipc="", kind=COMPANY[0], classification=COMPANY[1])


def _day(value: str) -> date | None:
    if not value:
        return None
    match = DAY.match(value)
    if match is None:
        raise GleifError("Data GLEIF inválida.")
    try:
        return date.fromisoformat(match[1])
    except ValueError as exc:
        raise GleifError("Data GLEIF inválida.") from exc


def _rr_columns(header: list[str]) -> tuple[list[int], list[tuple[int, int, int]]]:
    column = {name: index for index, name in enumerate(header)}
    periods = [
        (
            f"Relationship.Period.{number}.startDate",
            f"Relationship.Period.{number}.endDate",
            f"Relationship.Period.{number}.periodType",
        )
        for number in range(1, RR_PERIODS + 1)
    ]
    required = [*RR_COLUMNS, *(name for period in periods for name in period)]
    if any(name not in column for name in required):
        raise GleifError("Colunas de relações GLEIF inesperadas.")
    return [column[name] for name in RR_COLUMNS], [
        (column[start], column[end], column[kind]) for start, end, kind in periods
    ]


def _period(row: list[str], periods: list[tuple[int, int, int]]) -> tuple[date | None, date | None]:
    """The relationship period (not accounting or filing periods), if published."""
    for start, end, kind in periods:
        if row[kind] == "RELATIONSHIP_PERIOD":
            return _day(row[start]), _day(row[end])
    return None, None


def parse_relationships(archive: bytes, children: frozenset[str]) -> list[ParentLink]:
    """Consolidation parents of ``children`` from the zipped RR CSV golden copy, streamed."""
    links: dict[tuple[str, str, str], ParentLink] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = bundle.infolist()
            if (
                len(members) != 1
                or not members[0].filename.endswith(".csv")
                or members[0].file_size > MAX_RR_CSV_BYTES
            ):
                raise GleifError("Arquivo de relações GLEIF inesperado.")
            with bundle.open(members[0]) as raw:
                reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                header = next(reader, None)
                if header is None:
                    raise GleifError("Arquivo de relações GLEIF vazio.")
                indexes, periods = _rr_columns(header)
                start, start_type, end, end_type, kind, status, registration = indexes
                for row in reader:
                    if len(row) != len(header):
                        raise GleifError("Linha de relações GLEIF inválida.")
                    child = row[start]
                    level = CONSOLIDATION.get(row[kind])
                    if (
                        child not in children
                        or level is None
                        or row[start_type] != "LEI"
                        or row[end_type] != "LEI"
                        or row[status] != "ACTIVE"
                        or row[registration] not in RR_REGISTRATIONS
                    ):
                        continue
                    parent = row[end]
                    if not LEI_PATTERN.fullmatch(parent) or parent == child:
                        continue
                    first, last = _period(row, periods)
                    dropped = first is not None and last is not None and last < first
                    links.setdefault(
                        (child, level, parent),
                        ParentLink(
                            child=child,
                            parent=parent,
                            level=level,
                            relationship_type=row[kind],
                            start=first,
                            end=None if dropped else last,
                            end_dropped=dropped,
                        ),
                    )
    except (zipfile.BadZipFile, UnicodeDecodeError, csv.Error, OSError, EOFError) as exc:
        raise GleifError("Arquivo de relações GLEIF ilegível.") from exc
    return sorted(links.values(), key=lambda link: (link.child, link.level, link.parent))


def _api_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == API_HOST
        and parts.path == API_PATH
        and not parts.fragment
    )


def _golden_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == GOLDEN_HOST
        and not parts.query
        and not parts.fragment
        and (
            parts.path == GOLDEN_PATH
            or (parts.path.startswith(GOLDEN_FILES) and parts.path.endswith(RR_SUFFIX))
        )
    )


class _Session:
    """One bounded run: a shared deadline and API pacing."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.requests = 0

    def raw(
        self, url: str, *, allowed: Callable[[str], bool], max_bytes: int, accept: str
    ) -> bytes:
        if self.requests and REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL)
        self.requests += 1
        try:
            return download(
                url,
                allowed=allowed,
                max_bytes=max_bytes,
                deadline=self.deadline,
                headers={"Accept": accept},
            )
        except OfficialHTTPError as exc:
            raise GleifError("Não foi possível consultar a GLEIF em segurança.") from exc

    def json(
        self,
        url: str,
        *,
        allowed: Callable[[str], bool],
        max_bytes: int,
        accept: str = JSON_API,
    ) -> JSONObject:
        content = self.raw(url, allowed=allowed, max_bytes=max_bytes, accept=accept)
        try:
            return _object(cast(JSONValue, json.loads(content)))
        except (ValueError, RecursionError) as exc:
            raise GleifError("JSON GLEIF inválido.") from exc


def records_url(page_size: int = PAGE_SIZE) -> str:
    query = urlencode(
        {
            "filter[entity.legalAddress.country]": COUNTRY,
            "filter[entity.registeredAt]": REGISTRY,
            "page[size]": str(page_size),
            "page[cursor]": "*",
        }
    )
    return f"{API_URL}?{query}"


def _data(page: JSONObject) -> list[JSONValue]:
    data = page.get("data")
    if not isinstance(data, list):
        raise GleifError("Página GLEIF sem registos.")
    return data


def _fetch_records(session: _Session, limit: int | None) -> tuple[list[LeiRecord], set[str], int]:
    """Cursor crawl; returns kept records, excluded LEIs (never parents) and skip count."""
    url = records_url(min(PAGE_SIZE, limit) if limit else PAGE_SIZE)
    records: dict[str, LeiRecord] = {}
    excluded: set[str] = set()
    seen = 0
    total: int | None = None
    for _ in range(MAX_PAGES):
        page = session.json(url, allowed=_api_allowed, max_bytes=MAX_PAGE_BYTES)
        pagination = _optional_object(_optional_object(page.get("meta")).get("pagination"))
        count = pagination.get("total")
        if type(count) is not int or count < 0 or count > MAX_RECORDS:
            raise GleifError("Contagem de registos GLEIF inválida ou excessiva.")
        total = count
        for item in _data(page):
            if limit is not None and seen >= limit:
                break
            lei, record = parse_record(item)
            if lei in records or lei in excluded:
                raise GleifError("Registo GLEIF repetido na paginação.")
            seen += 1
            if record is None:
                excluded.add(lei)
            else:
                records[lei] = record
        following = _text(_optional_object(page.get("links")).get("next"))
        if not following or (limit is not None and seen >= limit):
            break
        url = following
    else:
        raise GleifError("Paginação GLEIF excessiva; não foi aplicada.")
    if limit is None and seen != total:
        raise GleifError("Recolha GLEIF incompleta; não foi aplicada.")
    return [records[lei] for lei in sorted(records)], excluded, len(excluded)


def _rr_archive(session: _Session) -> bytes:
    metadata = session.json(GOLDEN_URL, allowed=_golden_allowed, max_bytes=MAX_METADATA_BYTES)
    rr = _object(_object(metadata.get("data")).get("rr"))
    entry = _object(_object(rr.get("full_file")).get("csv"))
    url, size = _text(entry.get("url")), entry.get("size")
    if not _golden_allowed(url) or type(size) is not int or not 0 < size <= MAX_RR_BYTES:
        raise GleifError("Metadados da cópia dourada GLEIF inesperados.")
    return session.raw(
        url, allowed=_golden_allowed, max_bytes=MAX_RR_BYTES, accept="application/zip"
    )


def _fetch_parents(session: _Session, leis: list[str]) -> dict[str, LeiRecord]:
    parents: dict[str, LeiRecord] = {}
    for batch in batched(leis, PARENT_BATCH, strict=False):
        wanted = set(batch)
        query = urlencode({"filter[lei]": ",".join(batch), "page[size]": str(PARENT_BATCH)})
        page = session.json(f"{API_URL}?{query}", allowed=_api_allowed, max_bytes=MAX_PAGE_BYTES)
        for item in _data(page):
            lei, record = parse_parent(item)
            if lei in wanted and record is not None:
                parents[lei] = record
    return parents


def fetch_snapshot(*, as_of: date, limit: int | None = None) -> GleifSnapshot:
    """Portuguese LEI records and their accounting-consolidation parents."""
    if limit is not None and limit < 1:
        raise GleifError("O limite tem de ser positivo.")
    session = _Session()
    retrieved_at = timezone.now()
    records, excluded, skipped = _fetch_records(session, limit)
    known = frozenset(record.lei for record in records)
    found = parse_relationships(_rr_archive(session), known)
    outside = sorted({link.parent for link in found} - known - excluded)
    parents = _fetch_parents(session, outside)
    links = tuple(link for link in found if link.parent in known or link.parent in parents)
    return GleifSnapshot(
        as_of=as_of,
        retrieved_at=retrieved_at,
        records=tuple(records),
        parents=tuple(parents[lei] for lei in sorted(parents)),
        links=links,
        skipped=skipped,
        unresolved_links=len(found) - len(links),
        complete=limit is None,
    )


def _identities(scheme: str, external_ids: list[str]) -> dict[str, SourceIdentity]:
    found: dict[str, SourceIdentity] = {}
    for chunk in batched(external_ids, 5000, strict=False):
        for identity in SourceIdentity.objects.select_related("entity", "reviewed_by").filter(
            source=scheme, external_id__in=chunk
        ):
            found[identity.external_id] = identity
    return found


def _attach(identities: list[SourceIdentity]) -> None:
    """Add identities to already-resolved organisations (one organisation, several ids)."""
    for identity in identities:
        # FK and uniqueness checks would query per row; the database enforces them.
        identity.full_clean(exclude=["entity"], validate_unique=False, validate_constraints=False)
    SourceIdentity.objects.bulk_create(identities, batch_size=1000)


def _resolve_records(records: tuple[LeiRecord, ...]) -> tuple[dict[str, Entity], Counter[str]]:
    """``lei`` identity, else the ``nipc`` identity, else a new organisation with both."""
    counts: Counter[str] = Counter()
    by_lei = {
        lei: identity.entity for lei, identity in _identities(LEI, [r.lei for r in records]).items()
    }
    by_nipc = {
        nipc: identity.entity
        for nipc, identity in _identities(NIPC, sorted({r.nipc for r in records})).items()
    }
    added: list[SourceIdentity] = []
    # First complete organisations already known by LEI, so a free NIPC joins them.
    for record in records:
        entity = by_lei.get(record.lei)
        if entity is None:
            continue
        holder = by_nipc.get(record.nipc)
        if holder is None:
            by_nipc[record.nipc] = entity
            added.append(SourceIdentity(source=NIPC, external_id=record.nipc, entity=entity))
            counts["nipc_added"] += 1
        elif holder.pk != entity.pk:
            # Two entities hold the two identifiers; editors must merge, never the import.
            counts["conflicts"] += 1
    create: dict[str, tuple[str, str, str]] = {}
    waiting: list[LeiRecord] = []
    for record in records:
        if record.lei in by_lei:
            continue
        holder = by_nipc.get(record.nipc)
        if holder is not None:
            by_lei[record.lei] = holder
            added.append(SourceIdentity(source=LEI, external_id=record.lei, entity=holder))
            counts["lei_added"] += 1
        else:
            create.setdefault(record.nipc, (record.name, record.kind, record.classification))
            waiting.append(record)
    created = official_entities_bulk(NIPC, create)
    counts["organisations_created"] = len(create)
    for record in waiting:
        entity = created[record.nipc]
        by_lei[record.lei] = entity
        added.append(SourceIdentity(source=LEI, external_id=record.lei, entity=entity))
    _attach(added)
    return by_lei, counts


def _temporal_status(link: ParentLink, as_of: date) -> str:
    if link.end_dropped or (link.start is not None and link.start > as_of):
        return TemporalStatus.UNKNOWN
    if link.end is not None and link.end < as_of:
        return TemporalStatus.ENDED
    return TemporalStatus.CURRENT


def _observation(
    link: ParentLink,
    *,
    identity: SourceIdentity,
    parent: Entity,
    names: dict[str, str],
    snapshot: GleifSnapshot,
) -> ObservationInput:
    status = _temporal_status(link, snapshot.as_of)
    passage = (
        f"{names[link.child]} (LEI {link.child}) é consolidada contabilisticamente "
        f"{PASSAGE_LEVEL[link.level]} por {names[link.parent]} (LEI {link.parent}), segundo o "
        f"registo de relações da GLEIF ({link.relationship_type})."
    )
    if link.start is not None:
        passage += f" Início do período da relação: {link.start.isoformat()}."
    if link.end is not None:
        passage += f" Fim do período da relação: {link.end.isoformat()}."
    if link.end_dropped:
        passage += " A data de fim publicada antecede o início e não foi usada."
    reference = f"GLEIF LEI-RR; {link.child} {link.relationship_type} {link.parent}"
    projection: JSONObject = {
        "passage": passage,
        "reference": reference,
        "identity": identity.pk,
        "parent": str(parent.pk),
        "start": link.start.isoformat() if link.start else None,
        "end": link.end.isoformat() if link.end else None,
        "temporal_status": status,
    }
    return ObservationInput(
        external_id=f"{link.level}:{link.parent}",
        revision=hashlib.sha256(canonical_json(projection).encode()).hexdigest(),
        category=SourceObservation.Category.ORGANISATION_STRUCTURE,
        passage=passage,
        source_url=RECORD_URL.format(lei=link.child),
        publisher=DATASET.publisher,
        reference=reference,
        title=DATASET.title,
        identity=identity,
        effective_start=link.start,
        effective_end=link.end,
        object=parent,
        object_name=names[link.parent][:OBJECT_NAME_LENGTH],
        object_identifier=f"lei:{link.parent}",
        kind=Relationship.Kind.PART_OF,
        dataset=DATASET.key,
        role=ROLES[link.level],
        temporal_status=status,
        retrieved_at=snapshot.retrieved_at,
    )


def apply_snapshot(snapshot: GleifSnapshot) -> dict[str, int]:
    """Atomic: LEI/NIPC crosswalk, parents keyed by LEI and one scope per child LEI."""
    with import_transaction():
        organisations, counts = _resolve_records(snapshot.records)
        parent_rows = {p.lei: (p.name, p.kind, p.classification) for p in snapshot.parents}
        known_parents = _identities(LEI, list(parent_rows))
        entities = {**organisations, **official_entities_bulk(LEI, parent_rows)}
        counts["parents_created"] = len(parent_rows) - len(known_parents)
        names = {record.lei: record.name for record in (*snapshot.records, *snapshot.parents)}
        subjects = _identities(LEI, sorted({link.child for link in snapshot.links}))
        scopes: dict[str, list[ObservationInput]] = defaultdict(list)
        for link in snapshot.links:
            identity = subjects[link.child]
            parent = entities[link.parent]
            if parent.pk == identity.entity_id:
                # Both LEIs identify the same organisation (one shared NIPC).
                counts["same_organisation"] += 1
                continue
            scopes[f"{SCOPE_PREFIX}{link.child}"].append(
                _observation(link, identity=identity, parent=parent, names=names, snapshot=snapshot)
            )
        result: Counter[str] = Counter()
        if snapshot.complete:
            result.update(
                sync_scoped_snapshot(
                    source=GLEIF,
                    prefix=SCOPE_PREFIX,
                    snapshots={scope: tuple(items) for scope, items in scopes.items()},
                    as_of=snapshot.as_of,
                )
            )
        else:
            # A partial run replaces only the children it read.
            for record in snapshot.records:
                scope = f"{SCOPE_PREFIX}{record.lei}"
                result.update(
                    sync_observations(
                        source=GLEIF,
                        scope=scope,
                        observations=tuple(scopes.get(scope, ())),
                        as_of=snapshot.as_of,
                    )
                )
        result.update(counts)
        return dict(result)
