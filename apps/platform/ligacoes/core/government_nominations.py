"""Gabinete staff nominations published per Government (portugal.gov.pt «Nomeações»).

Only chiefs of staff, advisers (adjuntos) and specialists (técnicos especialistas) are
kept. Pay columns are never read. Staff have no official person id, so every row is a
name-only candidate for editorial review; the gabinete itself is an official
organisation, linked to its composition portfolio only when the heading names exactly one.
Pages list everyone ever appointed and never a cessation, so no end date is inferred.
"""

import hashlib
import json
import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import UUID

from django.utils import timezone
from django.utils.text import slugify

from .catalogue import DATASETS
from .enrichment import (
    GOVERNMENT_OFFICE,
    OFFICE_HOLDING,
    ORGANISATION_STRUCTURE,
    ObservationInput,
    sync_observations,
    sync_scoped_snapshot,
)
from .government import (
    GOVERNMENT,
    MAX_BYTES,
    ORIGIN,
    SLUG,
    GovernmentImportError,
    JSONObject,
    JSONValue,
    _Bootstrap,
    _field,
    _guid,
    _interval,
    _json,
    _object,
    _text,
    government_identity,
    government_term,
    revised,
    validate_government,
    validate_url,
)
from .identity import normalise_name, official_entity
from .models import (
    Entity,
    Relationship,
    SourceObservation,
    TemporalStatus,
    Term,
    import_transaction,
)
from .official_http import OfficialHTTPError, download

NOMINATIONS = DATASETS["gov_nomeacoes"]
MAX_REQUESTS = 60
MAX_PAGES = 40
MAX_ROWS = 3000
MAX_CONTENT = 1024 * 1024
TOTAL_TIMEOUT = 300
APPOINTMENT_PAGE = "Appointment Page"
# Informational subpages (e.g. applicable legislation) use the generic template.
INFORMATION_PAGE = "Page"
# Only these roles are kept; keys are accent-free, casefolded published labels.
ROLE_LABELS = {
    "chefe de gabinete": "Chefe de Gabinete",
    "chefe do gabinete": "Chefe de Gabinete",
    "adjunto": "Adjunto",
    "adjunta": "Adjunta",
    "tecnico especialista": "Técnico Especialista",
    "tecnica especialista": "Técnica Especialista",
    "tecnica especilaista": "Técnica Especialista",
}
# Columns 2 and 3 carry pay; the parser never records their content.
PAY_COLUMNS = frozenset({2, 3})
DR_DETAIL = re.compile(
    r"https://(?:www\.)?(?:diariodarepublica\.pt/dr|dre\.pt/dre)/(?:detalhe|analise-juridica)/"
    r"[a-z]+(?:-[a-z]+)*/[0-9a-z]+(?:-[0-9a-z]+)*?-(?P<id>[0-9]{5,12})"
)
KEY_STOPWORDS = frozenset({"a", "o", "e", "de", "do", "da", "dos", "das", "para"})
ROLE_WORDS = frozenset({"ministro", "ministra", "secretario", "secretaria", "estado"})


class NominationsImportError(GovernmentImportError):
    """Unexpected nominations layout or route; nothing is written."""


def validate_nominations_url(url: str, government: str) -> bool:
    """The nominations index page and its Next.js subpage data routes only."""
    validate_government(government)
    if len(url) > 2048 or any(ord(c) < 33 for c in url) or "\\" in url:
        return False
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.netloc != "portugal.gov.pt" or parsed.fragment:
        return False
    if parsed.path == f"/{government}/governo/nomeacoes":
        return not parsed.query
    if not re.fullmatch(
        rf"/_next/data/[A-Za-z0-9_-]{{1,80}}/{government}/governo/nomeacoes/{SLUG}\.json",
        parsed.path,
    ):
        return False
    return not parsed.query or (
        set(query) == {"dpl"}
        and len(query["dpl"]) == 1
        and bool(re.fullmatch(r"dpl_[A-Za-z0-9_-]{8,100}", query["dpl"][0]))
    )


class _Fetcher:
    def __init__(self, government: str) -> None:
        self.government = government
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.requests = 0

    def fetch(self, url: str) -> bytes:
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise NominationsImportError("Limite de pedidos de nomeações excedido.")
        try:
            return download(
                url,
                allowed=lambda target: validate_nominations_url(target, self.government),
                max_bytes=MAX_BYTES,
                deadline=self.deadline,
            )
        except OfficialHTTPError as exc:
            raise NominationsImportError(str(exc)) from exc


@dataclass(frozen=True)
class NominationsIndex:
    government: str
    government_id: str
    government_name: str
    start_date: date
    end_date: date | None
    build_id: str
    deployment: str
    # (subpage id, slug, navigation title)
    pages: tuple[tuple[str, str, str], ...]


def _menus(value: JSONValue, prefix: str) -> list[list[JSONObject]]:
    found: list[list[JSONObject]] = []
    if isinstance(value, dict):
        items = value.get("fields")
        if (
            value.get("componentName") == "Menu"
            and isinstance(items, list)
            and items
            and all(
                isinstance(item, dict)
                and isinstance(item.get("Href"), str)
                and re.fullmatch(rf"{prefix}{SLUG}", str(item.get("Href")))
                for item in items
            )
        ):
            found.append([_object(item) for item in items])
        for child in value.values():
            found.extend(_menus(child, prefix))
    elif isinstance(value, list):
        for child in value:
            found.extend(_menus(child, prefix))
    return found


def _site(data: JSONObject) -> JSONObject:
    return _object(_object(data.get("layoutData")).get("sitecore"))


def parse_index(content: bytes, *, government: str) -> NominationsIndex:
    validate_government(government)
    parser = _Bootstrap()
    try:
        parser.feed(content.decode("utf-8"))
    except (UnicodeError, RecursionError) as exc:
        raise NominationsImportError("Página de nomeações inválida.") from exc
    if len(parser.payloads) != 1:
        raise NominationsImportError("Configuração Next.js ausente ou ambígua.")
    data = _json(parser.payloads[0].encode())
    site = _site(_object(_object(data.get("props")).get("pageProps")))
    route = _object(site.get("route"))
    if route.get("templateName") != "Appointments Page":
        raise NominationsImportError("Página de nomeações desconhecida.")
    context = _object(_object(site.get("context")).get("governmentContext"))
    start, end = _interval(context.get("startDate"), context.get("endDate"))
    menus = _menus(route, f"/pt/{government}/governo/nomeacoes/")
    if len(menus) != 1 or len(menus[0]) > MAX_PAGES:
        raise NominationsImportError("Lista de páginas de nomeações ausente ou ambígua.")
    pages = tuple(
        (
            _guid(item.get("Id")),
            _text(item.get("Href")).rsplit("/", 1)[1],
            _text(_field(item, "NavigationTitle")),
        )
        for item in menus[0]
    )
    if (
        len({page_id for page_id, _, _ in pages}) != len(pages)
        or len({slug for _, slug, _ in pages}) != len(pages)
        or any(len(slug) > 100 for _, slug, _ in pages)
    ):
        raise NominationsImportError("Páginas de nomeações repetidas ou inválidas.")
    app_urls = {urljoin(ORIGIN, src) for src in parser.scripts if "/pages/_app-" in src}
    if len(app_urls) != 1:
        raise NominationsImportError("Aplicação pública ausente ou ambígua.")
    app_url = app_urls.pop()
    validate_url(app_url, government)
    build = _text(data.get("buildId"), 80)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", build):
        raise NominationsImportError("Identificador Next.js inválido.")
    return NominationsIndex(
        government,
        _guid(context.get("governmentId")),
        _text(context.get("governmentName")),
        start,
        end,
        build,
        urlsplit(app_url).query,
        pages,
    )


@dataclass
class _Cell:
    header: bool
    text: str = ""
    links: tuple[str, ...] = ()


@dataclass
class _Table:
    heading: str
    rows: list[list[_Cell]]


class _Content(HTMLParser):
    """Headings and table cells of the rich text; pay cells are never recorded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self.heading: list[str] | None = None
        self.last_heading = ""
        self.table: _Table | None = None
        self.cell: _Cell | None = None
        self.column = 0
        self.skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.table is None and re.fullmatch(r"h[1-6]", tag):
            self.heading = []
        elif tag == "table":
            if self.table is not None:
                raise NominationsImportError("Tabela de nomeações encaixada.")
            if not self.last_heading:
                raise NominationsImportError("Tabela de nomeações sem gabinete identificado.")
            self.table = _Table(self.last_heading, [])
            self.tables.append(self.table)
            # A heading names the next table only.
            self.last_heading = ""
        elif self.table is not None and tag == "tr":
            self.table.rows.append([])
            self.column = 0
        elif self.table is not None and tag in {"td", "th"}:
            if not self.table.rows or self.cell is not None:
                raise NominationsImportError("Tabela de nomeações malformada.")
            self.skip = tag == "td" and self.column in PAY_COLUMNS
            cell = _Cell(header=tag == "th")
            self.cell = cell
            self.table.rows[-1].append(cell)
            self.column += 1
        elif self.cell is not None and not self.skip:
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    self.cell.links += (href,)
            elif tag in {"br", "p", "div"}:
                self.cell.text += " "

    def handle_data(self, data: str) -> None:
        if self.cell is not None and not self.skip:
            self.cell.text += data
        elif self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.heading is not None and re.fullmatch(r"h[1-6]", tag):
            text = " ".join("".join(self.heading).split())
            # An empty heading may precede the real one: keep the last non-empty.
            if text:
                self.last_heading = text
            self.heading = None
        elif tag in {"td", "th"}:
            self.cell = None
            self.skip = False
        elif tag == "table":
            self.table = None


def _plain(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def _check_header(cells: list[_Cell]) -> None:
    labels = [_plain(cell.text) for cell in cells]
    if not (
        len(labels) == 6
        and labels[0] in {"funcao", "cargo"}
        and labels[1] == "nome"
        and all(label.startswith(("rendimento", "vencimento")) for label in labels[2:4])
        and labels[4] in {"data de nomeacao", "data da nomeacao"}
        and labels[5].startswith("publicacao")
    ):
        raise NominationsImportError("Cabeçalho de nomeações desconhecido.")


def _appointment_date(text: str) -> date | None:
    # Stray acute accents or quotes around the value are typing slips, not data.
    value = text.strip().strip("\u00b4`'\u2019").strip()
    if match := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", value):
        day, month, year = int(match[1]), int(match[2]), int(match[3])
    elif match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value):
        year, month, day = int(match[1]), int(match[2]), int(match[3])
    else:
        return None
    try:
        result = date(year, month, day)
    except ValueError:
        return None
    return result if result.year >= 1974 else None


def dr_document(href: str) -> tuple[str, str] | None:
    """(document id, link) for a Diário da República record page; else None."""
    candidate = href.strip()
    link, _, query = candidate.partition("?")
    match = DR_DETAIL.fullmatch(link)
    if match is None or (query and not re.fullmatch(r"_ts=[0-9]+", query)):
        return None
    return match["id"], link


def _name(text: str) -> str:
    # Footnote markers such as "(1)" or "*" refer to pay notes, not to the person.
    return " ".join(re.sub(r"\(\s*\d+\s*\)|\*", " ", text).split())


@dataclass(frozen=True)
class Nomination:
    heading: str
    role: str
    name: str
    appointed_on: date | None
    outside_term: bool
    dr_id: str
    dr_url: str
    fingerprint: str


@dataclass(frozen=True)
class NominationPage:
    page_id: str
    slug: str
    title: str
    url: str
    headings: tuple[str, ...]
    rows: tuple[Nomination, ...]
    dropped: int
    incomplete: int


@dataclass(frozen=True)
class NominationsSnapshot:
    government: str
    government_name: str
    start_date: date
    end_date: date | None
    as_of: date
    pages: tuple[NominationPage, ...]


def parse_page(
    payload: JSONObject, index: NominationsIndex, *, page_id: str, slug: str, title: str
) -> NominationPage | None:
    """Pure offline parser of one subpage; None for informational pages."""
    site = _site(_object(payload.get("pageProps")))
    route = _object(site.get("route"))
    context = _object(_object(site.get("context")).get("governmentContext"))
    if (
        _guid(route.get("itemId")) != page_id
        or _guid(context.get("governmentId")) != index.government_id
    ):
        raise NominationsImportError("Página de nomeações não corresponde ao índice.")
    field = _object(route.get("fields")).get("Content")
    raw = _object(field).get("value") if field is not None else ""
    template = route.get("templateName")
    if template == INFORMATION_PAGE and not (isinstance(raw, str) and "<table" in raw.casefold()):
        return None
    if template != APPOINTMENT_PAGE:
        raise NominationsImportError("Modelo de página de nomeações desconhecido.")
    content = _text(raw, MAX_CONTENT)
    parser = _Content()
    try:
        parser.feed(content)
        parser.close()
    except (RecursionError, AssertionError) as exc:
        raise NominationsImportError("Conteúdo de nomeações inválido.") from exc
    rows: dict[str, Nomination] = {}
    headings: list[str] = []
    dropped = incomplete = count = 0
    for table in parser.tables:
        if len(table.heading) > 240:
            raise NominationsImportError("Designação de gabinete demasiado extensa.")
        if table.heading not in headings:
            headings.append(table.heading)
        header = [row for row in table.rows if row and all(cell.header for cell in row)]
        body = [row for row in table.rows if row and not any(cell.header for cell in row)]
        if len(header) != 1 or len(header) + len(body) != len([r for r in table.rows if r]):
            raise NominationsImportError("Tabela de nomeações sem cabeçalho único.")
        _check_header(header[0])
        for cells in body:
            count += 1
            if len(cells) != 6 or count > MAX_ROWS:
                raise NominationsImportError("Linha de nomeações fora do formato conhecido.")
            role_text, name_text, date_text = (" ".join(cells[i].text.split()) for i in (0, 1, 4))
            role = ROLE_LABELS.get(_plain(role_text))
            if role is None:
                dropped += 1
                continue
            name = _name(name_text)
            # Never keep a name cell that carries digits (e.g. a stray tax number).
            if not name or len(name) > 300 or re.search(r"\d", name):
                incomplete += 1
                continue
            appointed_on = _appointment_date(date_text)
            documents = [doc for href in cells[5].links if (doc := dr_document(href))]
            dr_id, dr_url = documents[0] if documents else ("", "")
            outside = appointed_on is not None and (
                appointed_on < index.start_date
                or (index.end_date is not None and appointed_on >= index.end_date)
            )
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        index.government,
                        slug,
                        table.heading,
                        role,
                        name,
                        appointed_on.isoformat() if appointed_on else date_text,
                        sorted(href.strip() for href in cells[5].links),
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            # An identical repeated row states the same fact once.
            rows.setdefault(
                fingerprint,
                Nomination(
                    table.heading,
                    role,
                    name,
                    appointed_on,
                    outside,
                    dr_id,
                    dr_url,
                    fingerprint,
                ),
            )
    return NominationPage(
        page_id,
        slug,
        title,
        f"{ORIGIN}/pt/{index.government}/governo/nomeacoes/{slug}",
        tuple(headings),
        tuple(rows.values()),
        dropped,
        incomplete,
    )


def fetch_snapshot(*, government: str, as_of: date) -> NominationsSnapshot:
    validate_government(government)
    if as_of > timezone.localdate():
        raise NominationsImportError("Não é possível validar nomeações futuras.")
    fetcher = _Fetcher(government)
    index = parse_index(
        fetcher.fetch(f"{ORIGIN}/{government}/governo/nomeacoes"), government=government
    )
    if as_of < index.start_date:
        raise NominationsImportError("A data pedida é anterior à posse deste Governo.")
    pages: list[NominationPage] = []
    for page_id, slug, title in index.pages:
        url = f"{ORIGIN}/_next/data/{index.build_id}/{government}/governo/nomeacoes/{slug}.json"
        if index.deployment:
            url += "?" + index.deployment
        page = parse_page(_json(fetcher.fetch(url)), index, page_id=page_id, slug=slug, title=title)
        if page is not None:
            pages.append(page)
    return NominationsSnapshot(
        government, index.government_name, index.start_date, index.end_date, as_of, tuple(pages)
    )


def office_key(text: str) -> str | None:
    """Holder type plus distinctive title words of a gabinete heading or portfolio label.

    "Adjunto/a" stays a distinguishing word; role words and connectors do not count.
    """
    words = normalise_name(text).split()
    if words[:1] == ["gabinete"]:
        words = words[1:]
        if words[:1] and words[0] in {"do", "da", "dos", "das"}:
            words = words[1:]
    if words[:2] in (["primeiro", "ministro"], ["primeira", "ministra"]):
        titles = {"primeiro", "primeira"}
        rest = {w for w in words[2:] if w not in KEY_STOPWORDS | ROLE_WORDS | titles}
        return None if rest else "pm"
    if words[:1] in (["secretario"], ["secretaria"]) and words[1:3] == ["de", "estado"]:
        holder = "se"
    elif words[:1] in (["ministro"], ["ministra"]):
        holder = "min"
    else:
        return None
    rest = {"adjunto" if w == "adjunta" else w for w in words}
    return f"{holder}:" + " ".join(sorted(rest - KEY_STOPWORDS - ROLE_WORDS))


def portfolio_candidates(government: str) -> dict[str, list[tuple[Entity, str]]]:
    """Composition portfolios of one Government by office key (composition import first)."""
    titles: dict[UUID, str] = {}
    for entity_id, title in SourceObservation.objects.filter(
        source=GOVERNMENT,
        scope=f"government-structure:{government}",
        category=ORGANISATION_STRUCTURE,
        is_current=True,
        identity__isnull=False,
    ).values_list("identity__entity_id", "subject_name"):
        titles[entity_id] = title
    keys: dict[str, set[UUID]] = defaultdict(set)
    for object_id, role in SourceObservation.objects.filter(
        object_id__in=list(titles),
        source=GOVERNMENT,
        scope=f"government:{government}",
        category=GOVERNMENT_OFFICE,
        is_current=True,
    ).values_list("object_id", "role"):
        key = office_key(f"{role} {titles[object_id]}")
        if key:
            keys[key].add(object_id)
    entities = Entity.objects.in_bulk([pk for pks in keys.values() for pk in pks])
    return {
        key: [(entities[pk], titles[pk]) for pk in sorted(pks, key=str)]
        for key, pks in keys.items()
    }


def gabinete_id(government: str, heading: str) -> str:
    slug = slugify(heading)[:200]
    if not slug:
        raise NominationsImportError("Designação de gabinete sem texto identificável.")
    return f"gabinete:{government}:{slug}"


def _reference(government: str, page: NominationPage, row: Nomination) -> str:
    base = f"{government}/nomeacoes/{page.slug}"
    if not row.dr_id:
        return f"{base}; publicação em DR não identificada"
    reference = f"{base}; Diário da República {row.dr_url}"
    if len(reference) > 160:
        reference = f"{base}; Diário da República, doc. {row.dr_id}"
    return reference


def _staff(
    page: NominationPage,
    row: Nomination,
    *,
    snapshot: NominationsSnapshot,
    gabinete: Entity,
    term: Term,
) -> ObservationInput:
    passage = f"{row.name} — {row.role}, {row.heading} ({snapshot.government_name})."
    if row.appointed_on is None:
        passage += " Data de nomeação não indicada ou ilegível na fonte."
    else:
        passage += f" Data de nomeação: {row.appointed_on.isoformat()}."
    if row.outside_term:
        passage += " A data publicada está fora da vigência deste Governo."
    if row.dr_url:
        passage += f" Publicação em Diário da República: {row.dr_url}."
    return revised(
        ObservationInput(
            external_id=f"nomeacao:{row.fingerprint[:40]}",
            revision="",
            identity=None,
            subject_name=row.name,
            subject_reference=(
                f"dr:{row.dr_id}" if row.dr_id else f"nomeacao:{row.fingerprint[:32]}"
            ),
            category=OFFICE_HOLDING,
            passage=passage,
            source_url=page.url,
            publisher=NOMINATIONS.publisher,
            reference=_reference(snapshot.government, page, row),
            title=f"{NOMINATIONS.title} — {snapshot.government_name}",
            effective_start=row.appointed_on,
            object=gabinete,
            object_name=row.heading,
            kind=Relationship.Kind.PUBLIC_OFFICE,
            dataset=NOMINATIONS.key,
            role=row.role,
            role_class=Relationship.RoleClass.STAFF,
            term=None if row.outside_term else term,
            temporal_status=TemporalStatus.UNKNOWN,
        )
    )


def apply_snapshot(snapshot: NominationsSnapshot) -> dict[str, int]:
    """Gabinetes, their portfolio links and private staff candidates, atomically."""
    with import_transaction():
        term = government_term(
            snapshot.government, snapshot.government_name, snapshot.start_date, snapshot.end_date
        )
        candidates = portfolio_candidates(snapshot.government)
        gabinetes: dict[str, Entity] = {}
        structure: list[ObservationInput] = []
        mapping = {"linked": 0, "ambiguous": 0, "unmatched": 0}
        snapshots: dict[str, tuple[ObservationInput, ...]] = {}
        for page in snapshot.pages:
            for heading in page.headings:
                external_id = gabinete_id(snapshot.government, heading)
                if external_id in gabinetes:
                    continue
                gabinetes[external_id] = official_entity(
                    GOVERNMENT,
                    external_id,
                    name=heading,
                    kind=Entity.Kind.ORGANISATION,
                    classification=Entity.Classification.GOVERNMENT_OFFICE,
                )
                key = office_key(heading)
                matches = candidates.get(key, []) if key else []
                if len(matches) != 1:
                    mapping["ambiguous" if matches else "unmatched"] += 1
                    continue
                mapping["linked"] += 1
                portfolio, title = matches[0]
                structure.append(
                    revised(
                        ObservationInput(
                            external_id=external_id,
                            revision="",
                            identity=government_identity(external_id),
                            subject_name=heading,
                            category=ORGANISATION_STRUCTURE,
                            passage=(
                                f"{heading} — gabinete da pasta «{title}» do "
                                f"{snapshot.government_name}."
                            ),
                            source_url=page.url,
                            publisher=NOMINATIONS.publisher,
                            reference=f"{snapshot.government}/nomeacoes/{page.slug}; gabinete",
                            title=f"{NOMINATIONS.title} — {snapshot.government_name}",
                            object=portfolio,
                            object_name=title,
                            kind=Relationship.Kind.PART_OF,
                            dataset=NOMINATIONS.key,
                            term=term,
                        )
                    )
                )
            snapshots[f"nominations:{snapshot.government}:{page.slug}"] = tuple(
                _staff(
                    page,
                    row,
                    snapshot=snapshot,
                    gabinete=gabinetes[gabinete_id(snapshot.government, row.heading)],
                    term=term,
                )
                for row in page.rows
            )
        result = sync_scoped_snapshot(
            source=GOVERNMENT,
            prefix=f"nominations:{snapshot.government}:",
            snapshots=snapshots,
            as_of=snapshot.as_of,
        )
        parts = sync_observations(
            source=GOVERNMENT,
            scope=f"nominations-structure:{snapshot.government}",
            observations=tuple(structure),
            as_of=snapshot.as_of,
        )
        result.update({f"structure_{key}": value for key, value in parts.items()})
        result.update(mapping)
        result["gabinetes"] = len(gabinetes)
        return result
