"""SIOE+ (DGAEP) public-sector organisations, supervision/succession structure and boards.

Public-token flow (verified 2026-09-28): the SPA's runtime config, ``POST /Auth/Login``,
``POST /Entity/search`` over universe 1 (Contas Nacionais) entities including extinct ones,
one search per entity type (classification), then ``POST /Entity/history`` per entity.
Organisations are anchored by their SIOE code; a legal-person NIPC links to an existing
``nipc`` organisation only when exactly one SIOE entity owns it. Ministries are the
per-Government ``governmentBodies`` codes (``XXV_MF``); legacy numeric codes name no
Government and are skipped. Board members are name-only persons: private candidates.
Gender, CV documents, submitters, contacts and addresses are dropped before caching.
Transient failures (network errors, 429, 5xx) are retried with bounded backoff honouring
Retry-After; with ``--cache-dir`` every completed response is kept, so a rerun resumes.
"""

import http.client
import json
import os
import re
import tempfile
import time
from base64 import b64encode
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from itertools import batched
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_observations, sync_scoped_snapshot
from .ept_offices import role_class
from .government import revised
from .identity import official_entities_bulk, valid_nipc
from .models import (
    EnrichmentSource,
    Entity,
    IdentityScheme,
    Relationship,
    SourceIdentity,
    SourceObservation,
    TemporalStatus,
    Term,
    import_transaction,
)
from .official_http import USER_AGENT, OfficialHTTPError, open_connection
from .parliament_parse import JSONObject, JSONValue, canonical_json

SITE = "https://www.sioe.dgaep.gov.pt"
CONFIG_URL = f"{SITE}/config/production.json"
API_ROOT = "https://sioeapi.dgaep.gov.pt"
LOGIN_URL = f"{API_ROOT}/Auth/Login"
SEARCH_URL = f"{API_ROOT}/Entity/search"
HISTORY_URL = f"{API_ROOT}/Entity/history"
ROUTES = frozenset({CONFIG_URL, LOGIN_URL, SEARCH_URL, HISTORY_URL})
# The SPA's public application id; the runtime config may confirm or replace it.
APPLICATION_ID = "B6355351-662E-4E6B-8DFC-BE84103C5F62"
UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
UNIVERSE = 1  # Contas Nacionais (a superset of "AP Jurídica")
ENTITY_CLASS = 1  # Entidade; Unidades Locais (sites) are not organisations
PAGE_SIZE = 5000
MAX_PAGES = 20
MAX_ROWS = 5000
CONFIG_BYTES = 256 * 1024
LOGIN_BYTES = 64 * 1024
SEARCH_BYTES = 16 * 1024 * 1024
HISTORY_BYTES = 4 * 1024 * 1024
CONFIG_TIMEOUT = 30
SEARCH_TIMEOUT = 180
HISTORY_TIMEOUT = 60
# Seconds between requests: sequential and polite over a ~3 h history crawl.
PAUSE = 0.5
# Bounded retries for transient failures (network errors, 429 and 5xx), doubling the wait.
MAX_ATTEMPTS = 5
BACKOFF = 5.0
MAX_BACKOFF = 120.0
# A longer Retry-After means the source asks us to stop, not to wait inside this run.
MAX_RETRY_AFTER = 300.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BATCH = 5000

DATASET = DATASETS["sioe"]
SIOE = IdentityScheme.SIOE
NIPC = IdentityScheme.NIPC
ORGANISATION_STRUCTURE = SourceObservation.Category.ORGANISATION_STRUCTURE
OFFICE_HOLDING = SourceObservation.Category.OFFICE_HOLDING
STRUCTURE_PREFIX = "sioe-structure:"
BOARDS_PREFIX = "sioe-boards:"
TUTELA = "Tutela"

SIOE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?")
# Unknown dates recorded as sentinels on legacy entities; never facts.
PLACEHOLDER_DATES = frozenset({date(1, 1, 1), date(1800, 1, 1), date(1900, 1, 1)})
GOVERNMENT_CODE = re.compile(r"(?P<roman>[IVXLC]+)_(?P<body>\S.{0,60})")
ROMAN = re.compile(r"C{0,3}(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})")
ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
# Regional Government bodies (RAA/RAM) share the "<Roman>_<abbr>" code pattern.
REGIONAL = re.compile(r"\bRegiona(?:l|is)\b|\((?:RAM|RAA)\)")
SUCCESSIONS = frozenset({"Extinção", "Fusão"})
HIGHER_EDUCATION_CAE = "8542"
ACT_HOSTS = frozenset(
    {
        "diariodarepublica.pt",
        "www.diariodarepublica.pt",
        "dre.pt",
        "www.dre.pt",
        "files.dre.pt",
        "data.dre.pt",
    }
)
NINE_DIGITS = re.compile(r"(?<!\d)\d{9}(?!\d)")

_KIND = Entity.Kind
_CLASS = Entity.Classification
PUBLIC_BODY = (_KIND.ORGANISATION, _CLASS.PUBLIC_BODY)
GOVERNMENT_OFFICE = (_KIND.ORGANISATION, _CLASS.GOVERNMENT_OFFICE)
REGULATOR = (_KIND.ORGANISATION, _CLASS.REGULATOR)
STATE_COMPANY = (_KIND.COMPANY, _CLASS.STATE_COMPANY)
MUNICIPALITY = (_KIND.ORGANISATION, _CLASS.MUNICIPALITY)
PARISH = (_KIND.ORGANISATION, _CLASS.PARISH)
ASSOCIATION = (_KIND.ORGANISATION, _CLASS.ASSOCIATION)
FOUNDATION = (_KIND.ORGANISATION, _CLASS.FOUNDATION)
COOPERATIVE = (_KIND.ORGANISATION, _CLASS.COOPERATIVE)
HIGHER_EDUCATION = (_KIND.UNIVERSITY, _CLASS.HIGHER_EDUCATION)
# SIOE+ "Tipo de Entidade" vocabulary (search filter ``types``). The universe is the
# national-accounts public sector, so every company type is a state company.
ENTITY_TYPES: dict[int, tuple[str, str]] = {
    1: PUBLIC_BODY,  # Direção-geral
    2: PUBLIC_BODY,  # Secretaria-geral
    3: PUBLIC_BODY,  # Inspeção-geral
    4: PUBLIC_BODY,  # Direção Regional
    5: PUBLIC_BODY,  # Estrutura temporária - estrutura de missão
    6: PUBLIC_BODY,  # Órgão consultivo
    7: GOVERNMENT_OFFICE,  # Gabinete Ministro
    8: PUBLIC_BODY,  # Governo Civil
    9: PUBLIC_BODY,  # Força de Segurança
    10: PUBLIC_BODY,  # Instituto Público
    11: STATE_COMPANY,  # Entidade Pública Empresarial
    12: PUBLIC_BODY,  # Órgão Independente
    13: PUBLIC_BODY,  # Junta de Turismo
    14: REGULATOR,  # Entidade Administrativa Independente
    15: PUBLIC_BODY,  # Serviço de Apoio
    16: ASSOCIATION,  # Comunidade Urbana
    17: ASSOCIATION,  # Área Metropolitana
    18: ASSOCIATION,  # Associação de Municípios de fins específicos
    19: ASSOCIATION,  # Comunidade Intermunicipal de fins gerais
    20: PARISH,  # Assembleia de Freguesia
    21: PARISH,  # Junta de Freguesia
    22: MUNICIPALITY,  # Assembleia Municipal
    23: MUNICIPALITY,  # Município
    24: STATE_COMPANY,  # Entidade Empresarial Local
    25: PUBLIC_BODY,  # Serviços Municipalizados
    26: GOVERNMENT_OFFICE,  # Gabinete
    27: PUBLIC_BODY,  # Estabelecimento de educação e ensino básico e secundário
    28: PUBLIC_BODY,  # Forças Armadas
    29: GOVERNMENT_OFFICE,  # Gabinete 1.º Ministro
    30: GOVERNMENT_OFFICE,  # Gabinete Secretário de Estado
    31: GOVERNMENT_OFFICE,  # Gabinete Subsecretário de Estado
    32: PUBLIC_BODY,  # Estrutura atípica
    33: STATE_COMPANY,  # Entidade Empresarial Regional
    34: GOVERNMENT_OFFICE,  # Gabinete Secretário Regional
    35: GOVERNMENT_OFFICE,  # Gabinete Presidente Regional
    36: GOVERNMENT_OFFICE,  # Gabinete Vice-Presidente Regional
    37: PUBLIC_BODY,  # Inspeção Regional
    38: PUBLIC_BODY,  # Gabinete do Representante da República (not a Government cabinet)
    39: ASSOCIATION,  # Comunidade intermunicipal
    40: HIGHER_EDUCATION,  # Unidade Orgânica de Ensino e Investigação
    41: STATE_COMPANY,  # Entidade Empresarial Municipal
    42: STATE_COMPANY,  # Empresa Municipal
    43: STATE_COMPANY,  # Empresa Intermunicipal
    44: STATE_COMPANY,  # Empresa Metropolitana
    45: STATE_COMPANY,  # Sociedade por Quotas
    46: STATE_COMPANY,  # Sociedade Anónima
    47: PUBLIC_BODY,  # Serviço Municipalizado e Intermunicipalizado
    48: PUBLIC_BODY,  # Provedor de Justiça
    49: PUBLIC_BODY,  # Assembleia Distrital
    50: STATE_COMPANY,  # Empresas públicas
    51: STATE_COMPANY,  # Empresa Participada
    52: PUBLIC_BODY,  # Centro de Formação Profissional
    53: COOPERATIVE,  # Cooperativa
    54: ASSOCIATION,  # Associação
    55: FOUNDATION,  # Fundação
    56: PUBLIC_BODY,  # Fundo Autónomo
    57: PUBLIC_BODY,  # Fundo da Segurança Social
    58: PUBLIC_BODY,  # Autoridade Metropolitana
    59: ASSOCIATION,  # Grande Área Metropolitana
    60: ASSOCIATION,  # Associação de Municípios de fins múltiplos
    61: ASSOCIATION,  # Federação de Municípios
    62: PUBLIC_BODY,  # Entidade Regional de Turismo
    63: ASSOCIATION,  # Associação de Freguesias
    64: STATE_COMPANY,  # Entidade Pública Empresarial Regional
    65: PUBLIC_BODY,  # Entidade Pública Regional
    66: STATE_COMPANY,  # Agrupamento Complementar de Empresas
    67: PUBLIC_BODY,  # Tribunal
    68: PUBLIC_BODY,  # Agrupamento de Centros de Saúde
    69: REGULATOR,  # Banco Central
    70: PUBLIC_BODY,  # Estrutura temporária - comissão
    71: PUBLIC_BODY,  # Estrutura temporária - grupo de trabalho
    72: PUBLIC_BODY,  # Estrutura temporária - grupo de projeto
    73: PUBLIC_BODY,  # Unidade Orgânica de Investigação
}

# Minimised projections (also what the optional cache keeps): list keys → row keys.
SEARCH_KEYS = frozenset(
    {"sioeCode", "name", "nipc", "classId", "active", "state", "endDate", "ghost"}
)
HISTORY_LISTS: dict[str, frozenset[str]] = {
    "governmentBodies": frozenset(
        {"governmentBodyName", "governmentBodyCode", "main", "startDate", "endDate", "id"}
    ),
    "scopes": frozenset({"entityType", "startDate", "endDate"}),
    "caes": frozenset({"caeName", "startDate", "endDate"}),
    "aggregatorEntities": frozenset({"sioeCode", "name", "isChild", "startDate", "endDate", "id"}),
    "relationships": frozenset({"sioeCode", "name", "typeName", "startDate", "endDate", "id"}),
    "managementBoardMembers": frozenset(
        {"name", "positionName", "cvurl", "startDate", "expiryDate", "endDate", "id"}
    ),
}
HISTORY_KEYS = frozenset({"sioeCode", "classID", *HISTORY_LISTS})


class SioeError(ValueError):
    """Safe, payload-free failure for an incomplete or unexpected SIOE+ response."""


@dataclass(frozen=True)
class Organisation:
    code: str
    name: str
    nipc: str
    active: bool
    extinct_on: date | None


@dataclass(frozen=True)
class Tutela:
    row_id: int
    code: str
    label: str
    main: bool
    start: date | None
    end: date | None


@dataclass(frozen=True)
class Link:
    """An aggregating parent or a predecessor, named by its SIOE code."""

    row_id: int
    code: str
    name: str
    relation: str
    start: date | None
    end: date | None


@dataclass(frozen=True)
class Member:
    row_id: int
    name: str
    role: str
    start: date | None
    end: date | None
    expiry: date | None
    reversed_end: date | None
    act_url: str


@dataclass(frozen=True)
class History:
    code: str
    type_id: int | None
    higher_education: bool
    tutelas: tuple[Tutela, ...]
    parents: tuple[Link, ...]
    predecessors: tuple[Link, ...]
    members: tuple[Member, ...]
    legacy_tutelas: int
    dropped_members: int


@dataclass(frozen=True)
class Ministry:
    code: str
    label: str
    government: int
    national: bool

    @property
    def key(self) -> str:
        return f"gov:{self.code}"

    @property
    def name(self) -> str:
        numeral = self.code.split("_", 1)[0]
        cabinet = "Governo" if self.national else "Governo Regional"
        return f"{self.label} ({numeral} {cabinet})"


@dataclass(frozen=True)
class SioeSnapshot:
    as_of: date
    retrieved_at: datetime
    organisations: tuple[Organisation, ...]
    types: dict[str, int]
    histories: dict[str, History]
    ghosts: int

    @property
    def complete(self) -> bool:
        """Only a history for every organisation may cease absent scopes."""
        return len(self.histories) == len(self.organisations)


# JSON projection -----------------------------------------------------------------


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise SioeError("Estrutura SIOE+ inesperada.")
    return value


def _rows(value: JSONValue) -> list[JSONObject]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise SioeError("Lista SIOE+ ausente ou demasiado extensa.")
    return [_object(item) for item in value]


def _text(value: JSONValue, *, limit: int = 240, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise SioeError("Texto SIOE+ inválido.")
    text = " ".join(value.split())
    if (required and not text) or len(text) > limit:
        raise SioeError("Texto SIOE+ vazio ou excessivo.")
    return text


def _code(value: JSONValue) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{9}", value):
        raise SioeError("Código SIOE+ inválido.")
    return value


def _integer(value: JSONValue, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SioeError("Número SIOE+ inválido.")
    return value


def _flag(value: JSONValue) -> bool:
    if type(value) is not bool:
        raise SioeError("Indicador SIOE+ inválido.")
    return value


def _date(value: JSONValue) -> date | None:
    """Date part only (times carry DST artefacts); sentinel dates are unknown."""
    if value is None or value == "":
        return None
    match = SIOE_DATE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise SioeError("Data SIOE+ inválida.")
    try:
        day = date.fromisoformat(match[1])
    except ValueError as exc:
        raise SioeError("Data SIOE+ inválida.") from exc
    return None if day in PLACEHOLDER_DATES else day


def _project(row: JSONObject, keys: frozenset[str]) -> JSONObject:
    return {key: value for key, value in row.items() if key in keys}


def minimise_search(payload: JSONValue) -> JSONObject:
    page = _object(payload)
    result = _project(page, frozenset({"pageNumber", "totalPages", "totalCount"}))
    result["items"] = [_project(item, SEARCH_KEYS) for item in _rows(page.get("items"))]
    return result


def minimise_history(payload: JSONValue) -> JSONObject:
    """Only the retained fields; gender, CVs, contacts and submitters never pass."""
    record = _project(_object(payload), HISTORY_KEYS)
    for name, keys in HISTORY_LISTS.items():
        record[name] = [_project(row, keys) for row in _rows(record.get(name))]
    return record


# Parsing ---------------------------------------------------------------------------


def classify(type_id: int | None, *, higher_education: bool = False) -> tuple[str, str]:
    """(kind, classification) for a new SIOE organisation; set only at creation."""
    kind, classification = (
        ENTITY_TYPES.get(type_id, PUBLIC_BODY) if type_id is not None else PUBLIC_BODY
    )
    # Universities and polytechnics have no own type: their CAE is "85420 - Ensino superior".
    if higher_education and kind == _KIND.ORGANISATION:
        return HIGHER_EDUCATION
    return kind, classification


def roman_number(numeral: str) -> int | None:
    if not numeral or not ROMAN.fullmatch(numeral):
        return None
    total = 0
    for current, following in zip(numeral, [*numeral[1:], ""], strict=True):
        value = ROMAN_VALUES[current]
        total += -value if following and ROMAN_VALUES[following] > value else value
    return total


def search_page(payload: JSONValue, *, number: int) -> tuple[int, int, list[JSONObject]]:
    """(total count, total pages, items) of one consistent page."""
    page = _object(payload)
    total = _integer(page.get("totalCount"))
    pages = _integer(page.get("totalPages"))
    expected_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > MAX_PAGES or pages not in {expected_pages, max(1, expected_pages)}:
        raise SioeError("Paginação SIOE+ inconsistente ou excessiva.")
    if _integer(page.get("pageNumber"), minimum=1) != number:
        raise SioeError("A pesquisa SIOE+ não devolveu a página pedida.")
    items = _rows(page.get("items"))
    if len(items) != min(PAGE_SIZE, max(0, total - (number - 1) * PAGE_SIZE)):
        raise SioeError("Página SIOE+ truncada.")
    return total, pages, items


def _organisation(item: JSONObject) -> Organisation:
    if _integer(item.get("classId")) != ENTITY_CLASS:
        raise SioeError("A pesquisa SIOE+ devolveu uma unidade local.")
    nipc = item.get("nipc")
    extinct = _text(item.get("state"), required=False) == "Extinto"
    return Organisation(
        code=_code(item.get("sioeCode")),
        name=_text(item.get("name")),
        nipc=nipc if isinstance(nipc, str) and re.fullmatch(r"[0-9]{9}", nipc) else "",
        active=_flag(item.get("active")),
        extinct_on=_date(item.get("endDate")) if extinct else None,
    )


def parse_universe(pages: list[JSONValue]) -> tuple[tuple[Organisation, ...], int]:
    """Entities of the complete universe search, code-sorted; ghosts are dropped."""
    codes = parse_codes(pages)
    organisations: dict[str, Organisation] = {}
    ghosts = 0
    for number, payload in enumerate(pages, 1):
        for item in search_page(payload, number=number)[2]:
            if item.get("ghost") is True:
                ghosts += 1
                continue
            organisation = _organisation(item)
            organisations[organisation.code] = organisation
    if len(organisations) + ghosts != len(codes):
        raise SioeError("Pesquisa SIOE+ incompleta.")
    return tuple(organisations[code] for code in sorted(organisations)), ghosts


def parse_codes(pages: list[JSONValue]) -> set[str]:
    """Every code of a complete, paginated search; repeated codes mean the pages shifted."""
    if not pages:
        raise SioeError("Pesquisa SIOE+ incompleta.")
    expected: tuple[int, int] | None = None
    codes: set[str] = set()
    count = 0
    for number, payload in enumerate(pages, 1):
        total, page_count, items = search_page(payload, number=number)
        if expected is None:
            expected = (total, page_count)
        if expected != (total, page_count):
            raise SioeError("A pesquisa SIOE+ mudou durante a consulta; nada foi aplicado.")
        for item in items:
            codes.add(_code(item.get("sioeCode")))
            count += 1
    if expected is None or len(pages) != max(1, expected[1]) or count != expected[0]:
        raise SioeError("Pesquisa SIOE+ incompleta.")
    if len(codes) != count:
        raise SioeError("A pesquisa SIOE+ mudou durante a consulta; nada foi aplicado.")
    return codes


def _open(row: JSONObject) -> bool:
    return row.get("endDate") in (None, "")


def _latest(rows: Iterable[JSONObject]) -> list[JSONObject]:
    """Open rows first, then by start date (most recent first)."""
    return sorted(
        rows,
        key=lambda row: (_open(row), _date(row.get("startDate")) or date.min),
        reverse=True,
    )


def _interval(row: JSONObject) -> tuple[date | None, date | None]:
    start, end = _date(row.get("startDate")), _date(row.get("endDate"))
    if start is not None and end is not None and end < start:
        return start, None
    return start, end


def act_url(value: JSONValue) -> str:
    """The appointing act, only as an https Diário da República link."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if len(url) > 500 or any(char.isspace() or ord(char) < 33 for char in url):
        return ""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ACT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return ""
    return url


def _member(row: JSONObject) -> Member | None:
    name = _text(row.get("name"), limit=300, required=False)
    role = _text(row.get("positionName"), required=False)
    # A nine-digit run in free text could be a personal tax number: drop the row unread.
    if not name or NINE_DIGITS.search(name) or NINE_DIGITS.search(role):
        return None
    start, end = _date(row.get("startDate")), _date(row.get("endDate"))
    reversed_end = end if start is not None and end is not None and end < start else None
    return Member(
        row_id=_integer(row.get("id"), minimum=1),
        name=name,
        role=role,
        start=start,
        end=None if reversed_end else end,
        expiry=_date(row.get("expiryDate")),
        reversed_end=reversed_end,
        act_url=act_url(row.get("cvurl")),
    )


def _link_name(row: JSONObject) -> str:
    """The related entity as published, else its SIOE code."""
    return _text(row.get("name"), required=False) or f"entidade {_code(row.get('sioeCode'))}"


def parse_history(payload: JSONValue, *, code: str) -> History:
    record = minimise_history(payload)
    if record.get("sioeCode") != code or _integer(record.get("classID")) != ENTITY_CLASS:
        raise SioeError("O histórico SIOE+ não corresponde à entidade pedida.")
    scopes = [row for row in _rows(record["scopes"]) if type(row.get("entityType")) is int]
    current_type = cast(int, _latest(scopes)[0]["entityType"]) if scopes else None
    higher_education = any(
        _open(row) and _text(row.get("caeName"), required=False).startswith(HIGHER_EDUCATION_CAE)
        for row in _rows(record["caes"])
    )
    tutelas: list[Tutela] = []
    legacy = 0
    for row in _rows(record["governmentBodies"]):
        body_code = _text(row.get("governmentBodyCode"), limit=64)
        match = GOVERNMENT_CODE.fullmatch(body_code)
        if match is None or roman_number(match["roman"]) is None:
            # Legacy numeric codes (pre-2025 rows) name a ministry but no Government.
            legacy += 1
            continue
        tutelas.append(
            Tutela(
                _integer(row.get("id"), minimum=1),
                body_code,
                _text(row.get("governmentBodyName"), limit=200),
                row.get("main") is True,
                *_interval(row),
            )
        )
    parents = [
        Link(
            _integer(row.get("id"), minimum=1),
            _code(row.get("sioeCode")),
            _link_name(row),
            "Agregação",
            *_interval(row),
        )
        for row in _rows(record["aggregatorEntities"])
        if row.get("isChild") is False
    ]
    predecessors = [
        Link(
            _integer(row.get("id"), minimum=1),
            _code(row.get("sioeCode")),
            _link_name(row),
            relation,
            _date(row.get("startDate")),
            None,
        )
        for row in _rows(record["relationships"])
        if (relation := _text(row.get("typeName"), required=False)) in SUCCESSIONS
    ]
    members = [_member(row) for row in _rows(record["managementBoardMembers"])]
    kept = tuple(member for member in members if member is not None)
    return History(
        code=code,
        type_id=cast(int | None, current_type),
        higher_education=higher_education,
        tutelas=tuple(tutelas),
        parents=tuple(parents),
        predecessors=tuple(predecessors),
        members=kept,
        legacy_tutelas=legacy,
        dropped_members=len(members) - len(kept),
    )


def entity_type(snapshot: SioeSnapshot, code: str) -> int | None:
    """Type-search tag first, then the entity's current history scope."""
    tagged = snapshot.types.get(code)
    if tagged is not None:
        return tagged
    history = snapshot.histories.get(code)
    return history.type_id if history is not None else None


def ministries(snapshot: SioeSnapshot) -> dict[str, Ministry]:
    """One organisation per per-Government ministry code (most frequent SIOE label)."""
    labels: dict[str, Counter[str]] = defaultdict(Counter)
    for history in snapshot.histories.values():
        for tutela in history.tutelas:
            labels[tutela.code][tutela.label] += 1
    result: dict[str, Ministry] = {}
    for code, counter in labels.items():
        label = min(counter, key=lambda text: (-counter[text], text))
        number = roman_number(code.split("_", 1)[0])
        if number is None:
            continue
        result[code] = Ministry(
            code=code, label=label, government=number, national=not REGIONAL.search(label)
        )
    return result


def nipc_owners(organisations: Iterable[Organisation]) -> dict[str, str]:
    """NIPC → the single SIOE entity owning it (unique among active ones, else overall)."""
    holders: dict[str, list[Organisation]] = defaultdict(list)
    for organisation in organisations:
        if organisation.nipc and valid_nipc(organisation.nipc):
            holders[organisation.nipc].append(organisation)
    owners: dict[str, str] = {}
    for nipc, group in holders.items():
        pool = [organisation for organisation in group if organisation.active] or group
        if len(pool) == 1:
            owners[nipc] = pool[0].code
    return owners


# Network ---------------------------------------------------------------------------


def _allowed(url: str) -> bool:
    return url in ROUTES


def _json(content: bytes, *, bom: bool = False) -> JSONValue:
    try:
        return cast(JSONValue, json.loads(content.decode("utf-8-sig" if bom else "utf-8")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SioeError("JSON SIOE+ inválido.") from exc


def _exchange(
    url: str,
    *,
    method: str,
    body: bytes | None,
    headers: dict[str, str],
    max_bytes: int,
    deadline: float,
) -> tuple[int, str, bytes]:
    """(status, Retry-After, body) over the pinned connection; only a 200 body is read.

    Network failures raise ``OfficialHTTPError`` (transient); size and encoding refusals
    raise ``SioeError``. The API never redirects: any other status is returned unread.
    """
    if not _allowed(url):
        raise SioeError("Destino fora das rotas SIOE+ autorizadas.")
    parsed = urlsplit(url)
    request_headers = {**headers, "User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    try:
        with open_connection(parsed.hostname or "", deadline=deadline) as connection:
            connection.request(method, parsed.path, body=body, headers=request_headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    return response.status, response.getheader("Retry-After", "") or "", b""
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise SioeError("Resposta SIOE+ comprimida recusada.")
                size = response.getheader("Content-Length")
                if size is not None and (not size.isdecimal() or int(size) > max_bytes):
                    raise SioeError("Resposta SIOE+ demasiado extensa.")
                content = bytearray()
                while chunk := response.read1(min(65536, max_bytes + 1 - len(content))):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise SioeError("Resposta SIOE+ demasiado extensa.")
                if size is not None and len(content) != int(size):
                    raise OfficialHTTPError("Resposta oficial truncada.")
                return 200, "", bytes(content)
    except (OfficialHTTPError, SioeError):
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise OfficialHTTPError("Não foi possível obter a fonte oficial em segurança.") from exc


def retry_delay(value: str) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP date), if valid."""
    text = value.strip()
    if text.isdecimal():
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        return None
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def _network_detail(error: OfficialHTTPError) -> str:
    """The failure's exception class only (e.g. TimeoutError); never a payload."""
    cause = error.__cause__
    return type(cause).__name__ if cause is not None else "falha de rede"


class _Client:
    """Sequential, polite public-token session with an optional minimised disk cache."""

    def __init__(self, *, as_of: date, cache_dir: Path | None) -> None:
        self.as_of = as_of
        self.cache = cache_dir / as_of.isoformat() if cache_dir is not None else None
        self.application_id = APPLICATION_ID
        self.token = ""
        self.requests = 0

    def _failure(self, step: str, detail: str) -> SioeError:
        message = f"SIOE+: {step} falhou ({detail})."
        if self.cache is not None:
            message += (
                " O já recolhido ficou na cache: repita com o mesmo --cache-dir para retomar."
            )
        return SioeError(message)

    def _request(
        self, url: str, *, step: str, body: JSONObject | None, max_bytes: int, timeout: int
    ) -> bytes:
        """One logical request: bounded retries with backoff; a 401 renews the token once."""
        renewed = False
        attempt = 0
        while True:
            attempt += 1
            if self.requests:
                time.sleep(PAUSE)
            self.requests += 1
            headers = {"Accept": "application/json"}
            if url != CONFIG_URL:
                headers["applicationID"] = self.application_id
                headers["Content-Type"] = "application/json"
            if self.token and url != LOGIN_URL:
                headers["Authorization"] = f"Bearer {self.token}"
            retry_after = ""
            try:
                status, retry_after, content = _exchange(
                    url,
                    method="GET" if body is None else "POST",
                    body=None if body is None else canonical_json(body).encode(),
                    headers=headers,
                    max_bytes=max_bytes,
                    deadline=time.monotonic() + timeout,
                )
            except OfficialHTTPError as exc:
                status, detail = 0, _network_detail(exc)
            except SioeError as exc:
                raise self._failure(step, str(exc).rstrip(".")) from exc
            else:
                if status == 200:
                    return content
                detail = f"HTTP {status}"
            renewable = url not in {CONFIG_URL, LOGIN_URL} and bool(self.token)
            if status == 401 and renewable and not renewed:
                # An expired or revoked public token: log in again, then repeat this request.
                renewed = True
                attempt -= 1
                self.login()
                continue
            if status and status not in RETRY_STATUSES:
                raise self._failure(step, detail)
            if attempt >= MAX_ATTEMPTS:
                raise self._failure(step, f"{detail}; {attempt} tentativas")
            delay = min(MAX_BACKOFF, BACKOFF * 2 ** (attempt - 1))
            requested = retry_delay(retry_after) if retry_after else None
            if requested is not None:
                if requested > MAX_RETRY_AFTER:
                    raise self._failure(step, f"{detail}; a fonte pediu uma pausa longa")
                delay = max(delay, requested)
            time.sleep(delay)

    def login(self) -> None:
        config = _object(
            _json(
                self._request(
                    CONFIG_URL,
                    step="configuração pública",
                    body=None,
                    max_bytes=CONFIG_BYTES,
                    timeout=CONFIG_TIMEOUT,
                ),
                bom=True,
            )
        )
        root = config.get("ROOT_API")
        if root is not None and (not isinstance(root, str) or root.rstrip("/") != API_ROOT):
            raise SioeError("A API do SIOE+ mudou de endereço; reveja a lista de rotas.")
        application = config.get("APP_DEFAULT_APPLICATION_ID")
        if isinstance(application, str) and UUID.fullmatch(application):
            self.application_id = application
        response = _object(
            _json(
                self._request(
                    LOGIN_URL,
                    step="autenticação pública",
                    body={
                        "auth_type": "public",
                        "auth_key": b64encode(self.application_id.encode()).decode(),
                    },
                    max_bytes=LOGIN_BYTES,
                    timeout=CONFIG_TIMEOUT,
                )
            )
        )
        token = response.get("access_token")
        if not isinstance(token, str) or len(token) > 4096 or not TOKEN.fullmatch(token):
            raise SioeError("O SIOE+ não concedeu a sessão pública.")
        self.token = token

    def _cached(
        self,
        name: str,
        *,
        step: str,
        url: str,
        body: JSONObject,
        max_bytes: int,
        timeout: int,
        minimise: Callable[[JSONValue], JSONObject],
    ) -> JSONObject:
        """Minimised response, from ``<cache>/<as_of>/<name>.json`` when a run resumes.

        Each response is written as soon as it arrives, so a failed crawl resumes at the
        first request it has not completed.
        """
        path = self.cache / f"{name}.json" if self.cache is not None else None
        if path is not None and path.is_file():
            try:
                return _object(cast(JSONValue, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, RecursionError) as exc:
                raise SioeError(
                    f"SIOE+: cache ilegível em {path.name}; apague esse ficheiro para o recolher "
                    "de novo."
                ) from exc
        content = self._request(url, step=step, body=body, max_bytes=max_bytes, timeout=timeout)
        try:
            value = minimise(_json(content))
        except SioeError as exc:
            raise self._failure(step, str(exc).rstrip(".")) from exc
        if path is not None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(canonical_json(value))
            os.replace(temporary, path)
        return value

    def search(self, *, type_id: int | None = None) -> list[JSONValue]:
        name = f"search-type-{type_id}" if type_id is not None else "search-universe"
        pages: list[JSONValue] = []
        number, total_pages = 1, 1
        while number <= total_pages:
            body: JSONObject = {
                "pagination": {
                    "pageNumber": number,
                    "pageSize": PAGE_SIZE,
                    "sortOrder": 0,
                    "sortField": "sioeCode",
                },
                "refDate": f"{self.as_of.isoformat()}T00:00:00.000Z",
                "universeTypeId": UNIVERSE,
                "classId": ENTITY_CLASS,
                "extinct": 1,
            }
            if type_id is not None:
                body["types"] = [type_id]
            subject = f"do tipo {type_id}" if type_id is not None else "universal"
            page = self._cached(
                f"{name}-{number}",
                step=f"pesquisa {subject} (página {number})",
                url=SEARCH_URL,
                body=body,
                max_bytes=SEARCH_BYTES,
                timeout=SEARCH_TIMEOUT,
                minimise=minimise_search,
            )
            pages.append(page)
            if number == 1:
                total_pages = max(1, search_page(page, number=1)[1])
            number += 1
        return pages

    def history(self, code: str) -> JSONObject:
        return self._cached(
            f"history-{code}",
            step=f"histórico da entidade {code}",
            url=HISTORY_URL,
            body={"applicationID": self.application_id, "sioeCode": code},
            max_bytes=HISTORY_BYTES,
            timeout=HISTORY_TIMEOUT,
            minimise=minimise_history,
        )


def fetch_snapshot(
    *,
    as_of: date,
    cache_dir: Path | None = None,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SioeSnapshot:
    """Universe and type searches in full; ``limit`` bounds only the history crawl."""
    if as_of > timezone.localdate():
        raise SioeError("Não é possível consultar o SIOE+ numa data futura.")
    client = _Client(as_of=as_of, cache_dir=cache_dir)
    retrieved_at = timezone.now()
    client.login()
    organisations, ghosts = parse_universe(client.search())
    known = {organisation.code for organisation in organisations}
    types: dict[str, int] = {}
    for type_id in sorted(ENTITY_TYPES):
        for code in parse_codes(client.search(type_id=type_id)) & known:
            # An entity tagged by two type searches keeps its history's current type.
            if types.setdefault(code, type_id) != type_id:
                types[code] = 0
    types = {code: type_id for code, type_id in types.items() if type_id}
    selected = organisations[:limit] if limit is not None else organisations
    histories: dict[str, History] = {}
    for done, organisation in enumerate(selected, 1):
        histories[organisation.code] = parse_history(
            client.history(organisation.code), code=organisation.code
        )
        if progress is not None:
            progress(done, len(selected))
    return SioeSnapshot(
        as_of=as_of,
        retrieved_at=retrieved_at,
        organisations=organisations,
        types=types,
        histories=histories,
        ghosts=ghosts,
    )


# Resolution ------------------------------------------------------------------------


def _identities(scheme: str, external_ids: Iterable[str]) -> dict[str, SourceIdentity]:
    found: dict[str, SourceIdentity] = {}
    for chunk in batched(sorted(set(external_ids)), BATCH, strict=False):
        for identity in SourceIdentity.objects.select_related("entity", "reviewed_by").filter(
            source=scheme, external_id__in=chunk
        ):
            found[identity.external_id] = identity
    return found


@dataclass(frozen=True)
class OrganisationPlan:
    existing: dict[str, Entity]
    via_nipc: dict[str, Entity]
    new: tuple[str, ...]
    owners: dict[str, str]
    held_nipcs: dict[str, Entity]


def plan_organisations(snapshot: SioeSnapshot) -> OrganisationPlan:
    """Read-only: SIOE code, else an existing organisation holding the owned NIPC, else new."""
    codes = [organisation.code for organisation in snapshot.organisations]
    existing = {code: identity.entity for code, identity in _identities(SIOE, codes).items()}
    owners = nipc_owners(snapshot.organisations)
    held = {nipc: identity.entity for nipc, identity in _identities(NIPC, owners).items()}
    # An organisation already anchored by another SIOE code is never merged through NIPC.
    anchored: set[object] = set()
    for chunk in batched([entity.pk for entity in held.values()], BATCH, strict=False):
        anchored.update(
            SourceIdentity.objects.filter(source=SIOE, entity_id__in=chunk).values_list(
                "entity_id", flat=True
            )
        )
    via_nipc: dict[str, Entity] = {}
    new: list[str] = []
    for organisation in snapshot.organisations:
        if organisation.code in existing:
            continue
        owned = owners.get(organisation.nipc) == organisation.code
        holder = held.get(organisation.nipc) if owned else None
        if holder is not None and holder.pk not in anchored:
            via_nipc[organisation.code] = holder
        else:
            new.append(organisation.code)
    return OrganisationPlan(existing, via_nipc, tuple(new), owners, held)


def _resolve_organisations(snapshot: SioeSnapshot, plan: OrganisationPlan) -> dict[str, Entity]:
    by_code = {organisation.code: organisation for organisation in snapshot.organisations}
    rows: dict[str, tuple[str, str, str]] = {}
    for code in plan.new:
        history = snapshot.histories.get(code)
        kind, classification = classify(
            entity_type(snapshot, code),
            higher_education=history is not None and history.higher_education,
        )
        rows[code] = (by_code[code].name, kind, classification)
    created = official_entities_bulk(SIOE, rows)
    dissolved: list[Entity] = []
    for code in plan.new:
        extinct_on = by_code[code].extinct_on
        if extinct_on is not None:
            created[code].dissolution_date = extinct_on
            dissolved.append(created[code])
    Entity.objects.bulk_update(dissolved, ["dissolution_date"], batch_size=1000)
    for code, entity in plan.via_nipc.items():
        SourceIdentity(source=SIOE, external_id=code, entity=entity).save()
    resolved = {**plan.existing, **plan.via_nipc, **created}
    attached: list[SourceIdentity] = []
    for nipc, code in plan.owners.items():
        if nipc not in plan.held_nipcs:
            identity = SourceIdentity(source=NIPC, external_id=nipc, entity=resolved[code])
            identity.full_clean(
                exclude=["entity"], validate_unique=False, validate_constraints=False
            )
            attached.append(identity)
    SourceIdentity.objects.bulk_create(attached, batch_size=1000)
    return resolved


# Claims ----------------------------------------------------------------------------


def _day(value: date | None) -> str:
    return value.isoformat() if value is not None else "desconhecida"


def _period(start: date | None, end: date | None) -> str:
    text = f" Início: {_day(start)}."
    if end is not None:
        text += f" Fim: {end.isoformat()}."
    return text


def _interval_status(start: date | None, end: date | None, *, active: bool, as_of: date) -> str:
    if start is not None and start > as_of:
        return TemporalStatus.UNKNOWN
    if end is not None:
        return TemporalStatus.ENDED if end <= as_of else TemporalStatus.CURRENT
    return TemporalStatus.CURRENT if active else TemporalStatus.UNKNOWN


def member_status(member: Member, *, active: bool, as_of: date) -> str:
    """Open mandates past their planned term, or of extinct bodies, are unknown."""
    if member.end is None and member.expiry is not None and member.expiry < as_of:
        return TemporalStatus.UNKNOWN
    return _interval_status(member.start, member.end, active=active, as_of=as_of)


def _government_label(ministry: Ministry) -> str:
    numeral = ministry.code.split("_", 1)[0]
    cabinet = "Governo Constitucional" if ministry.national else "Governo Regional"
    return f"{numeral} {cabinet}"


@dataclass(frozen=True)
class _Claims:
    structure: dict[str, tuple[ObservationInput, ...]]
    boards: dict[str, tuple[ObservationInput, ...]]
    unresolved: int
    unlinked_ministries: int


def _base(
    snapshot: SioeSnapshot,
    *,
    external_id: str,
    category: str,
    passage: str,
    reference: str,
    object: Entity,
    object_name: str,
    object_identifier: str,
    kind: str,
    temporal_status: str,
    identity: SourceIdentity | None = None,
    subject_name: str = "",
    subject_reference: str = "",
    role: str = "",
    role_class: str = "",
    effective_start: date | None = None,
    effective_end: date | None = None,
    term: Term | None = None,
) -> ObservationInput:
    """A SIOE+ observation whose revision digests every substantive value."""
    return revised(
        ObservationInput(
            external_id=external_id,
            revision="",
            category=category,
            passage=passage,
            source_url=DATASET.url,
            publisher=DATASET.publisher,
            reference=reference,
            title=DATASET.title,
            identity=identity,
            subject_name=subject_name,
            subject_reference=subject_reference,
            effective_start=effective_start,
            effective_end=effective_end,
            object=object,
            object_name=object_name,
            object_identifier=object_identifier,
            kind=kind,
            dataset=DATASET.key,
            role=role,
            role_class=role_class,
            term=term,
            temporal_status=temporal_status,
            retrieved_at=snapshot.retrieved_at,
        )
    )


def _claims(
    snapshot: SioeSnapshot,
    organisations: dict[str, Entity],
    ministry_entities: dict[str, Entity],
) -> _Claims:
    by_code = {organisation.code: organisation for organisation in snapshot.organisations}
    found = ministries(snapshot)
    referenced = {
        link.code
        for history in snapshot.histories.values()
        for link in (*history.parents, *history.predecessors)
    }
    identities = _identities(SIOE, [*snapshot.histories, *(m.key for m in found.values())])
    others = {
        code: identity.entity
        for code, identity in _identities(SIOE, referenced - set(organisations)).items()
    }
    targets = {**others, **organisations}
    structure: dict[str, tuple[ObservationInput, ...]] = {}
    boards: dict[str, tuple[ObservationInput, ...]] = {}
    unresolved = 0
    for code, history in snapshot.histories.items():
        organisation = by_code[code]
        subject = organisations[code]
        identity = identities[code]
        items: list[ObservationInput] = []
        for tutela in history.tutelas:
            ministry = found[tutela.code]
            share = "tutela principal" if tutela.main else "tutela partilhada"
            items.append(
                _base(
                    snapshot,
                    external_id=f"tutela:{tutela.row_id}",
                    category=ORGANISATION_STRUCTURE,
                    identity=identity,
                    object=ministry_entities[ministry.key],
                    object_name=ministry.label,
                    object_identifier=f"sioe:{ministry.key}",
                    kind=Relationship.Kind.PART_OF,
                    role=TUTELA,
                    passage=(
                        f"{organisation.name} — tutela: {ministry.label} "
                        f"({_government_label(ministry)}; código {tutela.code}; {share}), "
                        "segundo o SIOE+." + _period(tutela.start, tutela.end)
                    ),
                    reference=f"SIOE {code} / tutela {tutela.row_id}",
                    effective_start=tutela.start,
                    effective_end=tutela.end,
                    temporal_status=_interval_status(
                        tutela.start, tutela.end, active=organisation.active, as_of=snapshot.as_of
                    ),
                )
            )
        for link, kind in (
            *((parent, Relationship.Kind.PART_OF) for parent in history.parents),
            *((predecessor, Relationship.Kind.SUCCESSION) for predecessor in history.predecessors),
        ):
            target = targets.get(link.code)
            if target is None or target.pk == subject.pk:
                unresolved += 1
                continue
            if kind == Relationship.Kind.PART_OF:
                passage = (
                    f"{organisation.name} integra {link.name} (entidade agregadora "
                    f"{link.code} no SIOE+)." + _period(link.start, link.end)
                )
                reference = f"SIOE {code} / agregação {link.row_id}"
                external_id = f"agregacao:{link.row_id}"
                status = _interval_status(
                    link.start, link.end, active=organisation.active, as_of=snapshot.as_of
                )
            else:
                # Listed on the successor: the subject succeeds the related (earlier) entity.
                passage = (
                    f"{organisation.name} sucede a {link.name} ({link.relation}; entidade "
                    f"{link.code} no SIOE+). Data: {_day(link.start)}."
                )
                reference = f"SIOE {code} / sucessão {link.row_id}"
                external_id = f"sucessao:{link.row_id}"
                status = TemporalStatus.UNKNOWN
            items.append(
                _base(
                    snapshot,
                    external_id=external_id,
                    category=ORGANISATION_STRUCTURE,
                    identity=identity,
                    object=target,
                    object_name=link.name,
                    object_identifier=f"sioe:{link.code}",
                    kind=kind,
                    passage=passage,
                    reference=reference,
                    effective_start=link.start,
                    effective_end=link.end,
                    temporal_status=status,
                )
            )
        if items:
            structure[f"{STRUCTURE_PREFIX}{code}"] = tuple(items)
        board = tuple(
            _board_item(snapshot, member, organisation=organisation, entity=subject)
            for member in history.members
        )
        if board:
            boards[f"{BOARDS_PREFIX}{code}"] = board
    unlinked = 0
    governments = _identities(
        IdentityScheme.GOVERNMENT,
        [f"government:gc{m.government}" for m in found.values() if m.national],
    )
    terms = {
        term.code: term
        for term in Term.objects.filter(
            kind=Term.Kind.GOVERNMENT, code__in=[f"gc{m.government}" for m in found.values()]
        )
    }
    for ministry in found.values():
        if not ministry.national:
            continue
        government = governments.get(f"government:gc{ministry.government}")
        if government is None:
            # The Government importer owns Government entities; link on a later run.
            unlinked += 1
            continue
        term = terms.get(f"gc{ministry.government}")
        if term is None:
            status = TemporalStatus.UNKNOWN
        elif term.end_date is not None and term.end_date < snapshot.as_of:
            status = TemporalStatus.ENDED
        else:
            status = TemporalStatus.CURRENT
        structure[f"{STRUCTURE_PREFIX}{ministry.key}"] = (
            _base(
                snapshot,
                external_id=f"governo:gc{ministry.government}",
                category=ORGANISATION_STRUCTURE,
                identity=identities[ministry.key],
                object=government.entity,
                object_name=government.entity.name,
                object_identifier=f"government:gc{ministry.government}",
                kind=Relationship.Kind.PART_OF,
                term=term,
                passage=(
                    f"{ministry.label} é uma área governativa do "
                    f"{_government_label(ministry)} (código {ministry.code} no SIOE+)."
                ),
                reference=f"SIOE área governativa {ministry.code}",
                temporal_status=status,
            ),
        )
    return _Claims(structure, boards, unresolved, unlinked)


def _board_item(
    snapshot: SioeSnapshot, member: Member, *, organisation: Organisation, entity: Entity
) -> ObservationInput:
    kind = (
        Relationship.Kind.DIRECTORSHIP
        if entity.classification == _CLASS.STATE_COMPANY
        else Relationship.Kind.PUBLIC_OFFICE
    )
    role = member.role or "cargo sem designação publicada"
    passage = (
        f"{member.name} — {role} em {organisation.name} (órgão de direção no SIOE+)."
        + _period(member.start, member.end)
    )
    if member.expiry is not None:
        passage += (
            f" Termo previsto do mandato: {member.expiry.isoformat()} "
            "(data planeada, não é data de cessação)."
        )
    if member.reversed_end is not None:
        passage += (
            f" A data de fim publicada ({member.reversed_end.isoformat()}) antecede o início "
            "e não foi usada."
        )
    if member.act_url:
        passage += f" Ato de designação: {member.act_url}"
    return _base(
        snapshot,
        external_id=f"membro:{member.row_id}",
        category=OFFICE_HOLDING,
        subject_name=member.name,
        subject_reference=f"sioe:{organisation.code}:membro:{member.row_id}",
        object=entity,
        object_name=organisation.name,
        object_identifier=f"sioe:{organisation.code}",
        kind=kind,
        role=member.role,
        role_class=role_class(member.role),
        passage=passage,
        reference=f"SIOE {organisation.code} / membro {member.row_id}",
        effective_start=member.start,
        effective_end=member.end,
        temporal_status=member_status(member, active=organisation.active, as_of=snapshot.as_of),
    )


# Summary and application -----------------------------------------------------------


def summarise(snapshot: SioeSnapshot) -> dict[str, int]:
    """Read-only counts for a dry run; no payloads or personal data."""
    plan = plan_organisations(snapshot)
    found = ministries(snapshot)
    national = [m for m in found.values() if m.national]
    governments = _identities(
        IdentityScheme.GOVERNMENT, [f"government:gc{m.government}" for m in national]
    )
    histories = snapshot.histories.values()
    return {
        "organisations": len(snapshot.organisations),
        "ghosts": snapshot.ghosts,
        "existing": len(plan.existing),
        "via_nipc": len(plan.via_nipc),
        "new": len(plan.new),
        "nipc_attach": sum(1 for nipc in plan.owners if nipc not in plan.held_nipcs),
        "histories": len(snapshot.histories),
        "ministries": len(found),
        "regional_ministries": len(found) - len(national),
        "unlinked_ministries": sum(
            1 for m in national if f"government:gc{m.government}" not in governments
        ),
        "tutelas": sum(len(h.tutelas) for h in histories),
        "legacy_tutelas": sum(h.legacy_tutelas for h in histories),
        "parents": sum(len(h.parents) for h in histories),
        "successions": sum(len(h.predecessors) for h in histories),
        "members": sum(len(h.members) for h in histories),
        "dropped_members": sum(h.dropped_members for h in histories),
    }


def apply_snapshot(snapshot: SioeSnapshot) -> dict[str, int]:
    """Atomic: organisations, ministries, structure claims and board candidates.

    A partial crawl (``--limit``) never ceases scopes it did not observe.
    """
    with import_transaction():
        plan = plan_organisations(snapshot)
        organisations = _resolve_organisations(snapshot, plan)
        found = ministries(snapshot)
        ministry_entities = official_entities_bulk(
            SIOE,
            {
                ministry.key: (
                    ministry.name,
                    _KIND.ORGANISATION,
                    _CLASS.GOVERNMENT_DEPARTMENT,
                )
                for ministry in found.values()
            },
        )
        claims = _claims(snapshot, organisations, ministry_entities)
        result = {
            "new": len(plan.new),
            "via_nipc": len(plan.via_nipc),
            "ministries": len(found),
            "unlinked_ministries": claims.unlinked_ministries,
            "unresolved_links": claims.unresolved,
        }
        for prefix, scopes in (
            (STRUCTURE_PREFIX, claims.structure),
            (BOARDS_PREFIX, claims.boards),
        ):
            if snapshot.complete:
                counts = sync_scoped_snapshot(
                    source=EnrichmentSource.SIOE,
                    prefix=prefix,
                    snapshots=scopes,
                    as_of=snapshot.as_of,
                )
            else:
                counts = {}
                for scope, items in scopes.items():
                    for key, value in sync_observations(
                        source=EnrichmentSource.SIOE,
                        scope=scope,
                        observations=items,
                        as_of=snapshot.as_of,
                    ).items():
                        counts[key] = counts.get(key, 0) + value
            for key, value in counts.items():
                result[f"{prefix.rstrip(':').removeprefix('sioe-')}_{key}"] = value
        return result
