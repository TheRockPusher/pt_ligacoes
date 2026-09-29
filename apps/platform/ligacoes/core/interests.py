"""Minimised EpT public interests, scoped to one explicitly reviewed holder.

Wire contract inspected in the public Vue app.b20fd17b source map and anonymous
publicquery responses. Nothing from income/assets, associations, attachments,
addresses or request-only sections is retained. The NIF/NIPC columns keep only a
legal-person NIPC (prefix and check digit); any other value, which may be a natural
person's NIF, is dropped unseen. Responses also carry the holder's personal NIF,
which is never read.
"""

import hashlib
import http.client
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone

from .enrichment import (
    ObservationInput,
    get_source_identity,
    sync_observations,
)
from .identity import official_entity, valid_nipc
from .models import Entity, SourceIdentity, editorial_transaction
from .official_http import open_connection
from .parliament_parse import JSONObject, JSONValue, canonical_json

PUBLIC_URL = "https://entidadetransparencia.pt/"
API_HOST = "api.entidadetransparencia.pt"
ROOT = "dab5b2ca-921a-4c8a-950a-5413e46625ab"
INTERESTS = "1483a8d1-adca-4f39-8038-98cc6f728fcb"
INTEREST_TABLES = "88ff2a6e-3702-4687-9f05-2738f70ccbd0"
ACTIVITIES = "2c6d9562-dea5-488b-9afe-42519c636daf"
SUPPORTS = "62fb0107-a68d-4ba2-822b-bc8adc54369b"
SERVICES = "b1f8c095-fe0c-4bb3-b493-902dc0c31232"
COMPANIES = "9b58c917-b14c-4eed-9b3c-40c3d99d45da"
# Projected tables in wire order; the associations table is never read.
TABLES = (ACTIVITIES, COMPANIES, SUPPORTS, SERVICES)
# Older serviços rows lack the col8 professional-secrecy flag.
COLUMNS: dict[str, frozenset[int]] = {
    ACTIVITIES: frozenset({9}),
    COMPANIES: frozenset({8}),
    SUPPORTS: frozenset({9}),
    SERVICES: frozenset({7, 8}),
}
# "NIF/NIPC" columns: only a legal-person NIPC survives (see _nipc).
TAX_COLUMNS = {ACTIVITIES: 9, COMPANIES: 7, SUPPORTS: 6, SERVICES: 6}
# Commercial legal forms in a declared organisation name; everything else is an
# organisation of unknown nature (importers never reclassify existing entities).
COMMERCIAL_FORM = re.compile(
    r"(?:^|[\s,])(?:S\.\s?A\.?|SA|Lda\.?|LDA|Limitada|Unipessoal|SGPS|S\.G\.P\.S\.)(?=$|[\s,.])"
)
# declarationNatureTypeEnum: 4 cessação, 8 final; a replacement (16) keeps its moment.
POST_OFFICE = {4: "Declaração de cessação de funções", 8: "Declaração final"}
REPLACEMENT = 16
PUBLISHED = 8
STATES = frozenset({1, 2, 4, 8, 16, 32, 64})
NATURES = frozenset({1, 2, 4, 8, 16, 32})
MAX_BYTES = 4 * 1024 * 1024
MAX_DECLARATIONS = 100
MAX_PAGES = 100
TOTAL_TIMEOUT = 600
ENDPOINTS = frozenset({"/getallentities", "/getallroles", "/search", "/getdeclaration"})


class InterestsImportError(ValueError):
    """Safe, payload-free failure for an incomplete or ambiguous public scope."""


@dataclass(frozen=True)
class DeclarationEntry:
    identifier: str
    state: int
    nature: int
    related: str | None
    submitted: str
    moment: int | None = None

    @property
    def post_office(self) -> str | None:
        """Cessation/final declarations, including their replacements."""
        if self.nature in POST_OFFICE:
            return POST_OFFICE[self.nature]
        if self.nature == REPLACEMENT and self.moment is not None:
            return POST_OFFICE.get(self.moment)
        return None


@dataclass(frozen=True)
class InterestsSnapshot:
    holder_id: str
    identity: SourceIdentity
    as_of: date
    observations: tuple[ObservationInput, ...]
    declaration_count: int
    restricted_sections: int
    # NIPC -> (declared name, kind, classification), used only to create a missing entity.
    organisations: dict[str, tuple[str, str, str]]


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise InterestsImportError("Estrutura pública EpT inesperada.")
    return value


def _array(value: JSONValue) -> list[JSONValue]:
    if not isinstance(value, list) or len(value) > 10000:
        raise InterestsImportError("Lista pública EpT inválida ou excessiva.")
    return value


def _integer(value: JSONValue, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise InterestsImportError("Número público EpT inválido.")
    return value


def _identifier(value: JSONValue) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InterestsImportError("Identificador EpT inválido.")
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", result):
        raise InterestsImportError("Identificador EpT inválido.")
    return result


def _text(value: JSONValue, *, limit: int = 200) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise InterestsImportError("Texto público EpT inválido ou excessivo.")
    # Even an allowlisted free-text cell must not accidentally retain a tax ID.
    if re.search(r"(?<!\d)\d{9}(?!\d)", value):
        raise InterestsImportError("Campo textual EpT contém um identificador não permitido.")
    return value.strip()


def _date(value: JSONValue) -> tuple[date | None, str]:
    if value in (None, ""):
        return None, ""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})?)?",
        value,
    ):
        raise InterestsImportError("Data pública EpT ambígua ou inválida.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InterestsImportError("Data pública EpT inválida.") from exc
    # The frontend formats timestamps in the reader's local timezone. Do not
    # silently turn a previous-day UTC timestamp into a precise activity date.
    # Keep its literal source value for editorial review instead.
    if parsed.tzinfo is not None:
        local_day = parsed.astimezone(ZoneInfo("Europe/Lisbon")).date()
        if local_day != parsed.date():
            return None, value
    return parsed.date(), value


def _envelope(value: JSONValue) -> JSONValue:
    response = _object(value)
    if type(response.get("code")) is not int or response["code"] != 0 or "data" not in response:
        raise InterestsImportError("A consulta pública EpT não foi concluída.")
    return response["data"]


def _post(endpoint: str, body: JSONObject, *, holder_id: str, deadline: float) -> JSONValue:
    """Fixed public routes, pinned public DNS/TLS, bounded memory, no redirects."""
    get_source_identity(source="ept", external_id=holder_id)
    if endpoint not in ENDPOINTS or time.monotonic() >= deadline:
        raise InterestsImportError("Consulta EpT fora do âmbito ou do prazo permitido.")
    try:
        with open_connection(API_HOST, deadline=deadline) as connection:
            connection.request(
                "POST",
                "/publicquery" + endpoint,
                body=canonical_json(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "LigacoesPT-editorial-import/1",
                },
            )
            with connection.getresponse() as response:
                if response.status != 200:
                    raise InterestsImportError("A fonte EpT recusou a consulta pública.")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise InterestsImportError("Resposta EpT comprimida não permitida.")
                if (
                    response.getheader("Content-Type", "").split(";", 1)[0].lower()
                    != "application/json"
                ):
                    raise InterestsImportError("A fonte EpT não devolveu JSON público.")
                size = response.getheader("Content-Length")
                if size is not None and (not size.isdecimal() or int(size) > MAX_BYTES):
                    raise InterestsImportError("Resposta EpT excessiva.")
                content = bytearray()
                while True:
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
                    if not chunk:
                        break
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise InterestsImportError("Resposta EpT excessiva.")
                if size is not None and len(content) != int(size):
                    raise InterestsImportError("Resposta EpT truncada.")
                return cast(JSONValue, json.loads(content))
    except (OSError, http.client.HTTPException) as exc:
        raise InterestsImportError("Não foi possível consultar a fonte EpT em segurança.") from exc
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, InterestsImportError):
            raise
        raise InterestsImportError("JSON público EpT inválido.") from exc


def _entries(pages: list[JSONValue]) -> tuple[DeclarationEntry, ...]:
    if not pages or len(pages) > MAX_PAGES:
        raise InterestsImportError("Âmbito de declarações EpT incompleto.")
    entries: list[DeclarationEntry] = []
    identifiers: set[str] = set()
    expected: tuple[int, int, int] | None = None
    for number, payload in enumerate(pages, 1):
        page = _object(_envelope(payload))
        total = _integer(page.get("total"))
        count = _integer(page.get("pageCount"))
        page_size = _integer(page.get("pageSize"), minimum=1)
        if total > MAX_DECLARATIONS or count > MAX_PAGES:
            raise InterestsImportError("Âmbito individual EpT excessivo; não foi aplicado.")
        if count != (total + page_size - 1) // page_size:
            raise InterestsImportError("Paginação EpT inconsistente.")
        if expected is None:
            expected = (total, count, page_size)
        if expected != (total, count, page_size) or _integer(page.get("pageNumber")) != number:
            raise InterestsImportError("A lista EpT mudou ou está incompleta.")
        items = _array(page.get("items"))
        if len(items) != min(page_size, max(0, total - (number - 1) * page_size)):
            raise InterestsImportError("Página EpT truncada.")
        for item in items:
            row = _object(item)
            identifier = _identifier(row.get("id"))
            state = _integer(row.get("stateTypeId"), minimum=1)
            nature = _integer(row.get("natureTypeId"), minimum=1)
            submitted = row.get("deliveryDate")
            if not isinstance(submitted, str):
                raise InterestsImportError("Declaração EpT sem data de entrega.")
            _date(submitted)
            if identifier in identifiers or state not in STATES or nature not in NATURES:
                raise InterestsImportError("Declaração EpT duplicada ou com estado desconhecido.")
            identifiers.add(identifier)
            related = row.get("relatedDeclarationId")
            moment = row.get("declarativeMomentTypeId")
            entries.append(
                DeclarationEntry(
                    identifier,
                    state,
                    nature,
                    _identifier(related) if related is not None else None,
                    submitted,
                    _integer(moment) if moment is not None else None,
                )
            )
    if expected is None or len(pages) != max(1, expected[1]) or len(entries) != expected[0]:
        raise InterestsImportError("Âmbito de declarações EpT incompleto.")
    return tuple(entries)


def _visible(node: JSONObject, blocked: set[str]) -> bool:
    key = node.get("key")
    if not isinstance(key, str) or type(node.get("isVisible")) is not bool:
        raise InterestsImportError("Visibilidade EpT desconhecida.")
    return (
        node["isVisible"] is True
        and node.get("reason") is None
        and not node.get("justification")
        and key not in blocked
    )


def _children(node: JSONObject) -> dict[str, JSONObject]:
    result: dict[str, JSONObject] = {}
    for child in _array(node.get("value")):
        row = _object(child)
        key = row.get("key")
        if not isinstance(key, str) or not key or key in result:
            raise InterestsImportError("Campos EpT duplicados ou sem chave.")
        result[key] = row
    return result


def _child(node: JSONObject, key: str) -> JSONObject:
    children = _children(node)
    if key not in children:
        raise InterestsImportError("A estrutura dos campos públicos EpT mudou.")
    return children[key]


def _restrictions(detail: JSONObject) -> set[str]:
    # No response with non-empty oppositionKeys was observed. Do not infer its
    # wire shape or mistake a pending objection for positively reusable content.
    if _array(detail.get("oppositionKeys")):
        return {ROOT}
    unavailable = detail.get("unavailableSections")
    if unavailable is None:
        return set()
    blocked = set()
    for section in _array(unavailable):
        key = _object(section).get("sectionKey")
        if not isinstance(key, str) or not key:
            raise InterestsImportError("Restrição de secção EpT desconhecida.")
        blocked.add(key)
    return blocked


def _cell(cells: dict[str, JSONObject], table: str, column: int, blocked: set[str]) -> JSONValue:
    cell = cells.get(f"{table}-col{column}")
    return cell.get("value") if cell is not None and _visible(cell, blocked) else None


def _nipc(cells: dict[str, JSONObject], table: str, blocked: set[str]) -> str:
    """A legal-person NIPC, or "" — any other value is discarded without being kept or shown."""
    value = _cell(cells, table, TAX_COLUMNS[table], blocked)
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or len(value) > 40:
        return ""
    candidate = re.sub(r"[\s.]", "", value).removeprefix("PT")
    return candidate if valid_nipc(candidate) else ""


def _choice(value: JSONValue, allowed: tuple[int, ...], message: str) -> int | None:
    """A frontend enum value as int; blank is unspecified, anything unknown stops the scope."""
    if value in (None, ""):
        return None
    if (
        type(value) is bool
        or not isinstance(value, (int, str))
        or str(value) not in {str(option) for option in allowed}
    ):
        raise InterestsImportError(message)
    return int(value)


def _dated_passage(
    cells: dict[str, JSONObject], table: str, columns: tuple[int, int], blocked: set[str]
) -> tuple[date | None, date | None, str]:
    start, start_literal = _date(_cell(cells, table, columns[0], blocked))
    end, end_literal = _date(_cell(cells, table, columns[1], blocked))
    if start is not None and end is not None and end < start:
        raise InterestsImportError("Intervalo da atividade EpT invertido.")
    passage = ""
    for label, literal, resolved in (
        ("Início", start_literal, start),
        ("Termo", end_literal, end),
    ):
        if literal:
            passage += f" {label} (valor da fonte): {literal}."
            if resolved is None:
                passage += " Data civil por confirmar; o fuso horário altera o dia."
    return start, end, passage


def _project_row(
    row: JSONObject,
    *,
    table: str,
    blocked: set[str],
    detail: JSONObject,
    identity: SourceIdentity,
    declared_on: date,
    post_office: str | None,
    organisations: dict[str, tuple[str, str, str]],
) -> ObservationInput | None:
    if not _visible(row, blocked):
        return None
    if type(row.get("isEmpty")) is not bool:
        raise InterestsImportError("Estado de linha EpT desconhecido.")
    if row["isEmpty"] is True:
        return None
    key = row.get("key")
    if not isinstance(key, str) or not re.fullmatch(re.escape(table) + r"_\d{1,5}", key):
        raise InterestsImportError("Chave de linha EpT desconhecida.")
    cells = _children(row)
    if len(cells) not in COLUMNS[table] or set(cells) != {
        f"{table}-col{i}" for i in range(1, len(cells) + 1)
    }:
        raise InterestsImportError("As colunas dos interesses EpT mudaram.")
    start, end = None, None
    commercial = False
    if table == ACTIVITIES:
        role = _text(_cell(cells, table, 1, blocked))
        organisation = _text(_cell(cells, table, 2, blocked))
        area = _text(_cell(cells, table, 3, blocked))
        if not role or not organisation:
            return None
        start, end, dates = _dated_passage(cells, table, (6, 7), blocked)
        passage = f"Atividade profissional declarada: {role}. Entidade declarada: {organisation}."
        if area:
            passage += f" Natureza/área: {area}."
        passage += dates
        kind = "professional_activity"
    elif table == SUPPORTS:
        support = _text(_cell(cells, table, 1, blocked))
        organisation = _text(_cell(cells, table, 2, blocked))
        recipient = _choice(
            _cell(cells, table, 8, blocked),
            (0, 1, 2, 3),
            "Destinatário do apoio EpT desconhecido.",
        )
        # Only the declarant's own benefits: spouse/partner (1, 2) are family data and a
        # participated company (3) is not the holder; an unspecified recipient is not guessed.
        if recipient != 0 or not support or not organisation:
            return None
        entity_area = _text(_cell(cells, table, 3, blocked))
        support_area = _text(_cell(cells, table, 4, blocked))
        start, end, dates = _dated_passage(cells, table, (5, 7), blocked)
        passage = (
            f"Apoio ou benefício declarado: {support}. Entidade concedente declarada: "
            f"{organisation}."
        )
        if entity_area:
            passage += f" Natureza/área da entidade: {entity_area}."
        if support_area:
            passage += f" Natureza/área do apoio: {support_area}."
        passage += dates
        kind = "professional_activity"
    elif table == SERVICES:
        service = _text(_cell(cells, table, 1, blocked))
        organisation = _text(_cell(cells, table, 2, blocked))
        secrecy = _cell(cells, table, 8, blocked)
        # Services under professional secrecy are never projected, even if a cell leaks.
        if secrecy in (True, "true", "True", 1, "1") or not service or not organisation:
            return None
        area = _text(_cell(cells, table, 3, blocked))
        start, end, dates = _dated_passage(cells, table, (5, 7), blocked)
        passage = f"Serviço prestado declarado: {service}. Entidade declarada: {organisation}."
        if area:
            passage += f" Natureza/área: {area}."
        passage += dates
        kind = "professional_activity"
    else:
        organisation = _text(_cell(cells, table, 1, blocked))
        ownership = _cell(cells, table, 8, blocked)
        nature = _cell(cells, table, 6, blocked)
        # These are the frontend's explicit ownership-holder choices, NOT a
        # guess from the company name, amount, shared surname or spouse's entry.
        if ownership in (None, "", 3, "3", 4, "4") or not organisation:
            return None
        if type(ownership) is bool or ownership not in (1, "1", 2, "2"):
            raise InterestsImportError("Titularidade da participação EpT desconhecida.")
        if nature in (None, ""):
            return None
        if type(nature) is bool or nature not in (1, "1", 2, "2"):
            raise InterestsImportError("Natureza da sociedade EpT desconhecida.")
        owner_label = "titular único" if str(ownership) == "1" else "cotitular"
        nature_label = "civil" if str(nature) == "1" else "comercial"
        commercial = nature_label == "comercial"
        passage = (
            f"Participação declarada em sociedade {nature_label}: {organisation}. "
            f"O declarante é {owner_label}."
        )
        kind = "shareholding"
    nipc = _nipc(cells, table, blocked)
    if nipc:
        passage += f" NIPC declarado: {nipc}."
        if commercial or COMMERCIAL_FORM.search(organisation):
            organisations.setdefault(
                nipc, (organisation, Entity.Kind.COMPANY, Entity.Classification.COMPANY)
            )
        else:
            organisations.setdefault(
                nipc, (organisation, Entity.Kind.ORGANISATION, Entity.Classification.OTHER)
            )
    declaration_id = _identifier(detail.get("id"))
    entity_id = _identifier(detail.get("entityId"))
    role_id = _identifier(detail.get("roleId"))
    board_id = detail.get("boardId")
    board = _identifier(board_id) if board_id is not None else "—"
    reference = (
        f"Decl. {declaration_id}; titular {identity.external_id}; entidade {entity_id}; "
        f"órgão {board}; cargo {role_id}; {key}"
    )
    if post_office:
        reference += "; pós-cargo"
    if len(reference) > 160:
        raise InterestsImportError(
            "Referência EpT excessiva; identificadores não foram abreviados."
        )
    institution = _text(detail.get("entity"), limit=300)
    public_role = _text(detail.get("role"), limit=300)
    holder_label = _text(detail.get("holder"), limit=300)
    if not institution or not public_role or not holder_label:
        raise InterestsImportError("Falta o contexto público para localizar a declaração EpT.")
    passage += (
        f" Declaração entregue em {declared_on.isoformat()}; titular na fonte: {holder_label}; "
        f"instituição: {institution}; cargo público declarado: {public_role}. "
        "Consulta pelo portal EpT e pela referência; a ligação não é um endereço direto."
    )
    if post_office:
        passage += (
            f" {post_office}: declaração pós-cargo, entregue após o exercício do cargo público."
        )
    projection: JSONObject = {
        "passage": passage,
        "reference": reference,
        "declared_on": declared_on.isoformat(),
        "submitted": detail.get("submitedDate"),
        "nature": detail.get("natureType"),
        "related": detail.get("relatedDeclarationId"),
    }
    # Added only when present, so unchanged rows keep their earlier revision.
    if nipc:
        projection["nipc"] = nipc
    return ObservationInput(
        external_id=f"declaration:{declaration_id}:{key}",
        revision=hashlib.sha256(canonical_json(projection).encode()).hexdigest(),
        identity=identity,
        category="declared_interest",
        passage=passage,
        source_url=PUBLIC_URL,
        publisher="Entidade para a Transparência",
        reference=reference,
        title=f"EpT — declaração pública {declaration_id}, entregue em {declared_on.isoformat()}",
        effective_start=start,
        effective_end=end,
        declared_on=declared_on,
        object_name=organisation if nipc else "",
        object_identifier=f"nipc:{nipc}" if nipc else "",
        kind=kind,
        dataset="ept_declaracoes",
    )


def parse_snapshot(
    *,
    holder_id: str,
    identity: SourceIdentity,
    pages: list[JSONValue],
    details: dict[str, JSONValue],
    as_of: date,
) -> InterestsSnapshot:
    """Offline projection of a complete holder list; no raw response is retained."""
    holder_id = _identifier(holder_id)
    if (
        identity.source != "ept"
        or identity.external_id != holder_id
        or identity.entity.kind != Entity.Kind.PERSON
        or identity.reviewed_by_id is None
        or identity.reviewed_at is None
    ):
        raise InterestsImportError("É necessária uma correspondência de titular revista.")
    entries = _entries(pages)
    if set(details) != {entry.identifier for entry in entries if entry.state == PUBLISHED}:
        raise InterestsImportError("Faltam declarações do âmbito completo do titular.")
    observations: list[ObservationInput] = []
    organisations: dict[str, tuple[str, str, str]] = {}
    restricted = 0
    for entry in entries:
        if entry.state != PUBLISHED:
            continue
        detail = _object(_envelope(details[entry.identifier]))
        if (
            _identifier(detail.get("id")) != entry.identifier
            or _identifier(detail.get("holderId")) != holder_id
            or detail.get("stateType") != entry.state
            or detail.get("natureType") != entry.nature
            or detail.get("submitedDate") != entry.submitted
            or detail.get("relatedDeclarationId")
            != (int(entry.related) if entry.related is not None else None)
        ):
            raise InterestsImportError("Identidade ou revisão EpT diverge da lista completa.")
        declared_on, _ = _date(detail.get("submitedDate"))
        if declared_on is None or declared_on > as_of:
            raise InterestsImportError(
                "Data de entrega EpT não confirmada ou posterior à consulta."
            )
        blocked = _restrictions(detail)
        root = _object(detail.get("data"))
        if root.get("key") != ROOT:
            raise InterestsImportError("Modelo de declaração pública EpT desconhecido.")
        parent = root
        unavailable = False
        for key in (INTERESTS, INTEREST_TABLES):
            if not _visible(parent, blocked):
                unavailable = True
                break
            parent = _child(parent, key)
        if unavailable or not _visible(parent, blocked):
            restricted += len(TABLES)
            continue
        for table in TABLES:
            node = _child(parent, table)
            if not _visible(node, blocked):
                restricted += 1
                continue
            for row in _children(node).values():
                observation = _project_row(
                    row,
                    table=table,
                    blocked=blocked,
                    detail=detail,
                    identity=identity,
                    declared_on=declared_on,
                    post_office=entry.post_office,
                    organisations=organisations,
                )
                if observation is not None:
                    observations.append(observation)
    return InterestsSnapshot(
        holder_id,
        identity,
        as_of,
        tuple(observations),
        len(entries),
        restricted,
        organisations,
    )


def _selectors(payload: JSONValue) -> list[JSONValue]:
    identifiers = [_identifier(_object(row).get("value")) for row in _array(_envelope(payload))]
    if len(identifiers) != len(set(identifiers)):
        raise InterestsImportError("Seletores públicos EpT duplicados.")
    return [int(identifier) for identifier in identifiers]


def _fetch_pages(body: JSONObject, *, holder_id: str, deadline: float) -> list[JSONValue]:
    pages = [_post("/search", body | {"pageNumber": 1}, holder_id=holder_id, deadline=deadline)]
    first = _object(_envelope(pages[0]))
    count = _integer(first.get("pageCount"))
    if count > MAX_PAGES or _integer(first.get("total")) > MAX_DECLARATIONS:
        raise InterestsImportError("Âmbito individual EpT excessivo; não foi aplicado.")
    for number in range(2, count + 1):
        pages.append(
            _post("/search", body | {"pageNumber": number}, holder_id=holder_id, deadline=deadline)
        )
    _entries(pages)
    return pages


def fetch_snapshot(*, holder_id: str) -> InterestsSnapshot:
    """No cohort/name discovery: fetch only an already reviewed holder crosswalk."""
    holder_id = _identifier(holder_id)
    identity = get_source_identity(source="ept", external_id=holder_id)
    as_of = timezone.localdate()
    deadline = time.monotonic() + TOTAL_TIMEOUT
    entities = _selectors(
        _post(
            "/getallentities",
            {"HolderId": int(holder_id), "RoleId": None},
            holder_id=holder_id,
            deadline=deadline,
        )
    )
    roles = _selectors(
        _post(
            "/getallroles",
            {"HolderId": int(holder_id), "EntityId": None},
            holder_id=holder_id,
            deadline=deadline,
        )
    )
    body: JSONObject = {
        "HolderId": int(holder_id),
        "EntityId": entities,
        "RoleId": roles,
        "sortBy": "Id asc",
    }
    pages = _fetch_pages(body, holder_id=holder_id, deadline=deadline)
    entries = _entries(pages)
    details = {
        entry.identifier: _post(
            "/getdeclaration",
            {"Id": int(entry.identifier), "isDraft": False},
            holder_id=holder_id,
            deadline=deadline,
        )
        for entry in entries
        if entry.state == PUBLISHED
    }
    # Complete unfiltered holder query also detects selector omissions, moving
    # pagination and declarations changed/withdrawn while fetching details.
    final_pages = _fetch_pages(
        body | {"EntityId": [], "RoleId": []}, holder_id=holder_id, deadline=deadline
    )
    if _entries(final_pages) != entries:
        raise InterestsImportError("O âmbito EpT mudou durante a consulta; nada foi aplicado.")
    return parse_snapshot(
        holder_id=holder_id, identity=identity, pages=pages, details=details, as_of=as_of
    )


def apply_snapshot(snapshot: InterestsSnapshot) -> dict[str, int]:
    """Private editorial candidates; declared NIPCs anchor their organisation.

    Absence invalidates this holder's scope.
    """
    with editorial_transaction():
        identity = get_source_identity(source="ept", external_id=snapshot.holder_id)
        if identity.pk != snapshot.identity.pk or identity.entity_id != snapshot.identity.entity_id:
            raise ValidationError("A correspondência de titular mudou; repita a revisão.")
        entities = {
            f"nipc:{nipc}": official_entity(
                "nipc", nipc, name=name, kind=kind, classification=classification
            )
            for nipc, (name, kind, classification) in snapshot.organisations.items()
        }
        observations = tuple(
            replace(item, object=entities[item.object_identifier])
            if item.object_identifier
            else item
            for item in snapshot.observations
        )
        return sync_observations(
            source="ept",
            scope=f"holder:{snapshot.holder_id}",
            observations=observations,
            as_of=snapshot.as_of,
        )
