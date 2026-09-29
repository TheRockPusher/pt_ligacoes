"""IGF lists of public subsidies, benefits and donations (Lei n.º 64/2013), as events.

Each yearly dados.gov.pt dataset carries two ODS files: the subsidies/benefits list
(art. 4.º) and the donations list (art. 6.º). Every row names a granting public entity
and a beneficiary by NIF. Rows whose beneficiary NIF is not a legal-person NIPC are
natural persons (or unverifiable foreign ids) and are dropped while parsing: their
name, NIF and amount never leave the parser.
"""

import hashlib
import json
import re
import time
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import cast
from xml.etree.ElementTree import Element, ParseError, iterparse

from .catalogue import DATASETS
from .events import EventInput, PartyInput, sync_events
from .identity import official_entities_bulk, valid_nipc
from .models import Entity, Event, EventParty, IdentityScheme, import_transaction
from .official_http import OfficialHTTPError, download
from .parliament_parse import JSONObject, JSONValue

DATASET = DATASETS["igf_subvencoes"].key
FIRST_YEAR = 2020
API_ROOT = "https://dados.gov.pt/api/1/datasets/"
SLUG = "listas-de-subvencoes-e-beneficios-publicos-e-de-doacoes-ano-{year}"
RESOURCE_URL = re.compile(
    r"https://dados\.gov\.pt/s/resources/[A-Za-z0-9._-]+/[0-9]{8}-[0-9]{6}(?:-[0-9a-f]+)?/"
    r"(?P<file>[A-Za-z0-9._-]+\.ods)"
)
API_BYTES = 2 * 1024 * 1024
ODS_BYTES = 96 * 1024 * 1024
# content.xml of the largest list (2020) inflates to a few hundred MB; refuse bombs.
CONTENT_BYTES = 3 * 1024 * 1024 * 1024
MAX_ROWS = 2_000_000
TOTAL_TIMEOUT = 900
BATCH = 5000

SUBSIDIES = "subsidies"
DONATIONS = "donations"
CATEGORY_LABELS = {SUBSIDIES: "Subvenção ou benefício público", DONATIONS: "Doação"}
AMOUNT_LABELS = {SUBSIDIES: "Montante", DONATIONS: "Valor patrimonial estimado"}
SCOPE_SUFFIX = {SUBSIDIES: "", DONATIONS: "-doacoes"}

NIPC = IdentityScheme.NIPC
GRANTOR = EventParty.Role.GRANTOR
BENEFICIARY = EventParty.Role.BENEFICIARY
_KIND = Entity.Kind
_CLASS = Entity.Classification

# Columns (0-based): NIF (EO/EP), entity, NIF (B), beneficiary, amount, decision date,
# purpose, legal basis type / number / date.
COLUMNS = 10
NAME_LIMIT = 300  # EventParty.name
ENTITY_NAME_LIMIT = 240  # Entity.name
TITLE_LIMIT = 500
DETAIL_LIMIT = 120

TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
ROW = f"{TABLE}table-row"
CELLS = frozenset({f"{TABLE}table-cell", f"{TABLE}covered-table-cell"})
NUMERIC_TYPES = frozenset({"float", "currency", "percentage"})
TEXT_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class IgfError(ValueError):
    """Safe, payload-free failure for an unexpected IGF file or response."""


class NotPublished(IgfError):
    """The yearly dataset does not exist (yet) on dados.gov.pt."""


@dataclass(frozen=True)
class Resource:
    category: str
    url: str
    filename: str


@dataclass(frozen=True)
class Grant:
    """A retained row: only legal-person beneficiaries reach this type."""

    grantor_nipc: str
    grantor_name: str
    beneficiary_nipc: str
    beneficiary_name: str
    amount: Decimal | None
    decided_on: date | None
    purpose: str
    basis_type: str
    basis_number: str
    basis_date: date | None


# ODS streaming ---------------------------------------------------------------------


def _text(element: Element) -> str:
    parts = [element.text or ""]
    for child in element:
        if child.tag == f"{TEXT}s":
            parts.append(" " * int(child.get(f"{TEXT}c", "1") or "1"))
        elif child.tag in {f"{TEXT}tab", f"{TEXT}line-break"}:
            parts.append(" ")
        else:
            parts.append(_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _cell(element: Element) -> str:
    kind = element.get(f"{OFFICE}value-type", "")
    if kind in NUMERIC_TYPES:
        return element.get(f"{OFFICE}value", "")
    if kind == "date":
        return element.get(f"{OFFICE}date-value", "")
    return "\n".join(_text(p) for p in element.iter(f"{TEXT}p"))


def _repeat(element: Element, attribute: str) -> int:
    value = element.get(f"{TABLE}{attribute}", "1")
    if not value.isdecimal() or int(value) < 1:
        raise IgfError("Folha ODS inválida.")
    return int(value)


def ods_rows(content: bytes) -> Iterator[tuple[int, tuple[str, ...]]]:
    """(1-based row number, first ``COLUMNS`` cell values) of the first sheet.

    Streams ``content.xml``; repeated rows/cells are expanded, empty rows skipped.
    """
    try:
        archive = zipfile.ZipFile(BytesIO(content))
        info = archive.getinfo("content.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise IgfError("Ficheiro ODS inválido.") from exc
    if info.file_size > CONTENT_BYTES:
        raise IgfError("Ficheiro ODS demasiado extenso.")
    number = 0
    stack: list[Element] = []
    tables = 0
    try:
        with archive, archive.open(info) as stream:
            # stdlib expat ≥2.4 limits entity expansion; no external entities are resolved.
            for event, element in iterparse(stream, events=("start", "end")):  # noqa: S314
                if event == "start":
                    stack.append(element)
                    if element.tag == f"{TABLE}table":
                        tables += 1
                    continue
                stack.pop()
                if element.tag == f"{TABLE}table":
                    return
                if element.tag != ROW or tables != 1:
                    continue
                values: list[str] = []
                for cell in element:
                    if cell.tag not in CELLS or len(values) >= COLUMNS:
                        continue
                    value = _cell(cell).strip()
                    count = min(_repeat(cell, "number-columns-repeated"), COLUMNS - len(values))
                    values.extend([value] * count)
                repeated = _repeat(element, "number-rows-repeated")
                if stack:
                    stack[-1].remove(element)
                if any(values):
                    row = tuple(values + [""] * (COLUMNS - len(values)))
                    for offset in range(repeated):
                        yield number + offset + 1, row
                number += repeated
                if number > MAX_ROWS and any(values):
                    raise IgfError("Folha ODS com demasiadas linhas.")
    except (ParseError, zipfile.BadZipFile, EOFError, OSError) as exc:
        raise IgfError("Conteúdo ODS inválido.") from exc
    if tables == 0:
        raise IgfError("Ficheiro ODS sem folha de cálculo.")


# Values ----------------------------------------------------------------------------


def _clean(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit].strip()


def nif(value: str) -> str:
    """Digits of a NIF cell: float cells (``500051054`` / ``500051054.0``) or text."""
    text = value.strip().replace(" ", "")
    if not text:
        return ""
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE]\+?[0-9]{1,2})?", text):
        try:
            number = Decimal(text)
        except InvalidOperation:
            return ""
        if number != number.to_integral_value() or number < 0:
            return ""
        return str(int(number)).zfill(9)
    return text


def amount(value: str) -> Decimal | None:
    """Euros from an ODS numeric value, or pt-PT text (``1.234,56``) as a fallback."""
    text = value.strip().replace("€", "").replace(" ", "").replace("\xa0", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return number.quantize(Decimal("0.01"))


def day(value: str) -> date | None:
    """ODS date values (``2025-07-21T00:00:00``) or text ``dd/mm/yyyy`` (2021 lists)."""
    text = value.strip()
    try:
        if match := ISO_DATE.match(text):
            return date(int(match[1]), int(match[2]), int(match[3]))
        if match := TEXT_DATE.fullmatch(text):
            return date(int(match[3]), int(match[2]), int(match[1]))
    except ValueError:
        return None
    return None


def _number(value: str) -> str:
    """Legal act number; numeric cells lose the float suffix (``64.0`` → ``64``)."""
    text = _clean(value, DETAIL_LIMIT)
    if re.fullmatch(r"[0-9]+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _folded(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def classify(name: str, nipc: str, *, grantor: bool) -> tuple[str, str]:
    """(kind, classification) for a new organisation; set only at creation."""
    folded = _folded(name)
    if re.search(r"\b(municipio|camara municipal)\b", folded):
        return _KIND.ORGANISATION, _CLASS.MUNICIPALITY
    if re.search(r"\bfreguesia\b", folded):
        return _KIND.ORGANISATION, _CLASS.PARISH
    if re.search(r"\b(universidade|instituto politecnico)\b", folded):
        return _KIND.UNIVERSITY, _CLASS.HIGHER_EDUCATION
    if re.search(r"\bfundacao\b", folded):
        return _KIND.ORGANISATION, _CLASS.FOUNDATION
    if re.search(r"\bassociacao\b", folded):
        return _KIND.ORGANISATION, _CLASS.ASSOCIATION
    if re.search(r"\bcooperativa\b|\bc\.? ?r\.? ?l\.?(?:\s|$)", folded):
        return _KIND.ORGANISATION, _CLASS.COOPERATIVE
    # Prefix 6 NIPCs are public administration bodies.
    if grantor or nipc.startswith("6"):
        return _KIND.ORGANISATION, _CLASS.PUBLIC_BODY
    return _KIND.COMPANY, _CLASS.COMPANY


# Rows ------------------------------------------------------------------------------


def _header(row: tuple[str, ...]) -> bool:
    return _clean(row[0], 40).upper().startswith("NIF (")


def _check_header(row: tuple[str, ...]) -> None:
    labels = [_clean(value, 200).upper() for value in row]
    if not (
        labels[2].startswith("NIF (B")
        and labels[3].startswith("BENEFICI")
        and ("MONTANTE" in labels[4] or "VALOR" in labels[4])
        and labels[5].startswith("DATA")
        and labels[6].startswith("FINALIDADE")
    ):
        raise IgfError("Cabeçalho IGF inesperado.")


def grants(content: bytes, stats: Counter[str]) -> Iterator[Grant]:
    """Retained rows of one list; natural persons are counted, never returned."""
    header = False
    for _, row in ods_rows(content):
        if not header:
            if _header(row):
                _check_header(row)
                header = True
            continue
        if "TIPO DE ATO" in _clean(row[7], 40).upper() or _header(row):
            continue
        stats["rows"] += 1
        beneficiary = nif(row[2])
        if not valid_nipc(beneficiary):
            stats["dropped_persons"] += 1
            continue
        grantor = nif(row[0])
        if not valid_nipc(grantor):
            grantor = ""
            stats["grantor_unanchored"] += 1
        value = amount(row[4])
        if value is None and row[4].strip():
            stats["invalid_amount"] += 1
        decided_on = day(row[5])
        if decided_on is None:
            stats["no_date"] += 1
        stats["kept"] += 1
        yield Grant(
            grantor_nipc=grantor,
            grantor_name=_clean(row[1], NAME_LIMIT) if grantor else "",
            beneficiary_nipc=beneficiary,
            beneficiary_name=_clean(row[3], NAME_LIMIT) or f"NIPC {beneficiary}",
            amount=value,
            decided_on=decided_on,
            purpose=_clean(row[6], TITLE_LIMIT),
            basis_type=_clean(row[7], DETAIL_LIMIT),
            basis_number=_number(row[8]),
            basis_date=day(row[9]),
        )
    if not header:
        raise IgfError("Cabeçalho IGF em falta.")


def record_id(grant: Grant, *, year: int, category: str, occurrence: int) -> str:
    """Content hash; ``occurrence`` numbers identical rows so inserts do not shift ids."""
    payload = json.dumps(
        [
            year,
            category,
            grant.grantor_nipc,
            grant.beneficiary_nipc,
            grant.decided_on.isoformat() if grant.decided_on else "",
            str(grant.amount) if grant.amount is not None else "",
            grant.purpose,
            occurrence,
        ],
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:40]
    return f"{year}-{'doacao' if category == DONATIONS else 'subvencao'}-{digest}"


def scope(year: int, category: str) -> str:
    return f"{year}{SCOPE_SUFFIX[category]}"


def _numbered(items: Iterator[Grant]) -> Iterator[tuple[Grant, int]]:
    """Each grant with its 1-based occurrence among identical rows of the file."""
    seen: Counter[tuple[object, ...]] = Counter()
    for grant in items:
        key = (
            grant.grantor_nipc,
            grant.beneficiary_nipc,
            grant.decided_on,
            grant.amount,
            grant.purpose,
        )
        seen[key] += 1
        yield grant, seen[key]


def event(
    grant: Grant, *, year: int, category: str, entities: dict[str, Entity], occurrence: int
) -> EventInput:
    details: dict[str, str] = {"category": CATEGORY_LABELS[category]}
    if grant.basis_type:
        details["legal_basis_type"] = grant.basis_type
    if grant.basis_number:
        details["legal_basis_number"] = grant.basis_number
    if grant.basis_date is not None:
        details["legal_basis_date"] = grant.basis_date.isoformat()
    parties = [
        PartyInput(
            role=BENEFICIARY,
            name=grant.beneficiary_name,
            entity=entities.get(grant.beneficiary_nipc),
            identifier=f"nipc:{grant.beneficiary_nipc}",
        )
    ]
    if grant.grantor_nipc:
        parties.append(
            PartyInput(
                role=GRANTOR,
                name=grant.grantor_name or f"NIPC {grant.grantor_nipc}",
                entity=entities.get(grant.grantor_nipc),
                identifier=f"nipc:{grant.grantor_nipc}",
            )
        )
    return EventInput(
        record_id=record_id(grant, year=year, category=category, occurrence=occurrence),
        kind=Event.Kind.SUBSIDY,
        title=grant.purpose or CATEGORY_LABELS[category],
        date=grant.decided_on,
        amount=grant.amount,
        amount_label=AMOUNT_LABELS[category],
        details=details,
        parties=tuple(parties),
    )


def organisations(content: bytes, stats: Counter[str]) -> dict[str, tuple[str, str, str]]:
    """NIPC → (name, kind, classification) for creation; grantor wording wins."""
    rows: dict[str, tuple[str, str, str]] = {}
    grantors: set[str] = set()
    for grant in grants(content, stats):
        if grant.grantor_nipc and grant.grantor_nipc not in grantors:
            grantors.add(grant.grantor_nipc)
            name = grant.grantor_name or f"NIPC {grant.grantor_nipc}"
            rows[grant.grantor_nipc] = (
                name[:ENTITY_NAME_LIMIT].strip(),
                *classify(name, grant.grantor_nipc, grantor=True),
            )
        if grant.beneficiary_nipc not in rows:
            name = grant.beneficiary_name
            rows[grant.beneficiary_nipc] = (
                name[:ENTITY_NAME_LIMIT].strip(),
                *classify(name, grant.beneficiary_nipc, grantor=False),
            )
    return rows


def summarise(content: bytes) -> dict[str, int]:
    """Read-only counts of one list; no payloads or personal data."""
    stats: Counter[str] = Counter()
    rows = organisations(content, stats)
    return {
        "rows": stats["rows"],
        "kept": stats["kept"],
        "dropped_persons": stats["dropped_persons"],
        "grantor_unanchored": stats["grantor_unanchored"],
        "invalid_amount": stats["invalid_amount"],
        "no_date": stats["no_date"],
        "organisations": len(rows),
    }


def apply_file(content: bytes, *, year: int, category: str, as_of: date) -> dict[str, int]:
    """Atomic: NIPC organisations, then the complete list as one events scope."""
    with import_transaction():
        stats: Counter[str] = Counter()
        entities = official_entities_bulk(NIPC, organisations(content, stats))
        result = sync_events(
            dataset=DATASET,
            scope=scope(year, category),
            events=(
                event(grant, year=year, category=category, entities=entities, occurrence=seen)
                for grant, seen in _numbered(grants(content, Counter()))
            ),
            as_of=as_of,
            complete=True,
            batch_size=BATCH,
        )
    return {**result, "dropped_persons": stats["dropped_persons"], "kept": stats["kept"]}


# Network ---------------------------------------------------------------------------


def _api_url(year: int) -> str:
    return f"{API_ROOT}{SLUG.format(year=year)}/"


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise IgfError("Resposta dados.gov.pt inesperada.")
    return value


def parse_resources(payload: JSONValue) -> list[Resource]:
    """The subsidies list and (if published) the donations list, chosen by file name."""
    items = _object(payload).get("resources")
    if not isinstance(items, list):
        raise IgfError("Resposta dados.gov.pt inesperada.")
    found: dict[str, Resource] = {}
    for item in items:
        url = _object(item).get("url")
        if not isinstance(url, str) or not (match := RESOURCE_URL.fullmatch(url)):
            continue
        filename = match["file"]
        folded = filename.casefold()
        category = SUBSIDIES if "subv" in folded else DONATIONS if "doac" in folded else None
        if category is None:
            continue
        if category in found:
            raise IgfError("Mais do que uma lista IGF do mesmo tipo no conjunto de dados.")
        found[category] = Resource(category, url, filename)
    if SUBSIDIES not in found:
        raise IgfError("Lista de subvenções IGF em falta no conjunto de dados.")
    return [found[key] for key in (SUBSIDIES, DONATIONS) if key in found]


class Client:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + TOTAL_TIMEOUT

    def resources(self, year: int) -> list[Resource]:
        url = _api_url(year)
        try:
            content = download(
                url,
                allowed=lambda target: target == url,
                max_bytes=API_BYTES,
                deadline=self.deadline,
                headers={"Accept": "application/json"},
            )
        except OfficialHTTPError as exc:
            if "HTTP 404" in str(exc):
                raise NotPublished("Lista IGF do ano ainda não publicada.") from exc
            raise IgfError(str(exc)) from exc
        try:
            payload = cast(JSONValue, json.loads(content))
        except (ValueError, RecursionError) as exc:
            raise IgfError("JSON dados.gov.pt inválido.") from exc
        return parse_resources(payload)

    def content(self, resource: Resource) -> bytes:
        try:
            return download(
                resource.url,
                allowed=lambda target: target == resource.url,
                max_bytes=ODS_BYTES,
                deadline=time.monotonic() + TOTAL_TIMEOUT,
            )
        except OfficialHTTPError as exc:
            raise IgfError(str(exc)) from exc


def years(value: str, *, as_of: date) -> list[int]:
    """``all`` = every published year (lists appear in the following year)."""
    last = as_of.year - 1
    if value == "all":
        return list(range(FIRST_YEAR, last + 1))
    if not value.isdecimal() or not FIRST_YEAR <= int(value) <= last:
        raise IgfError(f"Ano inválido: indique {FIRST_YEAR}\u2013{last} ou «all».")
    return [int(value)]
