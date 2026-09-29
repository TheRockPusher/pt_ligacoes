"""Entidade do Tesouro e Finanças (ETF): members of the boards of state-owned companies.

The public company list (``/informacao-sobre-as-empresas``) links one page per company; a
page may attach a "Modelo de governo/Membros dos órgãos sociais" PDF whose tables read
``Cargo | Órgãos Sociais | Eleição/Nomeação | Mandato``. The text layer is extracted with
pypdf (layout mode, plain mode as a fallback) and read organ by organ: cargo, name,
election and mandate cells are collected per column and paired in order, which covers
row-aligned tables, vertically centred cells and column-ordered extractions alike. A
table whose cells cannot be paired one to one is skipped and counted.

Members are name-only persons and the site publishes no NIPC, so every row is a private
candidate without identity or organisation; editors resolve both on conversion. Reading
stops at remuneration, benefits and CV sections; auditing firms and vacant seats are
dropped; free-text notes only contribute resignation dates and designation acts.
"""

import hashlib
import logging
import re
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import quote, unquote, urljoin, urlsplit

from django.utils import timezone
from pypdf import PdfReader

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_observations, sync_scoped_snapshot
from .government import revised
from .models import (
    DatePrecision,
    EnrichmentSource,
    Relationship,
    SourceObservation,
    TemporalStatus,
    import_transaction,
)
from .official_http import OfficialHTTPError, download

HOST = "www.etf.gov.pt"
SITE = f"https://{HOST}"
INDEX_PATH = "/informacao-sobre-as-empresas"
INDEX_URL = f"{SITE}{INDEX_PATH}"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DOCUMENT_PATH = re.compile(r"/media/doc/[A-Za-z0-9._~%-]{1,200}\.pdf", re.IGNORECASE)
INDEX_BYTES = 2 * 1024 * 1024
PAGE_BYTES = 2 * 1024 * 1024
PDF_BYTES = 25 * 1024 * 1024
PAGE_TIMEOUT = 30
PDF_TIMEOUT = 120
MAX_PDF_PAGES = 120
MAX_COMPANIES = 400
# Seconds between requests: sequential and polite.
PAUSE = 0.5
PREFIX = "etf:"

DATASET = DATASETS["etf_orgaos_sociais"]
OFFICE_HOLDING = SourceObservation.Category.OFFICE_HOLDING
DIRECTORSHIP = Relationship.Kind.DIRECTORSHIP
ROLE = Relationship.RoleClass
# A nine-digit run could be a personal tax number: such cells are never read.
NINE_DIGITS = re.compile(r"(?<!\d)\d{9}(?!\d)")

MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}
NUMERIC_DATE = re.compile(r"\b(\d{1,2})\s?[/.-]\s?(\d{1,2})\s?[/.-]\s?(\d{4})\b")
LONG_DATE = re.compile(
    r"\b(\d{1,2})\.?º?\s+(?:de\s+)?([a-zç]+)\s+(?:de\s+)?(\d{4})\b", re.IGNORECASE
)
YEARS = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*(?:[-/\u2013]|\be\b|\ba\b)\s*((?:19|20)\d{2})(?!\d)")
MANDATE_CELL = re.compile(r"(?:19|20)\d{2}\s*[-/\u2013]\s*(?:19|20)\d{2}|(?:19|20)\d{2}")
PARTIAL_MANDATE = re.compile(r"((?:19|20)\d{2})\s*[-/\u2013]")
DATE_CELL = re.compile(r"\d{1,2}\s?[/.-]\s?\d{1,2}\s?[/.-]\s?\d{4}")
# Designation acts: institutional identifiers only (never the surrounding free text).
_DAY = r"\d{1,2}(?:\s?[/.-]\s?\d{1,2}\s?[/.-]\s?\d{4}|\.?º?\s+de\s+[a-zç]+\s+de\s+\d{4})"
ACT = re.compile(
    r"Resolu[çc][ãa]o\s+do\s+Conselho\s+de\s+Ministros\s+n\.?\s*[º°o]\s*[\w/.-]+"
    r"|\bRCM\s+n\.?\s*[º°o]\s*[\w/.-]+"
    r"|\bDesp(?:acho|\.)?(?:\s+(?:conjunto|[A-Z]{2,6}(?:\s+e\s+[A-Z]{2,6})?))?\s+n\.?\s*[º°o]\s*"
    r"[\d][\w/.-]*"
    r"|Delibera[çc][ãa]o\s+Un[âa]nime(?:\s+por\s+Escrito)?(?:\s+de\s+" + _DAY + r")?"
    r"|\bDUE(?:\s+de\s+" + _DAY + r")?"
    r"|\b(?:Assembleia[- ]Geral|AG)\s+de\s+" + _DAY,
    re.IGNORECASE,
)
ACT_START = re.compile(
    r"(?:Resolu[çc][ãa]o\b|Delibera[çc][ãa]o\b|Desp(?:acho|\.)|DUE\b|RCM\b|Dep\s|"
    r"Conselho\s+de\s+Ministros|Ministros\b|por\s+escrito\b)",
    re.IGNORECASE,
)

# Folded (accent-free, casefolded) vocabularies.
HEADER_CARGO = re.compile(r"\bcargos?\b")
HEADER_ORGAN = re.compile(r"\borgaos?\b")
HEADER_TAIL = re.compile(r"\b(?:mandatos?|eleicao|nomeacao|designacao)")
ELECTION_HEADER = re.compile(r"eleicao|nomeacao|designacao")
HEADER_WORD = re.compile(r"orgaos? sociais|orgaos?|sociais|eleicao|nomeacao|designacao|mandatos?")
SECTION = re.compile(r"^mandatos?\b")
ENDED_SECTION = re.compile(r"\bate\s+(?:a\s+|ao\s+)?\d")
STOP = re.compile(
    r"^(?:\d+\.?\s*)?(?:estatuto remunerat|remunera|curricul|sintese curricular|notas? curricular"
    r"|regalias|beneficios|senhas de presenca|despesas de representacao|viaturas|deslocac)"
)
JUNK = re.compile(r"^(?:\d{1,3}(?:\s*/\s*\d{1,3})?|p[aá]gina\s+\d+.*)$", re.IGNORECASE)
# Template placeholders left in some documents.
PLACEHOLDER = re.compile(r"Inserir\s+Logotipo\s+Aqui", re.IGNORECASE)
# Organ titles, compared without spaces or hyphens (extraction splits words: "Admin istração").
ORGANS = {
    "mesadaassembleiageral": "Mesa da Assembleia Geral",
    "assembleiageral": "Assembleia Geral",
    "conselhodeadministracao": "Conselho de Administração",
    "conselhodeadministracaoexecutivo": "Conselho de Administração Executivo",
    "conselhofiscal": "Conselho Fiscal",
    "conselhogeraledesupervisao": "Conselho Geral e de Supervisão",
    "conselhoconsultivo": "Conselho Consultivo",
    "conselhodiretivo": "Conselho Diretivo",
    "conselhodegerencia": "Conselho de Gerência",
    "conselhoexecutivo": "Conselho Executivo",
    "conselhodecuradores": "Conselho de Curadores",
    "comissaoexecutiva": "Comissão Executiva",
    "comissaodeauditoria": "Comissão de Auditoria",
    "comissaodevencimentos": "Comissão de Vencimentos",
    "comissaoderemuneracoes": "Comissão de Remunerações",
    "comissaodefixacaoderemuneracoes": "Comissão de Fixação de Remunerações",
    "revisoroficialdecontas": "Revisor Oficial de Contas",
    "revisoroficialdecontas(roc)": "Revisor Oficial de Contas",
    "sroc": "SROC",
    "roc": "Revisor Oficial de Contas",
    "fiscalunico": "Fiscal Único",
    "auditorexterno": "Auditor Externo",
    "auditoresexternos": "Auditor Externo",
    "direcao": "Direção",
    "gerencia": "Gerência",
}
FOOTNOTE = re.compile(r"^\((\*{1,3}|[a-z]|\d{1,2})\)\s*[-\u2013:]?\s*(\S.*)$|^(\*{1,3})\s*(\S.*)$")
ROLE_NOTE = re.compile(r"\s*\(([^()]+)\)?\s*$")
MARKER_CELL = re.compile(r"\((?:\*{1,3}|[a-z])\)|\*{1,3}")
MARKER = re.compile(r"\s*\((\*{1,3}|[a-z])\)\s*$|\s*(\*{1,3})\s*$")
ROLE_NUMBER = re.compile(r"\s*\(\d{1,2}\)\s*$")
RESIGNATION = re.compile(r"\bren\w*cia\b|\bcess(?:ou|acao)\b|\bexonera|\bdestitu")
ROC_SUFFIX = re.compile(r"\s*[\u2013-]?\s*\(?\bO?ROC\s+n\.?\s*[º°o]\s*\d+\)?\s*$", re.IGNORECASE)
FIRM = re.compile(
    r"\bS\.?\s?R\.?\s?O\.?\s?C\b|\bLda\b|\bS\.\s?A\.?(?=$|[\s,])|&|\bSociedade\b|\bAssociados\b"
    r"|\bAudit\b|\bAuditores\b|\bConsultores\b|\bPartners\b",
    re.IGNORECASE,
)
AUDITOR_ROLE = re.compile(r"\b(?:s?roc|revisor|auditor|fiscal unico)\b")
VACANT = frozenset(
    {"-", "\u2013", "—", "vago", "vaga", "n.a.", "n/a", "a designar", "por designar"}
)
PARTICLES = frozenset(
    {"de", "da", "do", "das", "dos", "e", "d'", "del", "della", "van", "von", "y"}
)
CONNECTORS = frozenset({"e", "de", "da", "do", "das", "dos", "a", "o", "(e"})
ROLE_HEAD = re.compile(
    r"^(?:presidente|vice\b|vice-|vogal|vogais|secretari[oa]|administrador|diretor|director"
    r"|enfermeir|membro|efetiv|efectiv|suplente|revisor|fiscal|tesoureir|relator"
    r"|gestor|auditor|ceo$|cfo$)"
)
ROLE_WORD = re.compile(
    r"^(?:e|da|de|do|das|dos|nao|executiv[oa]s?|comissao|auditoria|financeir[oa]|clinic[oa]"
    r"|diretor[a]?|director[a]?|enfermeir[oa]|oficial|contas|unico|efe?c?tiv[oa]s?|suplentes?"
    r"|conselho|administracao|fiscal|geral|mesa|assembleia(?:-geral)?|independentes?|presidente"
    r"|vice(?:-presidente)?|vice-|vogal|vogais|secretari[oa]|membros?|roc|sroc|revisor"
    r"|auditor|tesoureir[oa]|relator[a]?|gestor[a]?|executiva|cuidados|hospitalares|saude"
    r"|primarios|continuados|para|os|\d{1,2}|-|\u2013)$"
)

TITLE = re.compile(r"^(?:(?:Dr|Prof|Eng|Enf|Arq)[aªº]?\.?[aªº]?\.?|Doutora?|Professora?)$")


class EtfError(ValueError):
    """Safe, payload-free failure for an incomplete or unexpected ETF response."""


def fold(text: str) -> str:
    """Accent-free, casefolded, single-spaced text for matching only."""
    plain = "".join(
        char
        for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )
    return " ".join(plain.split())


def _spaces(text: str) -> str:
    return " ".join(text.split())


def parse_day(text: str) -> date | None:
    """The first numeric or long-form Portuguese date in ``text``."""
    candidates: list[tuple[int, date | None]] = []
    numeric = NUMERIC_DATE.search(text)
    if numeric:
        day, month, year = (int(part) for part in numeric.groups())
        candidates.append((numeric.start(), _date(year, month, day)))
    long = LONG_DATE.search(fold(text))
    if long and long.group(2) in MONTHS:
        # Folded text keeps offsets close enough to order the two forms.
        candidates.append(
            (long.start(), _date(int(long.group(3)), MONTHS[long.group(2)], int(long.group(1))))
        )
    found = [value for _, value in sorted(candidates, key=lambda item: item[0]) if value]
    return found[0] if found else None


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _years(first: int, last: int) -> tuple[int, int] | None:
    """A plausible mandate: ordered, at most eight years long."""
    if 1974 <= first <= last <= first + 8:
        return first, last
    return None


def mandate_years(text: str) -> tuple[int, int] | None:
    found = YEARS.search(text)
    if found:
        return _years(int(found.group(1)), int(found.group(2)))
    single = re.fullmatch(r"\s*((?:19|20)\d{2})\s*", text)
    return _years(int(single.group(1)), int(single.group(1))) if single else None


def role_class(cargo: str) -> str:
    """Shared role class from the cargo wording (first word decides; substitutes first)."""
    text = fold(cargo)
    if re.search(r"\bsuplentes?\b", text):
        return ROLE.SUBSTITUTE
    if text.startswith("vice"):
        return ROLE.DEPUTY_LEADERSHIP
    if text.startswith("presidente"):
        return ROLE.LEADERSHIP
    if ROLE_HEAD.match(text):
        return ROLE.MEMBER
    return ROLE.OTHER


# Text layout ------------------------------------------------------------------------


def clean_line(raw: str) -> str:
    """Repair common extraction artefacts: split capitals and glued cells."""
    line = PLACEHOLDER.sub("", raw.replace("\xa0", " ").replace("\t", "    ")).rstrip()
    # "M aria" → "Maria" (a detached capital), never before a particle ("A dos …").
    line = re.sub(r"\b([A-ZÀ-ÖØ-Ý]) (?!(?:de|da|do|das|dos|e)\b)(?=[a-zà-öø-ÿ]{2,})", r"\1", line)
    # Glued cells: "PresidenteTeresa", "(Executivo)Joaquim", "(a)31/05/2021", "…20262026-2029".
    line = re.sub(r"(?<=[a-zà-öø-ÿ)])(?=[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ])", "  ", line)
    line = re.sub(r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{4})(?=(?:19|20)\d{2})", r"\1  ", line)
    line = re.sub(r"(?<=[^\s\d/.,:º°-]) ?(?=\d{1,2}[/.-]\d{1,2}[/.-]\d{4}\b)", "  ", line)
    return re.sub(r"(?<=[^\s\d]) (?=(?:19|20)\d{2}\s*[-/\u2013]\s*(?:19|20)\d{2}\b)", "  ", line)


def segments(line: str) -> list[tuple[int, str]]:
    """Cells of one layout line: runs separated by two or more spaces."""
    return [(match.start(), match.group()) for match in re.finditer(r"\S+(?: \S+)*", line)]


def _tokens(text: str) -> list[str]:
    return [fold(token).strip("(),;:.") or fold(token) for token in text.split()]


def split_role(text: str, *, continuation: bool) -> tuple[str, str]:
    """(cargo words, remainder) of one cell; a cargo starts with a role word."""
    words = text.split()
    tokens = _tokens(text)
    count = 0
    while count < len(tokens) and _vocabulary(tokens[count]):
        count += 1
    if not count:
        return "", text
    if not ROLE_HEAD.match(tokens[0]):
        # A cargo continuation ("da Comissão Executiva", "Cuidados Hospitalares") is cargo
        # vocabulary throughout; outside the cargo column it needs more than particles.
        substantive = any(
            token.isalpha() and (continuation or token not in PARTICLES) for token in tokens
        )
        if not substantive:
            return "", text
        # "Executivo Dr. Fernando …": a wrapped cargo word glued before a titled name.
        if count < len(tokens) and not (continuation and TITLE.match(words[count])):
            return "", text
    return " ".join(words[:count]), " ".join(words[count:])


def _vocabulary(token: str) -> bool:
    return bool(ROLE_WORD.match(token) or ROLE_HEAD.match(token))


def _starts_role(cargo: str) -> bool:
    tokens = _tokens(cargo)
    return bool(tokens) and bool(ROLE_HEAD.match(tokens[0]))


def _open_role(cargo: str) -> bool:
    words = cargo.split()
    return bool(words) and (fold(words[-1]) in CONNECTORS or cargo.count("(") > cargo.count(")"))


def name_like(text: str) -> bool:
    """A published person or firm name, not prose, numbers or labels."""
    if any(char.isdigit() for char in text) or ":" in text:
        return False
    words = text.split()
    if not words or len(words) > 14:
        return False
    for word in words:
        bare = word.strip("(),;.'\"“”-\u2013")
        if not bare or bare.casefold() in PARTICLES:
            continue
        if not bare[0].isupper():
            return False
    return True


def _prose(text: str) -> bool:
    """A sentence rather than a cell: several words, some lowercase and not particles."""
    if FIRM.search(text):
        return False
    words = ROC_SUFFIX.sub("", text).split()
    lowercase = [
        word
        for word in words
        if word[:1].islower() and word.strip("(),;:.").casefold() not in PARTICLES | {"por"}
    ]
    return len(words) >= 4 and bool(lowercase)


def _markers(text: str) -> tuple[str, frozenset[str]]:
    markers: set[str] = set()
    while found := MARKER.search(text):
        markers.add(found.group(1) or found.group(2))
        text = text[: found.start()]
    return text.strip(" ,;\u2013-"), frozenset(markers)


# Table parsing ----------------------------------------------------------------------


@dataclass(frozen=True)
class BoardRow:
    organ: str
    cargo: str
    name: str
    page: int
    row: int  # position in its organ's table, counting vacant seats and firms
    years: tuple[int, int] | None
    act: str
    appointed_on: date | None
    resigned_on: date | None
    ended: bool

    @property
    def key(self) -> str:
        """Stable row id: organ, cargo, name and mandate as published."""
        years = f"{self.years[0]}-{self.years[1]}" if self.years else ""
        basis = "|".join((fold(self.organ), fold(self.cargo), fold(self.name), years))
        return hashlib.sha256(basis.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ParsedDocument:
    rows: tuple[BoardRow, ...]
    tables: int
    unparseable: int
    firms: int
    vacant: int
    duplicates: int

    @property
    def has_table(self) -> bool:
        return self.tables > 0


@dataclass
class _Cell:
    text: str
    kind: str  # person | firm | vacant
    page: int
    position: int
    markers: frozenset[str]
    note: str = ""  # a cargo detail printed after the name, e.g. "(Diretora Clínica)"


@dataclass
class _Block:
    organ: str
    markers: frozenset[str]
    cargos: list[str] = field(default_factory=list)
    names: list[_Cell] = field(default_factory=list)
    elections: list[str] = field(default_factory=list)
    mandates: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.cargos and not self.names


@dataclass
class _Section:
    years: tuple[int, int] | None = None
    act: str = ""
    ended: bool = False
    blocks: list[_Block] = field(default_factory=list)
    footnotes: list[tuple[int, str, str]] = field(default_factory=list)
    # Read with known column positions: cells are row-aligned, never re-sequenced.
    positional: bool = False


@dataclass(frozen=True)
class _Pair:
    cargo: str
    cell: _Cell
    block: _Block
    years: tuple[int, int] | None
    act: str
    appointed_on: date | None


def _heading(text: str) -> tuple[tuple[int, int] | None, str, bool]:
    folded = fold(text)
    years = mandate_years(text)
    if years is None:
        single = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
        years = _years(int(single.group(1)), int(single.group(1))) if single else None
    act = ACT.search(_join_hyphens(text))
    return years, _act(act.group()) if act else "", bool(ENDED_SECTION.search(folded))


def _join_hyphens(text: str) -> str:
    return re.sub(r"(?<=\w)-\s+(?=\w)", "-", _spaces(text))


def _act(text: str) -> str:
    return _spaces(text).rstrip(".,;")


def _distribute[T](values: list[T], count: int, persons: list[int]) -> list[T | None] | None:
    """Values per pair: one each, one per person pair, or a single shared value."""
    if values and len(values) == count:
        return list(values)
    if values and len(values) == len(persons):
        result: list[T | None] = [None] * count
        for index, value in zip(persons, values, strict=True):
            result[index] = value
        return result
    if values and len(set(values)) == 1:
        return [values[0]] * count
    return None


class _TableParser:
    """Line-by-line state machine over the extracted pages of one document."""

    def __init__(self) -> None:
        self.active = False
        self.columns: dict[str, float] | None = None
        self.sections: list[_Section] = []
        self.section: _Section | None = None
        self.block: _Block | None = None
        self.pending_heading: tuple[tuple[int, int] | None, str, bool] | None = None
        self.pending_organ: tuple[str, frozenset[str]] | None = None
        self.pending_gap = 0
        self.footnote: list[str] | None = None
        self.position = 0

    # Structure ---------------------------------------------------------------

    def _new_section(self, heading: tuple[tuple[int, int] | None, str, bool] | None) -> None:
        self._close_footnote()
        years, act, ended = heading if heading else (None, "", False)
        self.section = _Section(years=years, act=act, ended=ended)
        self.sections.append(self.section)
        self.block = None

    def _new_block(self, organ: str, markers: frozenset[str]) -> _Block:
        if self.section is None:
            self._new_section(None)
        if self.section is None:
            raise EtfError("Board section missing while opening a block.")
        self.block = _Block(organ=organ, markers=markers)
        self.section.blocks.append(self.block)
        return self.block

    def _current_block(self) -> _Block:
        return self.block if self.block is not None else self._new_block("", frozenset())

    def _close_footnote(self) -> None:
        if self.footnote is not None and self.section is not None:
            marker, *parts = self.footnote
            self.section.footnotes.append((self.position, marker, _spaces(" ".join(parts))[:600]))
        self.footnote = None

    # Line kinds --------------------------------------------------------------

    def _header(self, lines: Sequence[str], index: int) -> int:
        """Lines consumed by a table header at ``index`` (0 if none)."""
        folded = fold(lines[index])
        if not HEADER_CARGO.search(folded):
            return 0
        if HEADER_ORGAN.search(folded) and HEADER_TAIL.search(folded):
            self.columns = self._column_centres(lines[index])
            return 1
        # Column-ordered extraction: header words on consecutive short lines.
        if folded not in {"cargo", "cargos"}:
            return 0
        labels: list[str] = []
        last = index
        for offset in range(index + 1, min(len(lines), index + 9)):
            text = fold(lines[offset])
            if not text:
                continue
            if not HEADER_WORD.fullmatch(text):
                break
            labels.append(text)
            last = offset
        joined = " ".join(labels)
        if HEADER_ORGAN.search(joined) and HEADER_TAIL.search(joined):
            self.columns = None
            return last - index + 1
        return 0

    @staticmethod
    def _column_centres(line: str) -> dict[str, float] | None:
        """Column centres of a well-spaced header; glued labels mean content-only reading."""
        centres: dict[str, float] = {}
        owners: dict[str, int] = {}
        for number, (start, text) in enumerate(segments(line)):
            for word in re.finditer(r"\S+(?: (?:Sociais|social))?", text):
                label = fold(word.group())
                middle = start + word.start() + len(word.group()) / 2
                if label.startswith("cargo"):
                    column = "cargo"
                elif label.startswith("orgao"):
                    column = "name"
                elif ELECTION_HEADER.match(label):
                    column = "election"
                elif label.startswith("mandato"):
                    column = "mandate"
                else:
                    continue
                centres.setdefault(column, middle)
                owners.setdefault(column, number)
        if not {"cargo", "name"} <= centres.keys() or len(set(owners.values())) < len(owners):
            return None
        return centres

    @staticmethod
    def _organ(text: str) -> tuple[str, frozenset[str]] | None:
        """(canonical organ title, footnote markers) of an organ title line."""
        stripped, markers = _markers(text)
        stripped = re.sub(r"\s*\((?:\*{1,3}|[a-z])\)", "", stripped)
        title = ORGANS.get(re.sub(r"[\s-]", "", fold(stripped)))
        return (title, markers) if title else None

    # Feeding -----------------------------------------------------------------

    def parse(self, pages: Sequence[str]) -> None:
        for number, page in enumerate(pages, 1):
            lines = [clean_line(raw) for raw in page.splitlines()]
            skip = 0
            for index in range(len(lines)):
                self.position += 1
                if skip:
                    skip -= 1
                    continue
                skip = self._line(lines, index, page=number)
            self._close_footnote()
        self._close_footnote()

    def _line(self, lines: Sequence[str], index: int, *, page: int) -> int:
        line = lines[index]
        text = _spaces(line)
        if not text:
            self._close_footnote()
            return 0
        if JUNK.search(text):
            return 0
        folded = fold(text)
        if STOP.match(folded):
            self._close_footnote()
            self.active = False
            self.pending_heading, self.pending_organ = None, None
            return 0
        consumed = self._header(lines, index)
        if consumed:
            self._close_footnote()
            if not self.active:
                self.active = True
                self._new_section(self.pending_heading)
                if self.pending_organ is not None:
                    self._new_block(*self.pending_organ)
            self.pending_heading, self.pending_organ, self.pending_gap = None, None, 0
            if self.section is not None and self.columns is not None:
                self.section.positional = True
            return consumed - 1
        if SECTION.match(folded) and re.search(r"(?:19|20)\d{2}", text):
            heading = _heading(" ".join(_spaces(item) for item in lines[index : index + 2]))
            if self.active:
                self._new_section(heading)
            else:
                self.pending_heading, self.pending_organ, self.pending_gap = heading, None, 0
            return 0
        organ = self._organ_line(line)
        if not self.active:
            if organ is not None:
                self.pending_organ = organ[0]
            else:
                self.pending_gap += 1
                self.pending_organ = None
                if self.pending_gap > 3:
                    self.pending_heading = None
            return 0
        footnote = FOOTNOTE.match(text)
        if footnote:
            self._close_footnote()
            marker = footnote.group(1) or footnote.group(3)
            self.footnote = [marker, footnote.group(2) or footnote.group(4)]
            return 0
        if organ is not None:
            self._close_footnote()
            block = self._new_block(*organ[0])
            self._add_cells(block, organ[1], page=page)
            return 0
        if self.footnote is not None:
            self.footnote.append(text)
            return 0
        self._add_cells(self._current_block(), segments(line), page=page)
        return 0

    def _organ_line(
        self, line: str
    ) -> tuple[tuple[str, frozenset[str]], list[tuple[int, str]]] | None:
        """An organ title (left cells only); election/mandate cells stay with its block."""
        left: list[str] = []
        right: list[tuple[int, str]] = []
        for start, cell in segments(line):
            column = self._column(start, cell)
            if column in {"election", "mandate"}:
                right.append((start, cell))
            elif column == "cargo" and self.block is not None and self.block.cargos:
                # An open cargo ("Vogal e Vice-Presidente da") continues ("Comissão Executiva").
                if _open_role(self.block.cargos[-1]):
                    return None
                left.append(cell)
            else:
                left.append(cell)
        if not left:
            return None
        organ = self._organ(" ".join(left))
        return (organ, right) if organ is not None else None

    def _column(self, start: int, cell: str) -> str | None:
        """Right-hand columns by position when known; otherwise by content."""
        mandate = bool(MANDATE_CELL.fullmatch(cell) or PARTIAL_MANDATE.fullmatch(cell))
        dated = bool(DATE_CELL.fullmatch(cell))
        column: str | None = None
        if (centres := self.columns) is not None:
            middle = start + len(cell) / 2
            column = min(centres, key=lambda key: abs(centres[key] - middle))
            if column == "mandate" and dated:
                return "election"
            if column == "election" and YEARS.fullmatch(cell):
                return "mandate"
            if column in {"election", "mandate"}:
                return column
        if mandate:
            return "mandate"
        if dated or ACT_START.match(cell):
            return "election"
        return column

    def _add_cells(self, block: _Block, cells: list[tuple[int, str]], *, page: int) -> None:
        cargo_parts: list[str] = []
        name_parts: list[str] = []
        mandates: list[str] = []
        elections: list[str] = []
        for start, cell in cells:
            column = self._column(start, cell)
            if column == "mandate":
                mandates.append(cell)
                continue
            if column == "election":
                elections.append(cell)
                continue
            if MARKER_CELL.fullmatch(cell):
                name_parts.append(cell)
                continue
            cargo, rest = split_role(cell, continuation=column in {"cargo", None})
            if cargo:
                cargo_parts.append(cargo)
            if rest:
                name_parts.append(rest)
        name = " ".join(name_parts)
        if not cargo_parts and _prose(name):
            # Notes inside a table (e.g. "As funções … são exercidas por …") carry no cells.
            return
        block.mandates += mandates
        block.elections += elections
        if cargo_parts:
            self._add_cargo(block, " ".join(cargo_parts))
        if name:
            self._add_name(block, name, page=page)

    @staticmethod
    def _add_cargo(block: _Block, cargo: str) -> None:
        cargo = re.sub(r"\b(Vice)-\s+", r"\1-", _spaces(cargo), flags=re.IGNORECASE)
        cargo = cargo.strip(" :")
        if not cargo:
            return
        if block.cargos and (not _starts_role(cargo) or _open_role(block.cargos[-1])):
            block.cargos[-1] = f"{block.cargos[-1]} {cargo}"
        else:
            block.cargos.append(cargo)

    def _add_name(self, block: _Block, text: str, *, page: int) -> None:
        text = _spaces(text)
        if NINE_DIGITS.search(text):
            return
        if fold(text) in VACANT:
            block.names.append(_Cell(text, "vacant", page, self.position, frozenset()))
            return
        text, markers = _markers(text)
        note = ""
        detail = ROLE_NOTE.search(text)
        # "Enf.ª" is also a courtesy title, so it marks a cargo only inside the note.
        if detail and re.match(rf"{ROLE_HEAD.pattern}|enf", fold(detail.group(1))):
            note, text = _spaces(detail.group(1)).strip(" \u2013-"), text[: detail.start()]
        firm = bool(FIRM.search(text))
        text = ROC_SUFFIX.sub("", text).strip(" ,;\u2013-")
        if not text or not (firm or name_like(text)):
            return
        last = block.names[-1] if block.names else None
        if (
            last is not None
            and last.kind != "vacant"
            and (
                fold(text.split()[0]) in PARTICLES
                or last.text.endswith((",", "-", "\u2013"))
                or fold(last.text.split()[-1]) in PARTICLES | {"por"}
            )
        ):
            last.text = f"{last.text} {text}"
            last.markers |= markers
            if firm:
                last.kind = "firm"
            return
        kind = "firm" if firm else "person"
        block.names.append(_Cell(text, kind, page, self.position, markers, note))

    # Pairing -----------------------------------------------------------------

    @staticmethod
    def _reconcile(cells: list[tuple[_Block, _Cell]], target: int) -> list[tuple[_Block, _Cell]]:
        """Merge wrapped short name lines (within one organ) until ``target`` remain."""
        names = [(block, replace(cell)) for block, cell in cells]
        while len(names) > target:
            candidates = [
                index
                for index in range(1, len(names))
                if names[index][1].kind == "person"
                and names[index - 1][0] is names[index][0]
                and names[index - 1][1].kind != "vacant"
                and len(names[index][1].text.split()) <= 2
            ]
            if not candidates:
                break
            index = min(candidates, key=lambda item: (len(names[item][1].text.split()), -item))
            previous, current = names[index - 1][1], names[index][1]
            previous.text = f"{previous.text} {current.text}"
            previous.markers |= current.markers
            del names[index]
        return names

    @staticmethod
    def _values(
        block_mandates: list[str], block_elections: list[str]
    ) -> tuple[list[tuple[int, int]], list[str], list[date]]:
        joined = " ".join(block_mandates)
        years = [
            found
            for match in YEARS.finditer(joined)
            if (found := _years(int(match.group(1)), int(match.group(2))))
        ] or [
            found
            for match in re.finditer(r"(?<![\d-])((?:19|20)\d{2})(?![\d-])", joined)
            if (found := _years(int(match.group(1)), int(match.group(1))))
        ]
        text = _join_hyphens(" ".join(block_elections))
        acts = [_act(match.group()) for match in ACT.finditer(text)]
        remainder = ACT.sub(" ", text)
        dates = [
            found
            for match in NUMERIC_DATE.finditer(remainder)
            if (found := _date(int(match.group(3)), int(match.group(2)), int(match.group(1))))
        ]
        return years, acts, dates

    def _pairs(
        self,
        section: _Section,
        items: list[tuple[_Block, str, _Cell]],
        mandate_cells: list[str],
        election_cells: list[str],
    ) -> list[_Pair]:
        count = len(items)
        persons = [index for index, (_, _, cell) in enumerate(items) if cell.kind == "person"]
        years, acts, dates = self._values(mandate_cells, election_cells)
        by_years = _distribute(years, count, persons)
        if by_years is None:
            fallback = section.years
            partial = {
                int(found.group(1)) for found in PARTIAL_MANDATE.finditer(" ".join(mandate_cells))
            }
            if partial and (fallback is None or partial != {fallback[0]}):
                fallback = None
            by_years = [fallback] * count
        by_acts = _distribute(acts, count, persons) or [""] * count
        by_dates: list[date | None] = (
            (_distribute(dates, count, persons) or [None] * count) if not acts else [None] * count
        )
        return [
            _Pair(
                cargo=cargo,
                cell=cell,
                block=block,
                years=by_years[index],
                act=by_acts[index] or "",
                appointed_on=by_dates[index],
            )
            for index, (block, cargo, cell) in enumerate(items)
        ]

    def finish(self) -> ParsedDocument:
        rows: list[BoardRow] = []
        tables = unparseable = firms = vacant = 0
        for section in self.sections:
            blocks = [block for block in section.blocks if not block.empty]
            tables += len(blocks)
            pairs: list[_Pair] = []
            failed: list[_Block] = []
            for block in blocks:
                names = self._reconcile([(block, cell) for cell in block.names], len(block.cargos))
                if block.cargos and len(names) == len(block.cargos) and _whole(names):
                    items = [
                        (block, cargo, cell)
                        for cargo, (_, cell) in zip(block.cargos, names, strict=True)
                    ]
                    pairs += self._pairs(section, items, block.mandates, block.elections)
                else:
                    failed.append(block)
            if failed and not section.positional:
                # Column-ordered extraction: every cargo, then every name, in order.
                cargos = [cargo for block in blocks for cargo in block.cargos]
                cells = self._reconcile(
                    [(block, cell) for block in blocks for cell in block.names], len(cargos)
                )
                if cargos and len(cargos) == len(cells) and _whole(cells):
                    pairs = self._pairs(
                        section,
                        [
                            (block, cargo, cell)
                            for cargo, (block, cell) in zip(cargos, cells, strict=True)
                        ],
                        [cell for block in blocks for cell in block.mandates],
                        [cell for block in blocks for cell in block.elections],
                    )
                else:
                    unparseable += len(failed)
            elif failed:
                unparseable += len(failed)
            ordinals: dict[int, int] = {}
            for pair in pairs:
                ordinal = ordinals[id(pair.block)] = ordinals.get(id(pair.block), 0) + 1
                if pair.cell.kind == "firm" or _auditing_firm(pair):
                    firms += 1
                    continue
                if pair.cell.kind == "vacant":
                    vacant += 1
                    continue
                rows.append(self._row(section, pair, ordinal))
        unique: dict[str, BoardRow] = {}
        for row in _supersede(rows):
            unique.setdefault(row.key, row)
        return ParsedDocument(
            rows=tuple(unique.values()),
            tables=tables,
            unparseable=unparseable,
            firms=firms,
            vacant=vacant,
            duplicates=len(rows) - len(unique),
        )

    @staticmethod
    def _row(section: _Section, pair: _Pair, ordinal: int) -> BoardRow:
        cell = pair.cell
        resigned: date | None = None
        act = pair.act
        for marker in sorted(cell.markers | pair.block.markers):
            note = next(
                (
                    text
                    for position, key, text in section.footnotes
                    if key == marker and position > cell.position
                ),
                None,
            )
            if note is None:
                continue
            folded = fold(note)
            if marker in cell.markers and (cessation := RESIGNATION.search(folded)):
                resigned = resigned or parse_day(folded[cessation.start() :])
            if not act and (found := ACT.search(_join_hyphens(note))):
                act = _act(found.group())
        cargo = ROLE_NUMBER.sub("", pair.cargo).strip(" :\u2013-")
        if cell.note:
            cargo = f"{cargo} ({cell.note})"
        return BoardRow(
            organ=pair.block.organ,
            cargo=cargo[:160],
            name=cell.text[:300],
            page=cell.page,
            row=ordinal,
            years=pair.years,
            act=(act or section.act)[:200],
            appointed_on=pair.appointed_on,
            resigned_on=resigned,
            ended=section.ended or resigned is not None,
        )


def _whole(cells: list[tuple[_Block, _Cell]]) -> bool:
    """Every person cell is a complete name; a fragment means the table did not pair."""
    return all(cell.kind != "person" or not _fragment(cell.text) for _, cell in cells)


def _fragment(name: str) -> bool:
    """Fewer than two name words, a dangling title, or cargo words inside the name."""
    words = name.split()
    capitalised = [word for word in words if word[:1].isupper() and not TITLE.match(word)]
    return (
        len(capitalised) < 2
        or bool(TITLE.match(words[-1]))
        or any(_role_word(token) for token in _tokens(name))
    )


def _role_word(token: str) -> bool:
    return token.isalpha() and token not in PARTICLES and _vocabulary(token)


def _auditing_firm(pair: _Pair) -> bool:
    """Statutory auditors named as "X e Y" are partnerships (SROC), not persons."""
    return bool(AUDITOR_ROLE.search(fold(f"{pair.cargo} {pair.block.organ}"))) and bool(
        re.search(r"\s(?:e|&)\s", pair.cell.text)
    )


def _supersede(rows: list[BoardRow]) -> list[BoardRow]:
    """A mandate followed by a later one of the same organ in the document has ended."""
    latest: dict[str, int] = {}
    for row in rows:
        if row.years is not None:
            organ = fold(row.organ)
            latest[organ] = max(latest.get(organ, row.years[0]), row.years[0])
    result: list[BoardRow] = []
    for row in rows:
        superseded = row.years is not None and latest.get(fold(row.organ), 0) > row.years[1]
        result.append(replace(row, ended=True) if superseded else row)
    return result


def parse_pages(pages: Sequence[str]) -> ParsedDocument:
    """Board rows of one governance document from its extracted page texts."""
    parser = _TableParser()
    parser.parse(pages)
    return parser.finish()


# PDF text ---------------------------------------------------------------------------


@contextmanager
def _quiet_pypdf() -> Iterator[None]:
    logger = logging.getLogger("pypdf")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(level)


def pdf_text(content: bytes, *, layout: bool) -> tuple[str, ...]:
    """Page texts (bounded page count); an unreadable page reads as empty."""
    with _quiet_pypdf():
        try:
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted:
                raise EtfError("Documento ETF cifrado.")
            pages = list(reader.pages[:MAX_PDF_PAGES])
        except EtfError:
            raise
        except Exception as exc:  # pypdf raises many types on malformed input
            raise EtfError("Documento ETF ilegível.") from exc
        texts: list[str] = []
        for page in pages:
            try:
                texts.append(page.extract_text(extraction_mode="layout" if layout else "plain"))
            except Exception:  # one malformed page must not hide the others
                texts.append("")
        return tuple(texts)


def read_document(content: bytes) -> ParsedDocument:
    """The better reading of layout and plain text: more members, then fewer skipped tables."""
    layout = parse_pages(pdf_text(content, layout=True))
    plain = parse_pages(pdf_text(content, layout=False))
    return max(
        (layout, plain),
        key=lambda parsed: (len(parsed.rows), -parsed.unparseable, parsed is layout),
    )


# Site -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Company:
    slug: str
    name: str
    document_url: str
    rejected_link: bool = False


def _allowed(url: str) -> bool:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != HOST
        or parts.port is not None
        or parts.query
        or parts.fragment
    ):
        return False
    path = parts.path
    return (
        path == INDEX_PATH
        or (path.count("/") == 1 and bool(SLUG.fullmatch(path[1:])) and len(path) <= 121)
        or bool(DOCUMENT_PATH.fullmatch(path))
    )


class _Links(HTMLParser):
    """Anchors (href, text); ``tables_only`` keeps those inside ``<table>`` elements."""

    def __init__(self, *, tables_only: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.tables_only = tables_only
        self.depth = 0
        self.href: str | None = None
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.depth += 1
        elif tag == "a" and (self.depth or not self.tables_only):
            self.href = dict(attrs).get("href") or ""
            self.text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self.depth = max(0, self.depth - 1)
        elif tag == "a" and self.href is not None:
            self.links.append((self.href, _spaces("".join(self.text))))
            self.href = None

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.text.append(data)


def _html(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EtfError("Página ETF com codificação inesperada.") from exc


def parse_index(content: bytes) -> tuple[tuple[str, str], ...]:
    """(slug, company name as published) of every company linked from the list tables."""
    parser = _Links(tables_only=True)
    parser.feed(_html(content))
    parser.close()
    companies: dict[str, str] = {}
    for href, name in parser.links:
        found = re.fullmatch(r"/([a-z0-9-]{1,120})", href.strip())
        if found and SLUG.fullmatch(found.group(1)) and name:
            companies.setdefault(found.group(1), name[:300])
    if not companies or len(companies) > MAX_COMPANIES:
        raise EtfError("A lista de empresas da ETF mudou de formato; reveja o importador.")
    return tuple(companies.items())


def governance_link(content: bytes) -> tuple[str, bool]:
    """(PDF URL, rejected) of the "Modelo de governo" document linked from a company page."""
    parser = _Links(tables_only=False)
    parser.feed(_html(content))
    parser.close()
    for href, text in parser.links:
        if "modelo de governo" not in fold(text):
            continue
        parts = urlsplit(urljoin(f"{SITE}/", href.strip()))
        # The site's own absolute links may still use plain http.
        scheme = "https" if parts.scheme == "http" and parts.hostname == HOST else parts.scheme
        path = quote(unquote(parts.path), safe="/")
        url = f"{scheme}://{parts.netloc}{path}"
        if parts.query or parts.fragment or not _allowed(url) or not DOCUMENT_PATH.fullmatch(path):
            return "", True
        return url, False
    return "", False


class _Client:
    def __init__(self) -> None:
        self.requests = 0

    def get(self, url: str, *, max_bytes: int, timeout: int) -> bytes:
        if self.requests:
            time.sleep(PAUSE)
        self.requests += 1
        try:
            return download(
                url,
                allowed=_allowed,
                max_bytes=max_bytes,
                deadline=time.monotonic() + timeout,
                headers={"Accept": "text/html,application/pdf"},
            )
        except OfficialHTTPError as exc:
            raise EtfError("Não foi possível consultar a ETF em segurança.") from exc


@dataclass(frozen=True)
class EtfSnapshot:
    as_of: date
    retrieved_at: datetime
    companies: tuple[Company, ...]
    documents: dict[str, ParsedDocument]
    listed: int
    unreadable: int

    @property
    def complete(self) -> bool:
        return len(self.companies) == self.listed and not self.unreadable


def fetch_snapshot(*, as_of: date, limit: int | None = None) -> EtfSnapshot:
    """The company list in full; ``limit`` bounds the company pages and documents read."""
    if as_of > timezone.localdate():
        raise EtfError("Não é possível consultar a ETF numa data futura.")
    client = _Client()
    retrieved_at = timezone.now()
    listed = parse_index(client.get(INDEX_URL, max_bytes=INDEX_BYTES, timeout=PAGE_TIMEOUT))
    selected = listed[:limit] if limit is not None else listed
    companies: list[Company] = []
    documents: dict[str, ParsedDocument] = {}
    unreadable = 0
    for slug, name in selected:
        page = client.get(f"{SITE}/{slug}", max_bytes=PAGE_BYTES, timeout=PAGE_TIMEOUT)
        url, rejected = governance_link(page)
        companies.append(Company(slug=slug, name=name, document_url=url, rejected_link=rejected))
        del page
        if not url:
            continue
        content = client.get(url, max_bytes=PDF_BYTES, timeout=PDF_TIMEOUT)
        try:
            documents[slug] = read_document(content)
        except EtfError:
            unreadable += 1
        del content
    return EtfSnapshot(
        as_of=as_of,
        retrieved_at=retrieved_at,
        companies=tuple(companies),
        documents=documents,
        listed=len(listed),
        unreadable=unreadable,
    )


# Claims -----------------------------------------------------------------------------


def _passage(company: Company, row: BoardRow) -> str:
    organ = f" ({row.organ})" if row.organ else ""
    text = f"{row.name} — {row.cargo}{organ} em {company.name}."
    if row.years is not None:
        text += f" Mandato {row.years[0]}-{row.years[1]}."
    if row.act:
        text += f" Designação: {row.act}."
    elif row.appointed_on is not None:
        text += f" Eleição/nomeação: {row.appointed_on.isoformat()}."
    if row.resigned_on is not None:
        text += f" Renúncia ou cessação com efeitos a {row.resigned_on.isoformat()}."
    return text + " Fonte: Modelo de governo/Membros dos órgãos sociais (ETF)."


def _reference(company: Company, row: BoardRow) -> str:
    """Page, organ table and row within it: stable when other organs change."""
    tail = f" / linha {row.row}"
    head = f"ETF {company.slug} / p. {row.page}"
    organ = f" / {row.organ}" if row.organ else ""
    return head[: 160 - len(tail)] + organ[: max(0, 160 - len(tail) - len(head))] + tail


def observation(snapshot: EtfSnapshot, company: Company, row: BoardRow) -> ObservationInput:
    start = date(row.years[0], 1, 1) if row.years else None
    end = date(row.years[1], 12, 31) if row.years else None
    end_precision = DatePrecision.YEAR
    if row.resigned_on is not None and (start is None or row.resigned_on >= start):
        end, end_precision = row.resigned_on, DatePrecision.DAY
    role = f"{row.cargo} — {row.organ}" if row.organ else row.cargo
    return revised(
        ObservationInput(
            external_id=f"linha:{row.key}",
            revision="",
            category=OFFICE_HOLDING,
            passage=_passage(company, row),
            source_url=company.document_url,
            publisher=DATASET.publisher,
            reference=_reference(company, row),
            title=DATASET.title,
            identity=None,
            subject_name=row.name,
            subject_reference=f"etf:{company.slug}:{row.key}",
            effective_start=start,
            effective_end=end,
            object=None,
            object_name=company.name,
            object_identifier=f"etf:{company.slug}"[:80],
            kind=DIRECTORSHIP,
            dataset=DATASET.key,
            role=role[:240],
            role_class=role_class(row.cargo),
            start_precision=DatePrecision.YEAR,
            end_precision=end_precision,
            temporal_status=TemporalStatus.ENDED if row.ended else TemporalStatus.UNKNOWN,
            retrieved_at=snapshot.retrieved_at,
        )
    )


def scoped_claims(snapshot: EtfSnapshot) -> dict[str, tuple[ObservationInput, ...]]:
    """One complete scope per company with at least one parsed member."""
    scopes: dict[str, tuple[ObservationInput, ...]] = {}
    for company in snapshot.companies:
        document = snapshot.documents.get(company.slug)
        if document is not None and document.rows:
            scopes[f"{PREFIX}{company.slug}"] = tuple(
                observation(snapshot, company, row) for row in document.rows
            )
    return scopes


def summarise(snapshot: EtfSnapshot) -> dict[str, int]:
    """Read-only counts for a dry run; no payloads or personal data."""
    documents = snapshot.documents.values()
    return {
        "listed": snapshot.listed,
        "companies": len(snapshot.companies),
        "documents": sum(1 for company in snapshot.companies if company.document_url),
        "rejected_links": sum(1 for company in snapshot.companies if company.rejected_link),
        "unreadable": snapshot.unreadable,
        "without_table": sum(1 for document in documents if not document.has_table),
        "tables": sum(document.tables for document in documents),
        "unparseable": sum(document.unparseable for document in documents),
        "members": sum(len(document.rows) for document in documents),
        "firms": sum(document.firms for document in documents),
        "vacant": sum(document.vacant for document in documents),
        "duplicates": sum(document.duplicates for document in documents),
        "ended": sum(1 for document in documents for row in document.rows if row.ended),
    }


def apply_snapshot(snapshot: EtfSnapshot) -> dict[str, int]:
    """Atomic: board-member candidates per company; a partial run ceases nothing unseen."""
    with import_transaction():
        scopes = scoped_claims(snapshot)
        if snapshot.complete:
            return sync_scoped_snapshot(
                source=EnrichmentSource.ETF, prefix=PREFIX, snapshots=scopes, as_of=snapshot.as_of
            )
        result: dict[str, int] = {}
        for scope, items in scopes.items():
            for key, value in sync_observations(
                source=EnrichmentSource.ETF, scope=scope, observations=items, as_of=snapshot.as_of
            ).items():
                result[key] = result.get(key, 0) + value
        return result
