"""Portal BASE (IMPIC) public contracts as events between organisations keyed by NIPC.

The dados.gov.pt bulk dataset publishes one zipped JSON array per year
(``contratosYYYY.zip`` → ``ContratosYYYY.json``, 65k to 300k records; verified
2026-09-28). Resource URLs change on every re-upload, so they are resolved through the
dados.gov.pt API (by the dataset's stable id) on each run. A year file is a complete
snapshot: scope = year, absent contracts cease.

Parties are ``"<NIF> - <name>"`` (buyers, suppliers) or ``"<NIF>-<name>"`` (bidders).
Only legal-person NIPCs anchor organisations; natural persons (masked ``"- - name"``),
foreign or malformed numbers are dropped with their names, never stored. A contract left
without any legal-person party is skipped. Files are read twice as streams: the first
pass collects the organisations to resolve in bulk, the second emits the events, so a
year never sits in memory as parsed objects.
"""

import hashlib
import html
import io
import json
import re
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import IO, cast
from urllib.parse import urlsplit

from .catalogue import DATASETS
from .events import EventInput, PartyInput, sync_events
from .identity import official_entities_bulk, valid_nipc
from .models import Entity, EventParty, IdentityScheme, SourceIdentity, import_transaction
from .official_http import OfficialHTTPError, download

DATASET_KEY = "base_contratos"
DATASET = DATASETS[DATASET_KEY]
NIPC = IdentityScheme.NIPC

HOST = "dados.gov.pt"
# Stable dataset id; the slug carries the year range and is renamed as years are added.
DATASET_ID = "66d72d488ca4b7cb2de28712"
API_PATH = f"/api/1/datasets/{DATASET_ID}/"
API_URL = f"https://{HOST}{API_PATH}"
RESOURCE_TITLE = re.compile(r"contratos(?P<year>20[0-9]{2})\.zip")
RESOURCE_PATH = re.compile(
    r"/s/resources/[a-z0-9-]{1,200}/[0-9]{8}-[0-9]{6}(?:-[0-9a-f]{1,32})?"
    r"/contratos(?P<year>20[0-9]{2})\.zip"
)
MEMBER = re.compile(r"contratos(?P<year>20[0-9]{2})\.json", re.IGNORECASE)
RECORD_URL = "https://www.base.gov.pt/Base4/pt/detalhe/?type=contratos&id={id}"
FIRST_YEAR = 2012

API_BYTES = 1024 * 1024
ARCHIVE_BYTES = 256 * 1024 * 1024
JSON_BYTES = 3 * 1024 * 1024 * 1024
API_TIMEOUT = 60
ARCHIVE_TIMEOUT = 900
CHUNK_CHARS = 1024 * 1024
RECORD_CHARS = 4 * 1024 * 1024

TITLE_LENGTH = 500
ENTITY_NAME_LENGTH = 240
PARTY_NAME_LENGTH = 300
DETAIL_LENGTH = 160
BASIS_LENGTH = 200
MAX_AMOUNT = Decimal(10) ** 16
CENT = Decimal("0.01")

RECORD_ID = re.compile(r"[0-9]{1,20}")
# The NIF is a leading run of digits (occasionally spaced); anything else is not a NIPC.
PARTY = re.compile(r"\s*(?P<nif>[0-9][0-9 ]*[0-9])\s*-\s*(?P<name>.*)", re.DOTALL)
CPV = re.compile(r"\s*(?P<code>[0-9]{8}-[0-9])\b")
DAY = re.compile(r"(?P<day>[0-9]{2})/(?P<month>[0-9]{2})/(?P<year>[0-9]{4})")
ISO_DAY = re.compile(r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})")

ROLES = (
    ("adjudicante", EventParty.Role.BUYER),
    ("adjudicatarios", EventParty.Role.SUPPLIER),
    ("concorrentes", EventParty.Role.BIDDER),
)

_KIND = Entity.Kind
_CLASS = Entity.Classification
# Name forms that override the role default; set only when an organisation is created.
NAME_FORMS: tuple[tuple[re.Pattern[str], tuple[str, str]], ...] = (
    (
        re.compile(r"^(?:munic[ií]pio|c[âa]mara\s+municipal)\s+d", re.IGNORECASE),
        (_KIND.ORGANISATION, _CLASS.MUNICIPALITY),
    ),
    (
        re.compile(
            r"^(?:(?:junta\s+de\s+)?freguesia\s+d|uni[ãa]o\s+d(?:as|e)\s+freguesias\b)",
            re.IGNORECASE,
        ),
        (_KIND.ORGANISATION, _CLASS.PARISH),
    ),
    (
        re.compile(r"^(?:universidade|instituto\s+polit[ée]cnico)\b", re.IGNORECASE),
        (_KIND.UNIVERSITY, _CLASS.HIGHER_EDUCATION),
    ),
    (re.compile(r"^funda[çc][ãa]o\b", re.IGNORECASE), (_KIND.ORGANISATION, _CLASS.FOUNDATION)),
    (re.compile(r"^associa[çc][ãa]o\b", re.IGNORECASE), (_KIND.ORGANISATION, _CLASS.ASSOCIATION)),
    (
        re.compile(r"\bcooperativa\b|(?:^|[\s,])(?:C\.?\s?R\.?\s?L\.?|CIPRL)$", re.IGNORECASE),
        (_KIND.ORGANISATION, _CLASS.COOPERATIVE),
    ),
)
# Company legal forms ending a contracting authority's name (EPE hospitals, municipal
# and state companies); such buyers are public enterprises, not administrative bodies.
COMPANY_FORM = re.compile(
    r"(?:^|[\s,])(?:S\.\s?A\.?|SA|E\.\s?P\.\s?E\.?|EPE|E\.\s?M\.|E\.\s?E\.\s?M\.?|EEM|"
    r"E\.\s?I\.\s?M\.?|EIM|Lda\.?|LDA\.?)$"
)
PUBLIC_BODY = (_KIND.ORGANISATION, _CLASS.PUBLIC_BODY)
STATE_COMPANY = (_KIND.COMPANY, _CLASS.STATE_COMPANY)
COMPANY = (_KIND.COMPANY, _CLASS.COMPANY)
PUBLIC_NIPC_PREFIX = "6"

type Raw = str | int | float | Decimal | bool | list[Raw] | dict[str, Raw] | None
type RawObject = dict[str, Raw]
type Detail = str | list[str]


class BaseContractsError(ValueError):
    """Safe, payload-free failure for an unexpected BASE file or dados.gov.pt response."""


@dataclass(frozen=True)
class Resource:
    year: int
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class Party:
    role: str
    nipc: str
    name: str


@dataclass(frozen=True, slots=True)
class Contract:
    record_id: str
    title: str
    date: date | None
    end_date: date | None
    amount: Decimal | None
    details: dict[str, Detail]
    parties: tuple[Party, ...]


@dataclass
class YearScan:
    """First-pass counts and the organisations a year file names (no personal data)."""

    year: int
    counts: Counter[str] = field(default_factory=Counter)
    organisations: dict[str, tuple[str, str, str]] = field(default_factory=dict)


# Parsing ---------------------------------------------------------------------------


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean(value: Raw, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _cap(" ".join(html.unescape(value).split()), limit)


def parse_party(value: Raw, role: str) -> Party | None:
    """A legal-person party; masked, foreign, malformed and personal numbers yield None."""
    if not isinstance(value, str):
        return None
    match = PARTY.fullmatch(value)
    if match is None:
        return None
    nipc = match["nif"].replace(" ", "")
    if not valid_nipc(nipc):
        return None
    name = _clean(match["name"], PARTY_NAME_LENGTH).strip(" -") or f"NIPC {nipc}"
    return Party(role=role, nipc=nipc, name=name)


def parse_day(value: Raw) -> date | None:
    """``dd/mm/yyyy`` (or ISO) day; empty or impossible values are unknown."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = DAY.fullmatch(text) or ISO_DAY.fullmatch(text)
    if match is None:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return None


def parse_amount(value: Raw) -> Decimal | None:
    """Euros to the cent; zero means "not reported", negative or absurd values are unknown."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.replace("€", "").replace(" ", "").replace("\u00a0", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        value = text
    if not isinstance(value, int | float | Decimal | str):
        return None
    try:
        amount = Decimal(str(value) if isinstance(value, float) else value)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount >= MAX_AMOUNT:
        return None
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _texts(value: Raw, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _clean(item, limit))]


def _details(record: RawObject) -> dict[str, Detail]:
    details: dict[str, Detail] = {}
    if kinds := _texts(record.get("tipoContrato"), DETAIL_LENGTH):
        details["tipoContrato"] = kinds
    if procedure := _clean(record.get("tipoprocedimento"), DETAIL_LENGTH):
        details["tipoProcedimento"] = procedure
    codes: list[str] = []
    cpvs = record.get("cpv")
    for item in cpvs if isinstance(cpvs, list) else []:
        match = CPV.match(item) if isinstance(item, str) else None
        if match is not None and match["code"] not in codes:
            codes.append(match["code"])
    if codes:
        details["cpv"] = codes
    for key in ("dataPublicacao", "dataDecisaoAdjudicacao"):
        if (day := parse_day(record.get(key))) is not None:
            details[key] = day.isoformat()
    if (effective := parse_amount(record.get("PrecoTotalEfetivo"))) is not None:
        details["precoTotalEfetivo"] = str(effective)
    if basis := _clean(record.get("fundamentacao"), BASIS_LENGTH):
        details["fundamentacao"] = basis
    return details


def _parties(record: RawObject) -> tuple[tuple[Party, ...], int]:
    """Legal-person parties per role (one per NIPC) and the number of dropped items."""
    parties: list[Party] = []
    dropped = 0
    for key, role in ROLES:
        items = record.get(key)
        if not isinstance(items, list):
            continue
        seen: set[str] = set()
        for item in items:
            if isinstance(item, str) and not item.strip():
                continue
            party = parse_party(item, role)
            if party is None:
                dropped += 1
            elif party.nipc not in seen:
                seen.add(party.nipc)
                parties.append(party)
    return tuple(parties), dropped


def record_id(record: RawObject) -> str | None:
    value = record.get("idcontrato")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not RECORD_ID.fullmatch(value.strip()):
        return None
    return value.strip()


def parse_contract(record: RawObject, identifier: str) -> tuple[Contract, int]:
    """The contract with its legal-person parties, and how many parties were dropped."""
    parties, dropped = _parties(record)
    title = (
        _clean(record.get("objectoContrato"), TITLE_LENGTH)
        or _clean(record.get("descContrato"), TITLE_LENGTH)
        or f"Contrato público n.º {identifier}"
    )
    contract = Contract(
        record_id=identifier,
        title=title,
        date=parse_day(record.get("dataCelebracaoContrato")),
        end_date=parse_day(record.get("dataFechoContrato")),
        amount=parse_amount(record.get("precoContratual")),
        details=_details(record),
        parties=parties,
    )
    return contract, dropped


def classify(name: str, *, nipc: str, buyer: bool) -> tuple[str, str]:
    """(kind, classification) for a new organisation; existing entities keep theirs."""
    for pattern, kind in NAME_FORMS:
        if pattern.search(name):
            return kind
    if buyer:
        return STATE_COMPANY if COMPANY_FORM.search(name) else PUBLIC_BODY
    return PUBLIC_BODY if nipc.startswith(PUBLIC_NIPC_PREFIX) else COMPANY


# Streaming -------------------------------------------------------------------------


class _JSONArray:
    """Top-level JSON array read one element at a time from a text stream."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._reader = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
        self._decoder = json.JSONDecoder(parse_float=Decimal)
        self._buffer = ""
        self._index = 0
        self._eof = False

    def _fill(self) -> bool:
        if self._eof:
            return False
        chunk = self._reader.read(CHUNK_CHARS)
        if not chunk:
            self._eof = True
            return False
        self._buffer = self._buffer[self._index :] + chunk
        self._index = 0
        return True

    def _peek(self) -> str:
        """The next non-whitespace character, or "" at the end of the stream."""
        while True:
            while self._index < len(self._buffer) and self._buffer[self._index] in " \t\r\n":
                self._index += 1
            if self._index < len(self._buffer):
                return self._buffer[self._index]
            if not self._fill():
                return ""

    def _value(self) -> Raw:
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._index)
            except json.JSONDecodeError:
                # Incomplete element: read on, within the per-record bound.
                if len(self._buffer) - self._index > RECORD_CHARS or not self._fill():
                    raise BaseContractsError("JSON BASE inválido.") from None
                continue
            self._index = end
            return cast(Raw, value)

    def __iter__(self) -> Iterator[Raw]:
        if self._peek() != "[":
            raise BaseContractsError("O ficheiro BASE não é uma lista JSON.")
        self._index += 1
        if self._peek() == "]":
            self._index += 1
        else:
            while True:
                self._peek()
                yield self._value()
                separator = self._peek()
                self._index += 1
                if separator == "]":
                    break
                if separator != ",":
                    raise BaseContractsError("JSON BASE inválido.")
        if self._peek() != "":
            raise BaseContractsError("JSON BASE inválido.")


def records(archive: bytes, year: int) -> Iterator[Raw]:
    """Stream the one ``ContratosYYYY.json`` array of a year archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = [info for info in bundle.infolist() if not info.is_dir()]
            if len(members) != 1:
                raise BaseContractsError("Arquivo BASE inesperado.")
            member = members[0]
            match = MEMBER.fullmatch(member.filename)
            if match is None or int(match["year"]) != year or member.file_size > JSON_BYTES:
                raise BaseContractsError("Arquivo BASE inesperado.")
            with bundle.open(member) as stream:
                yield from _JSONArray(stream)
    except (zipfile.BadZipFile, zlib.error, EOFError, UnicodeDecodeError) as exc:
        raise BaseContractsError("Arquivo BASE ilegível.") from exc


def _digest(contract: Contract, dropped: int) -> bytes:
    text = repr((contract, dropped))
    return hashlib.blake2b(text.encode(), digest_size=12).digest()


def contracts(archive: bytes, year: int, counts: Counter[str] | None = None) -> Iterator[Contract]:
    """Unique contracts with at least one legal-person party, in file order.

    A repeated ``idcontrato`` keeps its first record. Repeats are compared on the
    retained content only (the file repeats per-lot rows that differ in unkept flags):
    identical ones are counted as duplicates, different ones as conflicts.
    """
    tally: Counter[str] = Counter() if counts is None else counts
    first: dict[str, bytes] = {}
    for item in records(archive, year):
        tally["rows"] += 1
        if not isinstance(item, dict):
            tally["invalid"] += 1
            continue
        identifier = record_id(item)
        if identifier is None:
            tally["invalid"] += 1
            continue
        contract, dropped = parse_contract(item, identifier)
        digest = _digest(contract, dropped)
        known = first.get(identifier)
        if known is not None:
            tally["duplicates" if known == digest else "conflicts"] += 1
            continue
        first[identifier] = digest
        tally["contracts"] += 1
        tally["dropped_parties"] += dropped
        if not contract.parties:
            tally["skipped"] += 1
            continue
        if contract.amount is None:
            tally["without_amount"] += 1
        for party in contract.parties:
            tally[f"parties_{party.role}"] += 1
        yield contract


def scan(archive: bytes, year: int) -> YearScan:
    """First pass: counts and the organisations (NIPC → kind/classification) to resolve."""
    result = YearScan(year=year)
    names: dict[str, Counter[str]] = {}
    buyers: set[str] = set()
    for contract in contracts(archive, year, result.counts):
        for party in contract.parties:
            names.setdefault(party.nipc, Counter())[party.name] += 1
            if party.role == EventParty.Role.BUYER:
                buyers.add(party.nipc)
    if not result.counts["rows"]:
        raise BaseContractsError("Ficheiro BASE vazio; não foi aplicado.")
    for nipc in sorted(names):
        # The most frequent published name (first seen on ties) names a new entity.
        name = _cap(names[nipc].most_common(1)[0][0], ENTITY_NAME_LENGTH)
        kind, classification = classify(name, nipc=nipc, buyer=nipc in buyers)
        result.organisations[nipc] = (name, kind, classification)
    result.counts["organisations"] = len(result.organisations)
    return result


def event_input(contract: Contract, entities: dict[str, Entity]) -> EventInput:
    return EventInput(
        record_id=contract.record_id,
        kind="contract",
        title=contract.title,
        date=contract.date,
        end_date=contract.end_date,
        amount=contract.amount,
        amount_label="Preço contratual",
        record_url=RECORD_URL.format(id=contract.record_id),
        details=contract.details,
        parties=tuple(
            PartyInput(
                role=party.role,
                name=party.name,
                entity=entities[party.nipc],
                identifier=party.nipc,
            )
            for party in contract.parties
        ),
    )


# Network ---------------------------------------------------------------------------


def _api_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == HOST
        and parts.path == API_PATH
        and not parts.query
        and not parts.fragment
    )


def _resource_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == HOST
        and RESOURCE_PATH.fullmatch(parts.path) is not None
        and not parts.query
        and not parts.fragment
    )


def parse_resources(payload: object) -> dict[int, Resource]:
    """Year → zipped JSON resource, selected by exact title (API order is not stable)."""
    if not isinstance(payload, dict):
        raise BaseContractsError("Resposta do dados.gov.pt inesperada.")
    items = payload.get("resources")
    if not isinstance(items, list):
        raise BaseContractsError("Resposta do dados.gov.pt sem recursos.")
    found: dict[int, Resource] = {}
    for item in items:
        if not isinstance(item, dict):
            raise BaseContractsError("Recurso do dados.gov.pt inesperado.")
        title = item.get("title")
        match = RESOURCE_TITLE.fullmatch(title) if isinstance(title, str) else None
        if match is None:
            continue
        year = int(match["year"])
        url, size = item.get("url"), item.get("filesize")
        path = RESOURCE_PATH.fullmatch(urlsplit(url).path) if isinstance(url, str) else None
        if (
            not isinstance(url, str)
            or not _resource_allowed(url)
            or path is None
            or int(path["year"]) != year
            or type(size) is not int
            or not 0 < size <= ARCHIVE_BYTES
            or year in found
        ):
            raise BaseContractsError("Recurso anual BASE inesperado.")
        found[year] = Resource(year=year, url=url, size=size)
    if not found:
        raise BaseContractsError("Nenhum ficheiro anual BASE encontrado.")
    return found


def fetch_resources() -> dict[int, Resource]:
    try:
        content = download(
            API_URL,
            allowed=_api_allowed,
            max_bytes=API_BYTES,
            deadline=time.monotonic() + API_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        payload = json.loads(content)
    except OfficialHTTPError as exc:
        raise BaseContractsError("Não foi possível consultar o dados.gov.pt em segurança.") from exc
    except (ValueError, RecursionError) as exc:
        raise BaseContractsError("JSON do dados.gov.pt inválido.") from exc
    return parse_resources(payload)


def fetch_archive(resource: Resource) -> bytes:
    try:
        content = download(
            resource.url,
            allowed=_resource_allowed,
            max_bytes=ARCHIVE_BYTES,
            deadline=time.monotonic() + ARCHIVE_TIMEOUT,
            headers={"Accept": "application/zip"},
        )
    except OfficialHTTPError as exc:
        raise BaseContractsError("Não foi possível obter o ficheiro BASE em segurança.") from exc
    if len(content) != resource.size:
        raise BaseContractsError("Ficheiro BASE com tamanho inesperado.")
    return content


# Application -----------------------------------------------------------------------


def known_organisations(nipcs: list[str]) -> int:
    """Read-only: how many NIPCs already identify an entity."""
    return sum(
        SourceIdentity.objects.filter(source=NIPC, external_id__in=chunk).count()
        for chunk in (nipcs[start : start + 5000] for start in range(0, len(nipcs), 5000))
    )


def apply_year(archive: bytes, year_scan: YearScan, *, as_of: date) -> dict[str, int]:
    """Atomic per year: create missing organisations, then sync the complete year scope."""
    with import_transaction():
        rows = year_scan.organisations
        known = known_organisations(list(rows))
        entities = official_entities_bulk(NIPC, rows)
        result = sync_events(
            dataset=DATASET_KEY,
            scope=str(year_scan.year),
            events=(
                event_input(contract, entities) for contract in contracts(archive, year_scan.year)
            ),
            as_of=as_of,
            complete=True,
        )
        result["organisations_created"] = len(rows) - known
        return result
