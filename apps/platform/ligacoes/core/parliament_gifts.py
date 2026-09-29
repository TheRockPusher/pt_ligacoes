"""AR register of gifts, travel and hospitality offered to deputies (``RDH.aspx``).

Per legislature the official list of deputies is enumerated through its search form (20
rows per page) and every deputy's register page is parsed with the stdlib HTML parser.
The deputy is the recipient (AR ``DepCadId``); the provider is kept as published and
unresolved, so those events stay private until an editor resolves it. The deputies' party
column on the list page is never read.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from .events import EventInput, PartyInput, sync_events
from .identity import ar_person
from .models import Event, EventParty, import_transaction
from .official_http import OfficialHTTPError, download

DATASET = "ar_ofertas_hospitalidades"
HOST = "www.parlamento.pt"
LIST_PATH = "/RegistoDeslocacoesHospitalidades/Paginas/ROfertasDHList.aspx"
DETAIL_PATH = "/RegistoDeslocacoesHospitalidades/Paginas/RDH.aspx"
LIST_URL = f"https://{HOST}{LIST_PATH}"
# The register is published from the XIV Legislatura onwards.
LEGISLATURES = ("XIV", "XV", "XVI", "XVII")
PAGE_SIZE = 20
MAX_PAGES = 50
MAX_BYTES = 4 * 1024 * 1024
REQUEST_SECONDS = 60
TOTAL_SECONDS = 30 * 60
SPACING = 1.0
# Attempts per request; retries wait BACKOFF, then double (5 s, 10 s, 20 s).
RETRIES = 4
BACKOFF = 5.0
TITLE_LIMIT = 500
NAME_LIMIT = 300
DETAIL_LIMIT = 240
# Repeater id suffix → (event kind, section label); the control prefix differs per page.
SECTIONS = {
    "rptOfertas": (Event.Kind.GIFT, "ofertas"),
    "rptHDeslocacoes": (Event.Kind.TRAVEL, "deslocacoes"),
    "rptHospitalidades": (Event.Kind.HOSPITALITY, "hospitalidades"),
}
FIELDS = {
    "rptOfertas": frozenset({"Descricao", "Valor", "Ofertante", "Data", "Decisao"}),
    "rptHDeslocacoes": frozenset(
        {"Descricao", "Local", "Ofertante", "Representacao", "Data", "Duracao"}
    ),
    "rptHospitalidades": frozenset({"Descricao", "Local", "Ofertante", "Data", "Duracao"}),
}
# Published field → details key (the gift's final destination is the AR's decision).
DETAILS = {
    "Local": "local",
    "Duracao": "duracao",
    "Representacao": "representacao",
    "Decisao": "destino_final",
}
ROW_ID = re.compile(r"_ctl00_(rpt\w+?)_ctl(\d+)_lbl(\w+)$")
PLAIN_AMOUNT = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s*(?:€|EUR)?")
DAY = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
RESULTS = re.compile(r"Deputados na (\w+) Legislatura.*\[(\d+) registo\(s\)\]")


class GiftsImportError(ValueError):
    """An incomplete, ambiguous or unsafe register snapshot must not reach the database."""


@dataclass(frozen=True)
class Deputy:
    cadastro_id: str
    name: str


@dataclass(frozen=True)
class Entry:
    record_id: str
    kind: str
    title: str
    date: date | None
    provider: str
    amount: Decimal | None
    details: dict[str, str]
    record_url: str


@dataclass(frozen=True)
class RegisterPage:
    deputy: Deputy
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class GiftsSnapshot:
    legislature: str
    as_of: date
    pages: tuple[RegisterPage, ...]


def validate_legislature(legislature: str) -> None:
    if legislature not in LEGISLATURES:
        raise GiftsImportError("The register covers only the XIV\u2013XVII legislatures.")


def detail_url(cadastro_id: str, legislature: str) -> str:
    return f"https://{HOST}{DETAIL_PATH}?{urlencode({'BID': cadastro_id, 'lg': legislature})}"


def allowed(url: str) -> bool:
    """Exactly the list page and the register pages of the four published legislatures."""
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=bool(parsed.query))
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.netloc != HOST or parsed.fragment:
        return False
    if parsed.path == LIST_PATH:
        return not parsed.query
    return (
        parsed.path == DETAIL_PATH
        and set(query) == {"BID", "lg"}
        and len(query["BID"]) == 1
        and re.fullmatch(r"[1-9][0-9]{0,9}", query["BID"][0]) is not None
        and query["lg"] in ([code] for code in LEGISLATURES)
    )


def _text(value: str) -> str:
    return " ".join(value.split())


def parse_day(value: str) -> date | None:
    """Register dates are ``DD/MM/YYYY``; empty or ``-`` means not indicated."""
    text = _text(value)
    if text in ("", "-"):
        return None
    match = DAY.fullmatch(text)
    if match is None:
        raise GiftsImportError("Invalid register date.")
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise GiftsImportError("Invalid register date.") from exc


def plain_amount(value: str) -> Decimal | None:
    """A declared value written as a plain euro number (``1.250,50 €``), else ``None``."""
    match = PLAIN_AMOUNT.fullmatch(_text(value))
    if match is None:
        return None
    units, cents = match.groups()
    return Decimal(f"{units.replace('.', '')}.{(cents or '0').ljust(2, '0')}")


class _ListPage(HTMLParser):
    """Form fields, deputy links and pager postback targets of the list page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.registers: list[tuple[str, str]] = []
        self.pager: dict[str, str] = {}
        self.results = ""
        self._link: tuple[str, str] | None = None
        self._label = ""
        self._span: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "input" and values.get("name") and values.get("type") == "hidden":
            self.fields[values["name"]] = values.get("value", "")
        elif tag in ("select", "input") and values.get("name"):
            self.fields.setdefault(values["name"], "")
        elif tag == "a":
            href = values.get("href", "")
            parsed = urlsplit(href)
            query = parse_qs(parsed.query)
            if parsed.path.endswith("/DeputadoGP/Paginas/Biografia.aspx") and query.get("BID"):
                self._link, self._label = ("name", query["BID"][0]), ""
            elif parsed.path.endswith(DETAIL_PATH) and query.get("BID") and query.get("lg"):
                self.registers.append((query["BID"][0], query["lg"][0]))
            else:
                target = re.fullmatch(r"javascript:__doPostBack\('([^']+)',''\)", href)
                if target and "dpgResultsByDate" in target.group(1):
                    self._link, self._label = ("page", target.group(1)), ""
        elif tag == "span" and values.get("id", "").endswith("_lblResultsByDate"):
            self._span, self.results = "results", ""

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._label += data
        if self._span is not None:
            self.results += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            kind, value = self._link
            if kind == "name":
                self.names[value] = _text(self._label)
            else:
                self.pager[_text(self._label)] = value
            self._link = None
        elif tag == "span":
            self._span = None


class _RegisterPage(HTMLParser):
    """Repeater label values of one deputy's register page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[tuple[str, int], dict[str, str]] = {}
        self.titled = False
        self._field: tuple[str, int, str] | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "span":
            return
        identifier = dict(attrs).get("id") or ""
        if identifier.endswith("_ctl00_onome"):
            self.titled = True
        match = ROW_ID.search(identifier)
        if match:
            self._field, self._text = (match.group(1), int(match.group(2)), match.group(3)), ""

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._field is not None:
            section, row, name = self._field
            fields = self.values.setdefault((section, row), {})
            if name in fields:
                raise GiftsImportError("Repeated register field.")
            fields[name] = _text(self._text)
            self._field = None


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise GiftsImportError("Official register page is not UTF-8.") from exc


def parse_list(content: bytes, legislature: str) -> tuple[_ListPage, int]:
    page = _ListPage()
    page.feed(_decode(content))
    page.close()
    match = RESULTS.search(_text(page.results))
    if match is None or match.group(1) != legislature:
        raise GiftsImportError("The deputies list does not show the requested legislature.")
    if any(code != legislature for _, code in page.registers):
        raise GiftsImportError("The deputies list mixes legislatures.")
    return page, int(match.group(2))


def parse_register(content: bytes, deputy: Deputy, legislature: str) -> RegisterPage:
    """Every entry of one register page; an empty register is a valid, complete page."""
    page = _RegisterPage()
    page.feed(_decode(content))
    page.close()
    if not page.titled:
        raise GiftsImportError("Unexpected register page layout.")
    url = detail_url(deputy.cadastro_id, legislature)
    entries: list[Entry] = []
    occurrences: dict[str, int] = {}
    for (section, _row), fields in sorted(page.values.items()):
        if section not in SECTIONS:
            raise GiftsImportError("Unknown register section.")
        if not fields.keys() <= FIELDS[section] or "Descricao" not in fields:
            raise GiftsImportError("Unexpected register fields.")
        kind, label = SECTIONS[section]
        digest = hashlib.sha256(
            json.dumps(
                [deputy.cadastro_id, legislature, label, sorted(fields.items())],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()[:32]
        # Identical rows on one page are distinct declarations; number repeats in order.
        occurrences[digest] = occurrences.get(digest, 0) + 1
        record_id = f"rdh:{legislature}:{deputy.cadastro_id}:{digest}"
        if occurrences[digest] > 1:
            record_id += f":{occurrences[digest]}"
        details = {
            key: fields[name][:DETAIL_LIMIT]
            for name, key in DETAILS.items()
            if fields.get(name, "-") not in ("", "-")
        }
        declared = fields.get("Valor", "")
        amount = plain_amount(declared) if declared not in ("", "-") else None
        if declared not in ("", "-") and amount is None:
            details["valor"] = declared[:DETAIL_LIMIT]
        provider = fields.get("Ofertante", "")
        description = fields["Descricao"]
        entries.append(
            Entry(
                record_id=record_id,
                kind=kind,
                title=(
                    description
                    if len(description) <= TITLE_LIMIT
                    else description[: TITLE_LIMIT - 1].rstrip() + "…"
                )
                or Event.Kind(kind).label,
                date=parse_day(fields.get("Data", "")),
                provider="" if provider == "-" else provider[:NAME_LIMIT],
                amount=amount,
                details=details,
                record_url=url,
            )
        )
    return RegisterPage(deputy, tuple(entries))


class _Collector:
    """Polite sequential fetching: fixed routes, spacing, bounded retries.

    Under load the server drops connections, times out or answers with an incomplete page
    (no results label, no register title); each request is retried with back-off, and a
    page is accepted only once its parser validates it.
    """

    def __init__(
        self, fetch: Callable[..., bytes] = download, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._fetch = fetch
        self._sleep = sleep
        self._deadline = time.monotonic() + TOTAL_SECONDS
        self._first = True

    def get[T](
        self,
        url: str,
        parse: Callable[[bytes], T],
        *,
        step: str,
        form: dict[str, str] | None = None,
    ) -> T:
        failure = "unknown"
        for attempt in range(RETRIES):
            if not self._first:
                self._sleep(SPACING if attempt == 0 else BACKOFF * 2 ** (attempt - 1))
            self._first = False
            if time.monotonic() >= self._deadline:
                raise GiftsImportError("The official register exceeded the time limit.")
            try:
                content = self._fetch(
                    url,
                    allowed=allowed,
                    max_bytes=MAX_BYTES,
                    deadline=min(self._deadline, time.monotonic() + REQUEST_SECONDS),
                    **(
                        {
                            "method": "POST",
                            "body": urlencode(form).encode(),
                            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                        }
                        if form is not None
                        else {}
                    ),
                )
                if not content:
                    failure = "empty response"
                    continue
                return parse(content)
            except OfficialHTTPError as exc:
                # Payload-free: the status or the failure class only.
                failure = f"{type(exc.__cause__ or exc).__name__}: {exc}"
            except GiftsImportError as exc:
                failure = str(exc)
        raise GiftsImportError(
            f"Could not retrieve the official register ({step}; {RETRIES} attempts; {failure})."
        )


def _form(page: _ListPage, legislature: str, target: str = "") -> dict[str, str]:
    """Postback of every form field the page declares, with the legislature selected."""
    form = dict(page.fields)
    names = {name.rsplit("$", 1)[-1]: name for name in form}
    if not {"ddlLegislatura", "txtNome"} <= names.keys():
        raise GiftsImportError("Unexpected deputies list form.")
    for name in [name for name in form if name.endswith(("$btnDepActual", "$btnLimpar"))]:
        del form[name]
    form[names["ddlLegislatura"]] = legislature
    form[names["txtNome"]] = ""
    form["__EVENTTARGET"] = target
    form["__EVENTARGUMENT"] = ""
    if not target:
        prefix = names["ddlLegislatura"].rsplit("$", 1)[0]
        form[f"{prefix}$btnDepActual"] = "Pesquisar"
    return form


def _search_form(content: bytes) -> _ListPage:
    page = _ListPage()
    page.feed(_decode(content))
    page.close()
    names = {name.rsplit("$", 1)[-1] for name in page.fields}
    if not {"ddlLegislatura", "txtNome", "__VIEWSTATE"} <= names:
        raise GiftsImportError("Unexpected deputies list form.")
    return page


def enumerate_deputies(collector: _Collector, legislature: str) -> list[Deputy]:
    """The complete list for one legislature, following the pager to the last page."""
    form = collector.get(LIST_URL, _search_form, step="list form")
    page, total = collector.get(
        LIST_URL,
        lambda content: parse_list(content, legislature),
        step="list search",
        form=_form(form, legislature),
    )
    deputies: dict[str, Deputy] = {}
    for number in range(2, MAX_PAGES + 2):
        for cadastro_id, _ in page.registers:
            name = page.names.get(cadastro_id, "")
            if not re.fullmatch(r"[1-9][0-9]{0,9}", cadastro_id) or not name:
                raise GiftsImportError("Deputy row without an official id or name.")
            deputies[cadastro_id] = Deputy(cadastro_id, name[:NAME_LIMIT])
        target = page.pager.get(str(number))
        if target is None:
            break
        page, again = collector.get(
            LIST_URL,
            lambda content: parse_list(content, legislature),
            step=f"list page {number}",
            form=_form(page, legislature, target),
        )
        if again != total:
            raise GiftsImportError("The deputies list changed while paging.")
    else:
        raise GiftsImportError("The deputies list has too many pages.")
    if len(deputies) != total:
        raise GiftsImportError("The deputies list is incomplete.")
    return [deputies[key] for key in sorted(deputies, key=int)]


def fetch_snapshot(
    *, legislature: str, as_of: date, collector: _Collector | None = None
) -> GiftsSnapshot:
    validate_legislature(legislature)
    collector = collector or _Collector()
    pages = tuple(
        collector.get(
            detail_url(deputy.cadastro_id, legislature),
            lambda content, deputy=deputy: parse_register(content, deputy, legislature),
            step="register page",
        )
        for deputy in enumerate_deputies(collector, legislature)
    )
    return GiftsSnapshot(legislature=legislature, as_of=as_of, pages=pages)


def _events(snapshot: GiftsSnapshot) -> Iterator[EventInput]:
    for page in snapshot.pages:
        if not page.entries:
            continue
        recipient = ar_person(page.deputy.cadastro_id, page.deputy.name)
        for entry in page.entries:
            parties = [PartyInput(EventParty.Role.RECIPIENT, page.deputy.name, recipient)]
            if entry.provider:
                parties.append(PartyInput(EventParty.Role.PROVIDER, entry.provider))
            yield EventInput(
                record_id=entry.record_id,
                kind=entry.kind,
                title=entry.title,
                date=entry.date,
                amount=entry.amount,
                amount_label="Valor declarado" if entry.amount is not None else "",
                record_url=entry.record_url,
                details=entry.details,
                parties=tuple(parties),
            )


def apply_snapshot(snapshot: GiftsSnapshot) -> dict[str, int]:
    """Atomic: the legislature's complete register; absent entries cease."""
    validate_legislature(snapshot.legislature)
    with import_transaction():
        return sync_events(
            dataset=DATASET,
            scope=f"rdh:{snapshot.legislature}",
            events=_events(snapshot),
            as_of=snapshot.as_of,
        )
