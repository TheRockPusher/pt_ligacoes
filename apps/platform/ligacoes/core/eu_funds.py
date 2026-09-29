"""EU-fund operations from dados.gov.pt as events on beneficiary organisations.

Each programme is one complete snapshot of two XLSX files joined by operation code:

- Portugal 2020 (AD&C, CC BY 4.0): approved operations and their beneficiaries, principal
  and consortium partners (dataset ``pt2020_beneficiarios``, scope ``pt2020``).
- Portugal 2030 (AD&C): operations and their entities, principal and other beneficiaries
  (``pt2030_operacoes``, scope ``pt2030``).
- PRR (Estrutura de Missão Recuperar Portugal, daily): projects and their entities, direct,
  final and intermediary beneficiaries and suppliers (``prr_entidades``, scope ``prr``).

Resource URLs change on every upload and are resolved through the dados.gov.pt API on
each run. Only legal-person NIPCs anchor parties: pseudonymised natural persons (PT2020
``AA999999``, PT2030 ``A99999999``, PRR 8-digit codes named ``RGPD``), natural-person and
foreign numbers are dropped with their names while parsing, and an operation left without
a legal-person party is skipped. A kept operation that also named such a person gets a
neutral title (code only): applicants' free-text titles can describe the person (names,
voucher numbers). The managing programme has no NIPC and stays in the event details.
Sheets are streamed from the XLSX zip with the standard library; the parties file is
reduced to a legal-person index and released before the operations file is fetched.
"""

import json
import posixpath
import re
import sys
import time
import unicodedata
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from itertools import batched
from typing import cast
from xml.etree.ElementTree import Element, ParseError, fromstring, iterparse

from .catalogue import DATASETS
from .events import EventInput, PartyInput, sync_events
from .identity import official_entities_bulk, valid_nipc
from .models import Entity, Event, EventParty, IdentityScheme, SourceIdentity, import_transaction
from .official_http import OfficialHTTPError, download
from .parliament_parse import JSONObject, JSONValue

API_ROOT = "https://dados.gov.pt/api/1/datasets/"
RESOURCE_URL = re.compile(
    r"https://dados\.gov\.pt/s/resources/[A-Za-z0-9._-]+/[0-9]{8}-[0-9]{6}(?:-[0-9a-f]+)?/"
    r"[A-Za-z0-9._-]+\.xlsx"
)
API_BYTES = 2 * 1024 * 1024
XLSX_BYTES = 160 * 1024 * 1024
# Decompressed parts; the largest sheet (PRR entities) inflates to ~325 MB.
PART_BYTES = 8 * 1024 * 1024
SHARED_BYTES = 512 * 1024 * 1024
SHEET_BYTES = 3 * 1024 * 1024 * 1024
MAX_ROWS = 3_000_000
MAX_COLUMNS = 256
API_TIMEOUT = 60
TOTAL_TIMEOUT = 900
BATCH = 5000

NAME_LIMIT = 240
TITLE_LIMIT = 300
DETAIL_LIMIT = 200
EXCEL_EPOCH = date(1899, 12, 30)
MAX_SERIAL = 2958465  # 9999-12-31
CENT = Decimal("0.01")
SERIAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
ISO_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ][0-9:.]+)?")
TEXT_DATE = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})")
COLUMN = re.compile(r"([A-Z]{1,3})[0-9]*")

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOCUMENT_RELATIONSHIPS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE_RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
SHEET_DATA = f"{MAIN}sheetData"
ROW = f"{MAIN}row"
CELL = f"{MAIN}c"
STRING_ITEM = f"{MAIN}si"
TEXT = f"{MAIN}t"
RUN = f"{MAIN}r"

NIPC = IdentityScheme.NIPC
EU_FUNDING = Event.Kind.EU_FUNDING
# Internal party roles; ``lead`` is the principal beneficiary (role beneficiary).
LEAD = "lead"
BENEFICIARY = str(EventParty.Role.BENEFICIARY)
INTERMEDIARY = str(EventParty.Role.INTERMEDIARY)
SUPPLIER = str(EventParty.Role.SUPPLIER)
PUBLIC_ROLE = {
    LEAD: BENEFICIARY,
    BENEFICIARY: BENEFICIARY,
    INTERMEDIARY: INTERMEDIARY,
    SUPPLIER: SUPPLIER,
}
_KIND = Entity.Kind
_CLASS = Entity.Classification


class EuFundsError(ValueError):
    """Safe, payload-free failure for an unexpected dados.gov.pt response or file."""


# Spreadsheets ----------------------------------------------------------------------


def _limited(archive: zipfile.ZipFile, name: str, limit: int) -> zipfile.ZipInfo:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise EuFundsError("Parte XLSX em falta.") from exc
    if info.file_size > limit:
        raise EuFundsError("Parte XLSX demasiado extensa.")
    return info


def _member(target: str) -> str:
    """Zip member of a workbook relationship target (absolute or relative to ``xl/``)."""
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(posixpath.join("xl", target))


# Official workbook parts. The stdlib expat parser (>= 2.4) bounds entity expansion and
# never fetches external entities; defusedxml is not a project dependency.
def _document(data: bytes) -> Element:
    return fromstring(data)  # noqa: S314


def _parts(archive: zipfile.ZipFile) -> tuple[str, str | None]:
    """(first worksheet, shared strings or None) from the workbook relationships."""
    workbook = _document(archive.read(_limited(archive, "xl/workbook.xml", PART_BYTES)))
    sheet = workbook.find(f"{MAIN}sheets/{MAIN}sheet")
    relation = sheet.get(f"{DOCUMENT_RELATIONSHIPS}id") if sheet is not None else None
    if not relation:
        raise EuFundsError("Livro XLSX sem folha.")
    links = _document(archive.read(_limited(archive, "xl/_rels/workbook.xml.rels", PART_BYTES)))
    worksheet: str | None = None
    shared: str | None = None
    for link in links.iter(f"{PACKAGE_RELATIONSHIPS}Relationship"):
        target = _member(link.get("Target", ""))
        if link.get("Id") == relation:
            worksheet = target
        elif link.get("Type", "").endswith("/sharedStrings"):
            shared = target
    if worksheet is None:
        raise EuFundsError("Livro XLSX sem folha.")
    return worksheet, shared


def _rich(element: Element) -> str:
    """Plain text of a string item: direct text and rich-text runs, not phonetic hints."""
    parts: list[str] = []
    for child in element:
        if child.tag == TEXT:
            parts.append(child.text or "")
        elif child.tag == RUN:
            parts.extend(node.text or "" for node in child.iter(TEXT))
    return "".join(parts)


def _shared(archive: zipfile.ZipFile, name: str) -> list[str]:
    strings: list[str] = []
    with archive.open(_limited(archive, name, SHARED_BYTES)) as stream:
        root: Element | None = None
        for phase, element in iterparse(stream, events=("start", "end")):  # noqa: S314
            if root is None:
                root = element
            elif phase == "end" and element.tag == STRING_ITEM:
                strings.append(_rich(element))
                root.clear()
    return strings


def _column(reference: str) -> int:
    match = COLUMN.match(reference)
    if match is None:
        raise EuFundsError("Referência de célula XLSX inválida.")
    number = 0
    for letter in match[1]:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number - 1


def _value(cell: Element, shared: list[str]) -> str:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        node = cell.find(f"{MAIN}is")
        return _rich(node) if node is not None else ""
    raw = cell.findtext(f"{MAIN}v") or ""
    if kind == "s":
        if not raw.isdecimal() or int(raw) >= len(shared):
            raise EuFundsError("Referência de texto XLSX inválida.")
        return shared[int(raw)]
    if kind == "e":
        return ""
    return raw


def _row(element: Element, shared: list[str]) -> list[str]:
    values: list[str] = []
    for cell in element.iterfind(CELL):
        reference = cell.get("r")
        index = _column(reference) if reference else len(values)
        if index < len(values):
            raise EuFundsError("Células XLSX fora de ordem.")
        if index >= MAX_COLUMNS:
            continue
        values.extend([""] * (index - len(values)))
        values.append(_value(cell, shared))
    return values


def xlsx_rows(content: bytes) -> Iterator[list[str]]:
    """Cell values (as stored) of the first worksheet, one list per row, streamed."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            worksheet, shared_part = _parts(archive)
            shared = _shared(archive, shared_part) if shared_part else []
            with archive.open(_limited(archive, worksheet, SHEET_BYTES)) as stream:
                data: Element | None = None
                count = 0
                for phase, element in iterparse(stream, events=("start", "end")):  # noqa: S314
                    if phase == "start":
                        if element.tag == SHEET_DATA:
                            data = element
                        continue
                    if element.tag != ROW:
                        continue
                    values = _row(element, shared)
                    if data is not None:
                        # Finished rows are dropped so memory stays bounded.
                        data.clear()
                    count += 1
                    if count > MAX_ROWS:
                        raise EuFundsError("Folha XLSX com demasiadas linhas.")
                    yield values
    except (zipfile.BadZipFile, ParseError, zlib.error, EOFError, OSError) as exc:
        raise EuFundsError("Ficheiro XLSX ilegível.") from exc


def _label(value: str) -> str:
    return " ".join(value.split()).casefold()


def records(content: bytes, columns: dict[str, str]) -> Iterator[dict[str, str]]:
    """Rows as ``{field: value}`` for the wanted header columns only; blank rows skipped."""
    rows = xlsx_rows(content)
    header = next(rows, None)
    if header is None:
        raise EuFundsError("Folha XLSX vazia.")
    positions: dict[str, int] = {}
    for index, value in enumerate(header):
        positions.setdefault(_label(value), index)
    if any(_label(name) not in positions for name in columns.values()):
        raise EuFundsError("Colunas inesperadas no ficheiro de fundos europeus.")
    wanted = {key: positions[_label(name)] for key, name in columns.items()}
    for row in rows:
        if not any(value.strip() for value in row):
            continue
        yield {key: row[index] if index < len(row) else "" for key, index in wanted.items()}


# Values ----------------------------------------------------------------------------


def clean(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def nif(value: str) -> str:
    """A tax number as published: padding removed; numeric cells lose a ``.0`` suffix."""
    text = "".join(value.split()).upper()
    if re.fullmatch(r"[0-9]+\.0+", text):
        return text.split(".", 1)[0]
    return text


def amount(value: str, stats: Counter[str]) -> Decimal | None:
    """Euros from a numeric cell (``.`` decimal, float noise rounded to the cent)."""
    text = value.strip().replace("€", "").replace(" ", "").replace("\xa0", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation:
        number = Decimal("NaN")
    if not number.is_finite() or number < 0:
        stats["invalid_amounts"] += 1
        return None
    return number.quantize(CENT, rounding=ROUND_HALF_UP)


def day(value: str, stats: Counter[str]) -> date | None:
    """Excel serial dates (days since 1899-12-30), ISO text or ``dd/mm/yyyy`` text."""
    text = value.strip()
    if not text:
        return None
    try:
        if SERIAL.fullmatch(text):
            serial = int(Decimal(text))
            if 1 <= serial <= MAX_SERIAL:
                return EXCEL_EPOCH + timedelta(days=serial)
        elif match := ISO_DATE.fullmatch(text):
            return date(int(match[1]), int(match[2]), int(match[3]))
        elif match := TEXT_DATE.fullmatch(text):
            return date(int(match[3]), int(match[2]), int(match[1]))
    except (ValueError, OverflowError):
        pass
    stats["invalid_dates"] += 1
    return None


def _folded(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def classify(name: str, nipc: str) -> tuple[str, str]:
    """(kind, classification) for a new beneficiary, intermediary or supplier."""
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
    # Prefix 6 NIPCs are public administration bodies (schools, institutes, services).
    if nipc.startswith("6"):
        return _KIND.ORGANISATION, _CLASS.PUBLIC_BODY
    return _KIND.COMPANY, _CLASS.COMPANY


# Programmes ------------------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    code: str
    title: str
    start: date | None
    end: date | None
    amount: Decimal | None
    details: dict[str, str]
    # Principal beneficiary as given in the operations sheet: (tax number, name).
    lead: tuple[str, str] | None


type Builder = Callable[[dict[str, str], Counter[str]], Operation]


@dataclass(frozen=True)
class Sheet:
    slug: str
    # Matched against the dados.gov.pt resource title (equal to the file name for PT2030/PRR).
    title: re.Pattern[str]
    columns: dict[str, str]


@dataclass(frozen=True)
class Programme:
    key: str
    label: str
    dataset: str
    amount_label: str
    # Record noun for neutral titles ("operação", "projeto").
    record: str
    parties: Sheet
    operations: Sheet
    # Casefolded role value of the parties sheet → internal role.
    roles: dict[str, str]
    build: Builder


def _details(**values: str) -> dict[str, str]:
    return {key: clean(value, DETAIL_LIMIT) for key, value in values.items() if value.strip()}


def _money(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.2f}"


# A single published date, or an (actual, planned) pair of which the actual one wins.
type Dates = str | tuple[str, str]


def _date(details: dict[str, str], key: str, value: Dates, stats: Counter[str]) -> date | None:
    if isinstance(value, str):
        return day(value, stats)
    actual, planned = value
    chosen, basis = day(actual, stats), "efetiva"
    if chosen is None:
        chosen, basis = day(planned, stats), "prevista"
    if chosen is not None:
        details[key] = basis
    return chosen


def _dated(
    details: dict[str, str], stats: Counter[str], *, start: Dates = "", end: Dates = ""
) -> tuple[date | None, date | None]:
    """Execution period; the basis of actual/planned pairs is noted in the details.

    An end before the start (a stale planned end after a late actual start) is dropped.
    """
    first = _date(details, "start_basis", start, stats)
    last = _date(details, "end_basis", end, stats)
    if first is not None and last is not None and last < first:
        stats["end_dates_dropped"] += 1
        details.pop("end_basis", None)
        last = None
    return first, last


def _pt2020(row: dict[str, str], stats: Counter[str]) -> Operation:
    value = amount(row["approved"], stats)
    details = _details(
        programme=row["programme"],
        fund=row["fund"],
        thematic_domain=row["domain"],
        investment_nature=row["nature"],
        status=row["status"],
        executed=_money(amount(row["executed"], stats)),
        framework=row["framework"],
    )
    start, end = _dated(details, stats, end=(row["actual_end"], row["planned_end"]))
    return Operation(
        code=row["code"].strip(),
        title=clean(row["title"], TITLE_LIMIT),
        start=start,
        end=end,
        amount=value,
        details=details,
        lead=(row["lead_nif"], row["lead_name"]),
    )


def _pt2030(row: dict[str, str], stats: Counter[str]) -> Operation:
    value = amount(row["approved"], stats)
    details = _details(
        programme=row["programme"],
        fund=row["fund"],
        objective=row["objective"],
        status=row["status"],
        eligible_cost=_money(amount(row["eligible"], stats)),
        executed=_money(amount(row["executed"], stats)),
        paid=_money(amount(row["paid"], stats)),
    )
    start, end = _dated(
        details,
        stats,
        start=(row["actual_start"], row["planned_start"]),
        end=(row["actual_end"], row["planned_end"]),
    )
    return Operation(
        code=row["code"].strip(),
        title=clean(row["title"], TITLE_LIMIT),
        start=start,
        end=end,
        amount=value,
        details=details,
        lead=(row["lead_nif"], row["lead_name"]),
    )


def _prr(row: dict[str, str], stats: Counter[str]) -> Operation:
    value = amount(row["approved"], stats)
    details = _details(
        programme="Plano de Recuperação e Resiliência",
        investment=row["investment"],
        paid=_money(amount(row["paid"], stats)),
        grants=_money(amount(row["grants"], stats)),
        loans=_money(amount(row["loans"], stats)),
    )
    start, end = _dated(
        details, stats, start=row["start"], end=(row["actual_end"], row["planned_end"])
    )
    return Operation(
        code=row["code"].strip(),
        title=clean(row["title"], TITLE_LIMIT),
        start=start,
        end=end,
        amount=value,
        details=details,
        lead=None,
    )


PROGRAMMES: dict[str, Programme] = {
    "pt2020": Programme(
        key="pt2020",
        label="Portugal 2020",
        dataset=DATASETS["pt2020_beneficiarios"].key,
        amount_label="Apoio aprovado",
        record="operação",
        parties=Sheet(
            slug="datasets-do-portugal-2020",
            title=re.compile(r"Lista de Benefici[aá]rios do Portugal 2020"),
            columns={
                "operation": "Código da Operação",
                "nif": "NIF das Entidades Beneficiárias",
                "name": "Designação da Entidade",
                "role": "Principal",
            },
        ),
        operations=Sheet(
            slug="datasets-do-portugal-2020",
            title=re.compile(r"Lista de Projetos Aprovados do Portugal 2020"),
            columns={
                "code": "Código da Operação",
                "title": "Designação da Operação",
                "programme": "Designação do Programa Operacional da Operação",
                "fund": "Sigla do Fundo da Operação",
                "domain": "Designação Domínio Temático da Operação",
                "status": "Estado da Operação",
                "nature": "Natureza do Investimento da Operação",
                "lead_nif": "NIF Beneficiário Principal da Operação",
                "lead_name": "Beneficiário Principal da Operação",
                "approved": "Apoio Total Aprovado - Em Vigor",
                "executed": "Apoio Executado",
                "planned_end": "Data Prevista Conclusão",
                "actual_end": "Data Efetiva Conclusão da Operação",
                "framework": "Enquadramento",
            },
        ),
        roles={"sim": LEAD, "não": BENEFICIARY, "nao": BENEFICIARY},
        build=_pt2020,
    ),
    "pt2030": Programme(
        key="pt2030",
        label="Portugal 2030",
        dataset=DATASETS["pt2030_operacoes"].key,
        amount_label="Fundo aprovado",
        record="operação",
        parties=Sheet(
            slug="datasets-pt2030-05-lista-de-entidades-pt2030",
            title=re.compile(r"05-datasets-entidades-pt2030-[0-9]{8}\.xlsx"),
            columns={
                "operation": "Código da Operação",
                "nif": "Nif entidade",
                "name": "Designação da entidade",
                "role": "Papel da entidade",
            },
        ),
        operations=Sheet(
            slug="datasets-pt2030-03-lista-de-operacoes-pt2030",
            title=re.compile(r"03-datasets-operacoes-pt2030-[0-9]{8}\.xlsx"),
            columns={
                "code": "Código da Operação",
                "title": "Nome da Operação",
                "programme": "Designação do Programa",
                "objective": "Designação do Objetivo Específico",
                "fund": "Sigla do Fundo",
                "lead_nif": "Nif Beneficiário",
                "lead_name": "Nome do Beneficiário",
                "eligible": "Custo Total Elegível Aprovado",
                "approved": "Fundo Aprovado",
                "executed": "Fundo Executado",
                "paid": "Fundo Pago",
                "status": "Designação do estado da Operação",
                "planned_start": "Data de Inicio Prevista",
                "actual_start": "Data de Inicio Efetiva",
                "planned_end": "Data de Conclusão Prevista",
                "actual_end": "Data de Conclusão Efetiva",
            },
        ),
        roles={"beneficiário principal": LEAD, "outros beneficiários": BENEFICIARY},
        build=_pt2030,
    ),
    "prr": Programme(
        key="prr",
        label="PRR",
        dataset=DATASETS["prr_entidades"].key,
        amount_label="Valor aprovado",
        record="projeto",
        parties=Sheet(
            slug="dataset-estrutura-de-missao-prr-entidades-1",
            title=re.compile(r"listagem-de-entidades-prr-[0-9]{8}\.xlsx"),
            columns={
                "operation": "cd_projeto",
                "nif": "nif_entidade",
                "name": "ds_entidade",
                "role": "papel_entidade",
            },
        ),
        operations=Sheet(
            slug="dataset-estrutura-de-missao-prr-projetos-2",
            title=re.compile(r"listagem-de-projetos-prr-[0-9]{8}\.xlsx"),
            columns={
                "code": "cd_projeto",
                "title": "ds_projeto",
                "approved": "valor_aprovado",
                "paid": "valor_pago",
                "grants": "subvencoes",
                "loans": "emprestimos",
                "investment": "cd_investimento",
                "start": "dt_inicio",
                "planned_end": "dt_prevista_conclusao",
                "actual_end": "dt_efetiva_conclusao",
            },
        ),
        roles={
            "beneficiário direto": BENEFICIARY,
            "beneficiário final": BENEFICIARY,
            "beneficiário intermediário": INTERMEDIARY,
            "fornecedor": SUPPLIER,
        },
        build=_prr,
    ),
}


# Joining ---------------------------------------------------------------------------


@dataclass
class PartyIndex:
    """Legal-person parties per operation code; one published name per NIPC.

    ``withheld`` holds codes of operations that also named a natural person (or an
    unusable number): their free-text titles may describe that person and are replaced.
    """

    by_operation: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    withheld: set[str] = field(default_factory=set)

    def add(self, code: str, role: str, number: str, name: str) -> None:
        number = sys.intern(number)
        self.names.setdefault(number, clean(name, NAME_LIMIT) or f"NIPC {number}")
        entries = self.by_operation.setdefault(code, [])
        if (role, number) not in entries:
            entries.append((role, number))


def _legal(number: str, name: str) -> bool:
    """Only legal-person NIPCs; pseudonymised and natural-person rows never pass."""
    return valid_nipc(number) and _label(name) != "rgpd"


def parse_parties(programme: Programme, content: bytes, stats: Counter[str]) -> PartyIndex:
    index = PartyIndex()
    for row in records(content, programme.parties.columns):
        stats["party_rows"] += 1
        role = programme.roles.get(_label(row["role"]))
        if role is None:
            raise EuFundsError(f"Papel de entidade desconhecido no ficheiro {programme.label}.")
        code = row["operation"].strip()
        number = nif(row["nif"])
        if not code or not _legal(number, row["name"]):
            # Name and number of a dropped row are never kept.
            stats["parties_dropped"] += 1
            if code:
                index.withheld.add(code)
            continue
        index.add(code, role, number, row["name"])
    return index


@dataclass(frozen=True)
class Funded:
    operation: Operation
    parties: tuple[tuple[str, str], ...]


def operations(
    programme: Programme, content: bytes, index: PartyIndex, stats: Counter[str]
) -> Iterator[Funded]:
    """Operations joined to their legal-person parties; operations without one are skipped."""
    seen: set[str] = set()
    for row in records(content, programme.operations.columns):
        operation = programme.build(row, stats)
        if not operation.code:
            stats["operations_without_code"] += 1
            continue
        stats["operations"] += 1
        if operation.code in seen:
            stats["duplicates"] += 1
            continue
        seen.add(operation.code)
        parties = index.by_operation.pop(operation.code, None)
        if parties is None and operation.lead is not None:
            # No entity rows: the operations sheet's principal beneficiary, if a legal person.
            number, name = nif(operation.lead[0]), operation.lead[1]
            if _legal(number, name):
                index.add(operation.code, LEAD, number, name)
                parties = index.by_operation.pop(operation.code)
        if not parties:
            stats["skipped_without_legal_party"] += 1
            continue
        # Titles redacted by the source ("RGPD") are replaced the same way.
        if operation.code in index.withheld or _label(operation.title) == "rgpd":
            stats["titles_withheld"] += 1
            title = f"{programme.label}: {programme.record} {operation.code}"
            operation = replace(operation, title=clean(title, TITLE_LIMIT))
        stats["events"] += 1
        yield Funded(operation, tuple(parties))
    index.withheld.clear()
    stats["orphan_party_rows"] += sum(len(parties) for parties in index.by_operation.values())


def event(
    programme: Programme, item: Funded, *, names: dict[str, str], entities: dict[str, Entity]
) -> EventInput:
    operation = item.operation
    details = dict(operation.details)
    leads = [number for role, number in item.parties if role == LEAD]
    if leads:
        details["lead_beneficiary"] = f"nipc:{leads[0]}"
    return EventInput(
        record_id=operation.code,
        kind=EU_FUNDING,
        title=operation.title or operation.code,
        start_date=operation.start,
        end_date=operation.end,
        amount=operation.amount,
        amount_label=programme.amount_label if operation.amount is not None else "",
        details=details,
        parties=tuple(
            PartyInput(
                role=PUBLIC_ROLE[role],
                name=names[number],
                entity=entities.get(number),
                identifier=f"nipc:{number}",
            )
            for role, number in item.parties
        ),
    )


class _Organisations:
    """NIPC organisations resolved (created if new) one event batch at a time."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names
        self.entities: dict[str, Entity] = {}
        self.created = 0

    def resolve(self, numbers: set[str]) -> None:
        missing = sorted(numbers - self.entities.keys())
        if not missing:
            return
        known = SourceIdentity.objects.filter(source=NIPC, external_id__in=missing).count()
        rows = {
            number: (self.names[number], *classify(self.names[number], number))
            for number in missing
        }
        self.entities.update(official_entities_bulk(NIPC, rows))
        self.created += len(missing) - known


def _events(
    programme: Programme, funded: Iterator[Funded], names: dict[str, str], found: _Organisations
) -> Iterator[EventInput]:
    for chunk in batched(funded, BATCH, strict=False):
        found.resolve({number for item in chunk for _, number in item.parties})
        for item in chunk:
            yield event(programme, item, names=names, entities=found.entities)


def summarise(
    programme: Programme, operations_content: bytes, index: PartyIndex, stats: Counter[str]
) -> dict[str, int]:
    """Read-only counts; no payloads or personal data."""
    numbers: set[str] = set()
    for item in operations(programme, operations_content, index, stats):
        numbers.update(number for _, number in item.parties)
    return {**stats, "organisations": len(numbers)}


def apply_programme(
    programme: Programme,
    operations_content: bytes,
    index: PartyIndex,
    stats: Counter[str],
    *,
    as_of: date,
) -> dict[str, int]:
    """Atomic: NIPC organisations and the complete programme as one events scope."""
    with import_transaction():
        found = _Organisations(index.names)
        result = sync_events(
            dataset=programme.dataset,
            scope=programme.key,
            events=_events(
                programme,
                operations(programme, operations_content, index, stats),
                index.names,
                found,
            ),
            as_of=as_of,
            complete=True,
            batch_size=BATCH,
        )
    return {**stats, **result, "organisations_created": found.created}


# Network ---------------------------------------------------------------------------


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise EuFundsError("Resposta dados.gov.pt inesperada.")
    return value


def select_resource(payload: JSONValue, sheet: Sheet) -> str:
    """URL of the one XLSX resource whose title matches the sheet."""
    items = _object(payload).get("resources")
    if not isinstance(items, list):
        raise EuFundsError("Resposta dados.gov.pt inesperada.")
    found: list[str] = []
    for item in items:
        entry = _object(item)
        title, url = entry.get("title"), entry.get("url")
        if (
            isinstance(title, str)
            and isinstance(url, str)
            and sheet.title.fullmatch(" ".join(title.split()))
            and RESOURCE_URL.fullmatch(url)
        ):
            found.append(url)
    if len(found) != 1:
        raise EuFundsError("Recurso de fundos europeus em falta ou ambíguo no dados.gov.pt.")
    return found[0]


class Client:
    def resource(self, sheet: Sheet) -> str:
        url = f"{API_ROOT}{sheet.slug}/"
        try:
            content = download(
                url,
                allowed=lambda target: target == url,
                max_bytes=API_BYTES,
                deadline=time.monotonic() + API_TIMEOUT,
                headers={"Accept": "application/json"},
            )
        except OfficialHTTPError as exc:
            raise EuFundsError(str(exc)) from exc
        try:
            payload = cast(JSONValue, json.loads(content))
        except (ValueError, RecursionError) as exc:
            raise EuFundsError("JSON dados.gov.pt inválido.") from exc
        return select_resource(payload, sheet)

    def content(self, url: str) -> bytes:
        try:
            return download(
                url,
                allowed=lambda target: target == url,
                max_bytes=XLSX_BYTES,
                deadline=time.monotonic() + TOTAL_TIMEOUT,
            )
        except OfficialHTTPError as exc:
            raise EuFundsError(str(exc)) from exc


def run(programme: Programme, *, apply: bool, as_of: date) -> dict[str, int]:
    """One programme: the parties file is parsed and released before the operations file."""
    client = Client()
    parties_url = client.resource(programme.parties)
    operations_url = client.resource(programme.operations)
    stats: Counter[str] = Counter()
    index = parse_parties(programme, client.content(parties_url), stats)
    content = client.content(operations_url)
    if not apply:
        return summarise(programme, content, index, stats)
    return apply_programme(programme, content, index, stats, as_of=as_of)
