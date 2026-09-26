"""Gated, bounded Sitecore composition collection; only office facts survive parsing.

The public frontend contract was inspected in September 2026. Build/context IDs and
all eight official-template selectors are discovered, never deployment constants.
The search total is a completeness check, not an assumed size of the Government.
"""

import hashlib
import http.client
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from itertools import pairwise
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import UUID

from django.utils import timezone

from .enrichment import (
    ObservationInput,
    get_source_identity,
    require_source_approval,
    sync_observations,
)
from .models import Entity, Relationship, editorial_transaction
from .official_http import open_connection

ORIGIN = "https://portugal.gov.pt"
EDGE = "https://edge-platform.sitecorecloud.io/v1/content/api/graphql/v1"
MAX_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_REQUESTS = 180
MAX_MEMBERS = 150
TOTAL_TIMEOUT = 300
TEMPLATE_NAMES = frozenset(
    {
        "prime-minister",
        "vice-prime-minister",
        "minister",
        "minister-of-state",
        "minister-in-the-cabinet",
        "secretary-of-state",
        "deputy-minister",
        "subsecretary-of-state",
    }
)
# These are schema template IDs, not people or deployment-specific identifiers.
AREA_TEMPLATES = (
    "e0eec2c3-d4a5-4563-bbf7-3f025d5e2117",
    "54a9a414-e8c5-47b8-aca3-d97beae5fbbd",
    "39da5e76-533f-4fa3-a66c-f22455ff3267",
    "e7c1d6db-9cfa-4565-8910-cc05f760592f",
)
MINISTRY_TEMPLATES = frozenset(AREA_TEMPLATES[2:])
type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None
type JSONObject = dict[str, JSONValue]


class GovernmentImportError(ValueError):
    """Unsafe, partial or ambiguous source material never reaches editorial storage."""


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise GovernmentImportError("Estrutura da fonte governamental inválida.")
    return value


def _rows(value: JSONValue, limit: int = MAX_MEMBERS) -> list[JSONObject]:
    if not isinstance(value, list) or len(value) > limit:
        raise GovernmentImportError("Lista governamental ausente ou demasiado extensa.")
    return [_object(item) for item in value]


def _text(value: JSONValue, limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise GovernmentImportError("Texto obrigatório da fonte inválido.")
    return value.strip()


def _field(row: JSONObject, name: str) -> JSONValue:
    return _object(row.get(name)).get("value")


def _guid(value: JSONValue) -> str:
    text = _text(value, 38)
    if not re.fullmatch(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F-]{36}|\{[0-9a-fA-F-]{36}\})", text):
        raise GovernmentImportError("Identificador oficial inválido.")
    try:
        result = UUID(text)
    except ValueError as exc:
        raise GovernmentImportError("Identificador oficial inválido.") from exc
    if result.int == 0:
        raise GovernmentImportError("Identificador oficial vazio.")
    return str(result)


def _date(value: JSONValue, *, optional: bool = False) -> date | None:
    if optional and value in (None, "", "00010101T000000Z", "0001-01-01T00:00:00Z"):
        return None
    text = _text(value, 20)
    if re.fullmatch(r"\d{8}T000000Z", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00Z", text):
        text = text[:10]
    else:
        raise GovernmentImportError("Data governamental ambígua.")
    try:
        result = date.fromisoformat(text)
    except ValueError as exc:
        raise GovernmentImportError("Data governamental inválida.") from exc
    if result.year < 1974:
        raise GovernmentImportError("Data governamental fora do âmbito.")
    return result


def _interval(start: JSONValue, end: JSONValue) -> tuple[date, date | None]:
    first, last = _date(start), _date(end, optional=True)
    if first is None or (last is not None and last < first):
        raise GovernmentImportError("Intervalo governamental inválido.")
    return first, last


def _active(start: date, end: date | None, as_of: date) -> bool:
    # Sitecore's search uses endDate > timeline: cessation is exclusive.
    return start <= as_of and (end is None or as_of < end)


def _pairs(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
    result: JSONObject = {}
    for key, value in pairs:
        if key in result:
            raise GovernmentImportError("Chave JSON repetida na fonte.")
        result[key] = value
    return result


def _json(content: bytes) -> JSONObject:
    if len(content) > MAX_BYTES:
        raise GovernmentImportError("Resposta governamental demasiado extensa.")
    try:
        return _object(json.loads(content, object_pairs_hook=_pairs))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GovernmentImportError("JSON governamental inválido.") from exc


def validate_government(government: str) -> None:
    if not re.fullmatch(r"gc[1-9][0-9]{0,2}", government):
        raise GovernmentImportError("Seleção de Governo inválida.")


def _profile_path(path: str, government: str) -> bool:
    return bool(
        re.fullmatch(
            rf"/{government}/(?:primeiro-ministro/acerca|area-de-governo/"
            r"[a-z0-9]+(?:-[a-z0-9]+)*/(?:ministro|secretarios-de-estado/"
            r"[a-z0-9]+(?:-[a-z0-9]+)*))",
            path,
        )
    )


def validate_url(url: str, government: str, *, method: str = "GET") -> None:
    """Only composition, discovered build assets/profiles and the exact public query route."""
    validate_government(government)
    if len(url) > 2048 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise GovernmentImportError("URL governamental inválido.")
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            raise ValueError
    except ValueError as exc:
        raise GovernmentImportError("URL governamental inválido.") from exc
    if any(len(v) != 1 for v in query.values()):
        raise GovernmentImportError("Parâmetros governamentais ambíguos.")
    if (
        method == "POST"
        and parsed.netloc == "edge-platform.sitecorecloud.io"
        and parsed.path == "/v1/content/api/graphql/v1"
        and set(query) == {"sitecoreContextId"}
        and re.fullmatch(r"[A-Za-z0-9_-]{12,80}", query["sitecoreContextId"][0])
    ):
        return
    if method != "GET" or parsed.netloc != "portugal.gov.pt":
        raise GovernmentImportError("Destino fora das rotas governamentais autorizadas.")
    if query and (
        set(query) != {"dpl"} or not re.fullmatch(r"dpl_[A-Za-z0-9_-]{8,100}", query["dpl"][0])
    ):
        raise GovernmentImportError("Parâmetros fora do contrato governamental.")
    if parsed.path == f"/{government}/governo/composicao" and not query:
        return
    if re.fullmatch(r"/_next/static/chunks/pages/_app-[a-f0-9]{8,64}\.js", parsed.path):
        return
    match = re.fullmatch(r"/_next/data/([A-Za-z0-9_-]{1,80})(/.+)\.json", parsed.path)
    if match and _profile_path(match[2], government):
        return
    raise GovernmentImportError("Destino fora das rotas governamentais autorizadas.")


class _Collector:
    def __init__(self, government: str) -> None:
        self.government = government
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.requests = 0
        self.bytes = 0

    def fetch(self, url: str, *, body: bytes | None = None) -> bytes:
        method = "POST" if body is not None else "GET"
        if body is not None and len(body) > 16000:
            raise GovernmentImportError("Consulta governamental demasiado extensa.")
        for _ in range(4):
            # Gate every request, including redirects and command-line dry runs.
            require_source_approval("government", scope="government_office")
            validate_url(url, self.government, method=method)
            self.requests += 1
            if self.requests > MAX_REQUESTS:
                raise GovernmentImportError("Limite de pedidos governamentais excedido.")
            parsed = urlsplit(url)
            try:
                with open_connection(parsed.hostname or "", deadline=self.deadline) as connection:
                    connection.request(
                        method,
                        parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                        body=body,
                        headers={
                            "User-Agent": "LigacoesPT-editorial-import/1",
                            "Accept-Encoding": "identity",
                            "Content-Type": "application/json"
                            if body is not None
                            else "text/plain",
                        },
                    )
                    with connection.getresponse() as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.getheader("Location")
                            if not location or (
                                body is not None and response.status not in {307, 308}
                            ):
                                raise GovernmentImportError(
                                    "Redirecionamento governamental inválido."
                                )
                            target = urljoin(url, location)
                            validate_url(target, self.government, method=method)
                            if urlsplit(target).netloc != parsed.netloc:
                                raise GovernmentImportError(
                                    "Redirecionamento entre domínios recusado."
                                )
                            url = target
                            continue
                        if response.status != 200:
                            raise GovernmentImportError(f"A fonte devolveu HTTP {response.status}.")
                        if response.getheader("Content-Encoding", "identity").lower() != "identity":
                            raise GovernmentImportError("Resposta comprimida recusada.")
                        size = response.getheader("Content-Length")
                        if size is not None and (not size.isdecimal() or int(size) > MAX_BYTES):
                            raise GovernmentImportError("Resposta governamental demasiado extensa.")
                        content = bytearray()
                        while True:
                            chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
                            if not chunk:
                                break
                            content.extend(chunk)
                            self.bytes += len(chunk)
                            if len(content) > MAX_BYTES or self.bytes > MAX_TOTAL_BYTES:
                                raise GovernmentImportError(
                                    "Limite de dados governamentais excedido."
                                )
                        if size is not None and len(content) != int(size):
                            raise GovernmentImportError("Resposta governamental truncada.")
                        return bytes(content)
            except (OSError, http.client.HTTPException) as exc:
                raise GovernmentImportError("Não foi possível obter a fonte em segurança.") from exc
        raise GovernmentImportError("Demasiados redirecionamentos governamentais.")


class _Bootstrap(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.payloads: list[str] = []
        self.recording = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attributes = dict(attrs)
        source = attributes.get("src")
        if source:
            self.scripts.append(source)
        if attributes.get("id") == "__NEXT_DATA__":
            self.payloads.append("")
            self.recording = True

    def handle_data(self, data: str) -> None:
        if self.recording:
            self.payloads[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.recording = False


def _components(value: JSONValue) -> list[JSONObject]:
    found: list[JSONObject] = []
    if isinstance(value, dict):
        if value.get("componentName") == "SearchResultsComposite":
            found.append(value)
        for child in value.values():
            found.extend(_components(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_components(child))
    return found


@dataclass(frozen=True)
class GovernmentConfig:
    government: str
    government_id: str
    prime_minister_id: str
    government_name: str
    start_date: date
    end_date: date | None
    templates: tuple[tuple[str, str], ...]
    build_id: str
    deployment: str
    app_url: str


def parse_bootstrap(content: bytes, *, government: str) -> GovernmentConfig:
    validate_government(government)
    parser = _Bootstrap()
    try:
        parser.feed(content.decode("utf-8"))
    except (UnicodeError, RecursionError) as exc:
        raise GovernmentImportError("Página governamental inválida.") from exc
    if len(parser.payloads) != 1:
        raise GovernmentImportError("Configuração Next.js ausente ou ambígua.")
    data = _json(parser.payloads[0].encode())
    site = _object(
        _object(_object(_object(data.get("props")).get("pageProps")).get("layoutData")).get(
            "sitecore"
        )
    )
    context = _object(_object(site.get("context")).get("governmentContext"))
    components = _components(site.get("route"))
    if len(components) != 1:
        raise GovernmentImportError("Configuração da composição ausente ou ambígua.")
    component = components[0]
    if _object(component.get("params")).get("SearchSignature") != "gc":
        raise GovernmentImportError("Contrato da composição desconhecido.")
    source = _object(_object(_object(component.get("fields")).get("data")).get("datasource"))
    roots = _rows(_object(source.get("SearchResultsRootItem")).get("jsonValue"))
    government_id = _guid(context.get("governmentId"))
    if (
        len(roots) != 1
        or _guid(roots[0].get("id")) != government_id
        or roots[0].get("name") != government
    ):
        raise GovernmentImportError("Raiz da composição não corresponde ao Governo.")
    selectors = _rows(_object(source.get("SearchResultsByTemplate")).get("jsonValue"))
    templates = tuple(
        (_text(row.get("name")), _guid(_field(_object(row.get("fields")), "Title")))
        for row in selectors
    )
    if (
        {name for name, _ in templates} != TEMPLATE_NAMES
        or len(templates) != len(TEMPLATE_NAMES)
        or len({identifier for _, identifier in templates}) != len(templates)
    ):
        raise GovernmentImportError("Seletores da composição incompletos ou desconhecidos.")
    app_urls = {urljoin(ORIGIN, src) for src in parser.scripts if "/pages/_app-" in src}
    if len(app_urls) != 1:
        raise GovernmentImportError("Aplicação pública ausente ou ambígua.")
    app_url = app_urls.pop()
    validate_url(app_url, government)
    build = _text(data.get("buildId"), 80)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", build):
        raise GovernmentImportError("Identificador Next.js inválido.")
    start, end = _interval(context.get("startDate"), context.get("endDate"))
    return GovernmentConfig(
        government,
        government_id,
        _guid(context.get("primeMinisterId")),
        _text(context.get("governmentName")),
        start,
        end,
        templates,
        build,
        urlsplit(app_url).query,
        app_url,
    )


def discover_context_id(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise GovernmentImportError("Configuração pública inválida.") from exc
    contexts = set(re.findall(r'SITECORE_EDGE_CONTEXT_ID\|\|"([A-Za-z0-9_-]{12,80})"', text))
    if len(contexts) != 1:
        raise GovernmentImportError("Contexto público Sitecore ausente ou ambíguo.")
    return contexts.pop()


def _search_query(config: GovernmentConfig, as_of: date) -> str:
    templates = ",".join(
        '{name:"_templates",value:' + json.dumps(identifier) + ",operator:EQ}"
        for _, identifier in config.templates
    )
    stamp = as_of.isoformat() + "T00:00:00Z"
    # All fields/operators below were exercised against the served frontend schema.
    return (
        "query SearchResults($language:String!,$first:Int,$cursor:String){childs:search("
        'where:{AND:[{name:"_path",value:' + json.dumps(config.government_id) + ",operator:EQ},"
        "{OR:[" + templates + ']},{name:"startDate",value:"' + stamp + '",operator:LTE},'
        '{OR:[{name:"endDate",value:"00010101T000000Z",operator:EQ},'
        '{name:"endDate",value:"' + stamp + '",operator:GT}]},'
        '{name:"isOfficialHidden",value:"true",operator:NEQ},'
        '{name:"_language",value:$language,operator:EQ}]},first:$first,after:$cursor,'
        'orderBy:{name:"sortOrder",direction:ASC}){total pageInfo{endCursor hasNext}'
        "results{id template{id name} url{path}"
        'isOfficialHidden:field(name:"IsOfficialHidden"){value}'
        'official:field(name:"Official"){jsonValue}'
        'startDate:field(name:"StartDate"){value} endDate:field(name:"EndDate"){value}'
        'governmentRole:field(name:"GovernmentRole"){value}'
        "ministryPage:ancestors(includeTemplateIDs:" + json.dumps(AREA_TEMPLATES) + ")"
        '{id template{id} governmentTitle:field(name:"GovernmentTitle"){value}'
        'governmentPreposition:field(name:"GovernmentPreposition"){value}}}}}'
    )


def _search_page(payload: JSONObject) -> tuple[int, bool, str, list[JSONObject]]:
    if payload.get("errors"):
        raise GovernmentImportError("A consulta governamental devolveu erros.")
    result = _object(_object(payload.get("data")).get("childs"))
    total = result.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or not 0 < total <= MAX_MEMBERS:
        raise GovernmentImportError("Total governamental ausente ou fora dos limites.")
    page = _object(result.get("pageInfo"))
    has_next = page.get("hasNext")
    if not isinstance(has_next, bool):
        raise GovernmentImportError("Paginação governamental ambígua.")
    cursor = _text(page.get("endCursor"), 2048) if has_next else ""
    return total, has_next, cursor, _rows(result.get("results"), 100)


@dataclass(frozen=True)
class GovernmentMember:
    official_id: str
    appointment_id: str
    name: str
    role: str
    portfolio_id: str
    portfolio: str
    area_id: str
    area: str
    start_date: date
    end_date: date | None
    source_url: str
    template: str


@dataclass(frozen=True)
class GovernmentSnapshot:
    government: str
    government_id: str
    government_name: str
    as_of: date
    members: tuple[GovernmentMember, ...]


def _member(row: JSONObject, config: GovernmentConfig, as_of: date) -> GovernmentMember:
    templates = {identifier: name for name, identifier in config.templates}
    template = templates.get(_guid(_object(row.get("template")).get("id")))
    if template is None or _field(row, "isOfficialHidden") not in ("", "0"):
        raise GovernmentImportError("Membro oculto ou categoria desconhecida.")
    official = _object(_object(row.get("official")).get("jsonValue"))
    start, end = _interval(_field(row, "startDate"), _field(row, "endDate"))
    if not _active(start, end, as_of):
        raise GovernmentImportError("A composição contém um mandato fora da data pedida.")
    appointment_id = _guid(row.get("id"))
    role = _text(_field(row, "governmentRole"))
    path = _text(_object(row.get("url")).get("path"), 1500)
    if not path.startswith(f"/pt/{config.government}/"):
        raise GovernmentImportError("Percurso do membro fora do Governo selecionado.")
    path = path.removeprefix("/pt")
    areas = _rows(row.get("ministryPage"), 4)
    if template == "prime-minister":
        if (
            appointment_id != config.prime_minister_id
            or path != f"/{config.government}/primeiro-ministro"
            or areas
        ):
            raise GovernmentImportError("Identidade do Primeiro-Ministro inconsistente.")
        portfolio_id, portfolio = appointment_id, role
        area_id, area = config.government_id, config.government_name
        path += "/acerca"
    else:
        if len(areas) not in {1, 2}:
            raise GovernmentImportError("Pasta governamental ausente ou ambígua.")
        for parent in areas:
            if _guid(_object(parent.get("template")).get("id")) not in AREA_TEMPLATES:
                raise GovernmentImportError("Tipo de pasta governamental desconhecido.")
        ministries = [
            p for p in areas if _guid(_object(p.get("template")).get("id")) in MINISTRY_TEMPLATES
        ]
        if len(ministries) != 1:
            raise GovernmentImportError("Área ministerial ausente ou ambígua.")
        portfolio_id = _guid(areas[0].get("id"))
        portfolio = _text(_field(areas[0], "governmentTitle"))
        area_id = _guid(ministries[0].get("id"))
        area = _text(_field(ministries[0], "governmentTitle"))
        if path.count("/Officials/") != 1:
            raise GovernmentImportError("Percurso do mandato inválido.")
        path = path.split("/Officials/")[0]
    if not _profile_path(path, config.government):
        raise GovernmentImportError("Percurso da pasta fora do contrato conhecido.")
    return GovernmentMember(
        _guid(official.get("id")),
        appointment_id,
        _text(_field(_object(official.get("fields")), "FullName")),
        role,
        portfolio_id,
        portfolio,
        area_id,
        area,
        start,
        end,
        ORIGIN + path,
        template,
    )


def _validate_profile(
    payload: JSONObject, member: GovernmentMember, config: GovernmentConfig, as_of: date
) -> None:
    site = _object(_object(_object(payload.get("pageProps")).get("layoutData")).get("sitecore"))
    context = _object(_object(site.get("context")).get("governmentContext"))
    fields = _object(_object(site.get("route")).get("fields"))
    if (
        _guid(context.get("governmentId")) != config.government_id
        or _guid(context.get("governmentAreaId")) != member.area_id
        or _text(context.get("governmentAreaTitle")) != member.area
    ):
        raise GovernmentImportError("Contexto da pasta não corresponde à composição.")
    if "IsEndedTerm" in fields and _field(fields, "IsEndedTerm") is not False:
        raise GovernmentImportError("Pasta governamental terminada ou ambígua.")
    history = _rows(fields.get("OfficialsHistory"))
    summaries = _rows(_object(context.get("officialInfo")).get("allOfficials"))
    if len(history) != len(summaries):
        raise GovernmentImportError("Histórico e resumo oficial incompletos.")
    seen: set[str] = set()
    intervals: list[tuple[date, date | None]] = []
    selected: list[str] = []
    for row in history:
        identifier = _guid(row.get("id"))
        if identifier in seen:
            raise GovernmentImportError("Mandato repetido no histórico oficial.")
        seen.add(identifier)
        data = _object(row.get("fields"))
        official = _object(data.get("Official"))
        start, end = _interval(_field(data, "StartDate"), _field(data, "EndDate"))
        matches = [s for s in summaries if _guid(s.get("itemId")) == identifier]
        if len(matches) != 1:
            raise GovernmentImportError("Resumo do mandato ausente ou ambíguo.")
        summary = matches[0]
        name = _text(_field(_object(official.get("fields")), "FullName"))
        role = _text(_field(data, "GovernmentRole"))
        person_id = _guid(official.get("id"))
        if (
            _guid(summary.get("officialId")) != person_id
            or summary.get("officialName") != name
            or summary.get("governmentRole") != role
            or _interval(summary.get("startDate"), summary.get("endDate")) != (start, end)
        ):
            raise GovernmentImportError("Histórico e resumo do mandato divergem.")
        if "IsOfficialHidden" in data and _field(data, "IsOfficialHidden") is not False:
            if _active(start, end, as_of):
                raise GovernmentImportError("Mandato atual oculto no histórico.")
            continue
        intervals.append((start, end))
        if _active(start, end, as_of):
            selected.append(identifier)
            if (identifier, person_id, name, role, start, end) != (
                member.appointment_id,
                member.official_id,
                member.name,
                member.role,
                member.start_date,
                member.end_date,
            ):
                raise GovernmentImportError("Histórico e composição atual divergem.")
    intervals.sort(key=lambda interval: interval[0])
    for previous, current in pairwise(intervals):
        if previous[1] is None or previous[1] > current[0]:
            raise GovernmentImportError("Mandatos sobrepostos na mesma pasta.")
    if selected != [member.appointment_id]:
        raise GovernmentImportError("Pasta sem um único mandato atual comprovado.")
    if member.template in {"secretary-of-state", "deputy-minister", "subsecretary-of-state"} and (
        _guid(_object(site.get("route")).get("itemId")) != member.portfolio_id
        or _text(_field(fields, "GovernmentTitle")) != member.portfolio
    ):
        raise GovernmentImportError("Identidade ou título da secretaria diverge.")


def parse_snapshot(
    config: GovernmentConfig,
    pages: tuple[JSONObject, ...],
    profiles: dict[str, JSONObject],
    *,
    as_of: date,
) -> GovernmentSnapshot:
    """Pure offline parser for captured wire shapes; fixtures must be fictional."""
    if not _active(config.start_date, config.end_date, as_of) or not pages or len(pages) > 2:
        raise GovernmentImportError("Composição fora da vigência ou sem páginas completas.")
    members: list[GovernmentMember] = []
    total: int | None = None
    cursors: set[str] = set()
    for index, payload in enumerate(pages):
        count, has_next, cursor, rows = _search_page(payload)
        if total is not None and count != total:
            raise GovernmentImportError("Total alterado durante a paginação governamental.")
        total = count
        if has_next != (index < len(pages) - 1) or (has_next and cursor in cursors):
            raise GovernmentImportError("Composição truncada ou paginação repetida.")
        cursors.add(cursor)
        members.extend(_member(row, config, as_of) for row in rows)
    if len(members) != total or len({m.appointment_id for m in members}) != total:
        raise GovernmentImportError("Contagem ou identificadores da composição inconsistentes.")
    if (
        len({m.portfolio_id for m in members}) != total
        or len({m.official_id for m in members}) != total
    ):
        raise GovernmentImportError("Identidade ou pasta repetida na composição.")
    if sum(m.template == "prime-minister" for m in members) != 1:
        raise GovernmentImportError("Composição sem um único Primeiro-Ministro.")
    expected_profiles = {m.source_url for m in members if m.template != "prime-minister"}
    if set(profiles) != expected_profiles:
        raise GovernmentImportError("Perfis da composição incompletos ou inesperados.")
    for member in members:
        # PM is itself the official appointment page; it has no OfficialsHistory
        # and /acerca is a separate content page, not a history-bearing profile.
        if member.template != "prime-minister":
            _validate_profile(profiles[member.source_url], member, config, as_of)
    return GovernmentSnapshot(
        config.government,
        config.government_id,
        config.government_name,
        as_of,
        tuple(sorted(members, key=lambda member: member.appointment_id)),
    )


def fetch_snapshot(*, government: str = "gc25", as_of: date) -> GovernmentSnapshot:
    require_source_approval("government", scope="government_office")
    validate_government(government)
    if as_of > timezone.localdate():
        raise GovernmentImportError("Não é possível validar uma composição futura.")
    collector = _Collector(government)
    config = parse_bootstrap(
        collector.fetch(f"{ORIGIN}/{government}/governo/composicao"), government=government
    )
    if not _active(config.start_date, config.end_date, as_of):
        raise GovernmentImportError("A data pedida não pertence à vigência deste Governo.")
    context_id = discover_context_id(collector.fetch(config.app_url))
    endpoint = EDGE + "?" + urlencode({"sitecoreContextId": context_id})
    query = _search_query(config, as_of)
    cursor: str | None = None
    pages: list[JSONObject] = []
    for _ in range(2):
        body = json.dumps(
            {
                "query": query,
                "variables": {"language": "pt", "first": 100, "cursor": cursor},
                "operationName": "SearchResults",
            }
        ).encode()
        payload = _json(collector.fetch(endpoint, body=body))
        pages.append(payload)
        _, has_next, cursor, _ = _search_page(payload)
        if not has_next:
            break
    profiles: dict[str, JSONObject] = {}
    for payload in pages:
        for row in _search_page(payload)[3]:
            member = _member(row, config, as_of)
            if member.template == "prime-minister":
                continue
            if member.source_url in profiles:
                raise GovernmentImportError("Pasta repetida na composição.")
            path = urlsplit(member.source_url).path
            url = f"{ORIGIN}/_next/data/{config.build_id}{path}.json"
            if config.deployment:
                url += "?" + config.deployment
            profiles[member.source_url] = _json(collector.fetch(url))
    return parse_snapshot(config, tuple(pages), profiles, as_of=as_of)


def apply_snapshot(snapshot: GovernmentSnapshot) -> dict[str, int]:
    """Atomic private identities and ordinary drafts; never publish or merge by name."""
    with editorial_transaction():
        observations: list[ObservationInput] = []
        for member in snapshot.members:
            person = get_source_identity(
                source="government",
                external_id=f"person:{member.official_id}",
                name=member.name,
            )
            portfolio = get_source_identity(
                source="government",
                external_id=f"portfolio:{member.portfolio_id}",
                name=member.portfolio,
                entity_kind=Entity.Kind.ORGANISATION,
            )
            projection = asdict(member)
            projection["government_id"] = snapshot.government_id
            projection["government_name"] = snapshot.government_name
            revision = hashlib.sha256(
                json.dumps(projection, sort_keys=True, ensure_ascii=False, default=str).encode()
            ).hexdigest()
            passage = (
                f"{member.name} — {member.role}; pasta: {member.portfolio}; "
                f"área: {member.area}. Início: {member.start_date.isoformat()}."
            )
            effective_end = None
            if member.end_date:
                # The source's exclusive day boundary becomes the domain's
                # inclusive last serving day, without losing the raw source date.
                effective_end = member.end_date - timedelta(days=1)
                passage += (
                    f" Cessação na fonte (limite exclusivo): {member.end_date.isoformat()}; "
                    f"último dia de exercício (limite inclusivo): {effective_end.isoformat()}."
                )
            observations.append(
                ObservationInput(
                    external_id=f"appointment:{member.appointment_id}",
                    revision=revision,
                    identity=person,
                    category="government_office",
                    passage=passage,
                    source_url=member.source_url,
                    publisher="Governo da República Portuguesa",
                    reference=f"Mandato {member.appointment_id}; pasta {member.portfolio_id}",
                    title=f"Composição — {snapshot.government_name}",
                    effective_start=member.start_date,
                    effective_end=effective_end,
                    object=portfolio.entity,
                    kind=Relationship.Kind.PUBLIC_OFFICE,
                )
            )
        return sync_observations(
            source="government",
            scope="current-government",
            observations=tuple(observations),
            as_of=snapshot.as_of,
        )
