"""Portuguese Members of the European Parliament from the EP Open Data API v2.

Every MEP who represented Portugal in any term (``meps?country-of-representation=PT``) is
read in detail, but only the name and dated memberships are kept: birth data, gender,
honorifics, contacts, photos and online accounts are never read. Mandates become public
offices at the European Parliament; EU political groups, committees and delegations become
memberships of per-term organisations keyed ``ep`` ``org:<id>`` (organisation ids are
versions per term). National political groups (national parties) are skipped before any
other field is read, per the no-party-affiliation decision; other bodies (Bureau,
Conference of Presidents, working groups, the institution's internal offices) are not
imported. The API allows roughly one request per second and answers 429 with
``Retry-After`` (verified 2026-09-28), so the collection is sequential and paced.
"""

import hashlib
import http.client
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, fields, replace
from datetime import date, datetime
from itertools import batched
from typing import cast
from urllib.parse import parse_qs, urlsplit

from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_observations, sync_scoped_snapshot
from .identity import official_entities_bulk, official_entity, resolve_person
from .models import (
    EnrichmentSource,
    Entity,
    IdentityScheme,
    Relationship,
    SourceIdentity,
    SourceObservation,
    TemporalStatus,
    Term,
    editorial_transaction,
    import_transaction,
)
from .official_http import USER_AGENT, open_connection
from .parliament_parse import JSONObject, JSONValue

DATASET = DATASETS["ep_deputados"]
EP = IdentityScheme.EP
SOURCE = EnrichmentSource.EP
SCOPE_PREFIX = "ep:"
STRUCTURE_SCOPE = "ep-structure"
INSTITUTION_ID = "institution:parlamento-europeu"
INSTITUTION_NAME = "Parlamento Europeu"
MANDATE_ROLE = "Deputado/a ao Parlamento Europeu"

API_HOST = "data.europarl.europa.eu"
API_ROOT = "/api/v2"
API_BASE = f"https://{API_HOST}{API_ROOT}"
FORMAT = "application/ld+json"
FORMAT_QUERY = "format=application%2Fld%2Bjson"
PROFILE_URL = "https://www.europarl.europa.eu/meps/pt/{id}"
COUNTRY = "PT"
# Without ``language`` a detail carries every label language; Portuguese is preferred.
LANGUAGES = ("pt", "en")
PAGE_SIZE = 500
MAX_PAGES = 20
MAX_MEPS = 2000
MAX_TERMS = 30
MAX_REQUESTS = 5000
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_DETAIL_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
TOTAL_TIMEOUT = 3 * 3600
# About one request per second; 429 answers carry ``Retry-After`` (60 s observed).
REQUEST_INTERVAL = 1.0
MAX_ATTEMPTS = 6
DEFAULT_RETRY_AFTER = 60
MAX_RETRY_AFTER = 300
# Transient gateway/server errors were observed live; back off 10 s, 20 s, ...
SERVER_ERRORS = frozenset({500, 502, 503, 504})
SERVER_ERROR_WAIT = 10
NAME_LENGTH = 240
REFERENCE_LENGTH = 160

NATIONAL_PARTY = "NATIONAL_POLITICAL_GROUP"
MANDATE = "MEMBER_PARLIAMENT"
GROUP = Entity.Classification.PARLIAMENTARY_GROUP
COMMITTEE = Entity.Classification.PARLIAMENTARY_COMMITTEE
DELEGATION = Entity.Classification.PARLIAMENTARY_DELEGATION
# EP body classification → (kind of body as shown, entity classification).
BODY_TYPES: dict[str, tuple[str, str]] = {
    "EU_POLITICAL_GROUP": ("Grupo político do Parlamento Europeu", GROUP),
    "COMMITTEE_PARLIAMENTARY_STANDING": ("Comissão permanente do Parlamento Europeu", COMMITTEE),
    "COMMITTEE_PARLIAMENTARY_SUB": ("Subcomissão do Parlamento Europeu", COMMITTEE),
    "COMMITTEE_PARLIAMENTARY_TEMPORARY": ("Comissão temporária do Parlamento Europeu", COMMITTEE),
    "COMMITTEE_PARLIAMENTARY_SPECIAL": ("Comissão especial do Parlamento Europeu", COMMITTEE),
    "COMMITTEE_PARLIAMENTARY_JOINT": ("Comissão parlamentar mista", COMMITTEE),
    "DELEGATION_PARLIAMENTARY": ("Delegação interparlamentar do Parlamento Europeu", DELEGATION),
    "DELEGATION_PARLIAMENTARY_ASSEMBLY": (
        "Delegação do Parlamento Europeu a assembleia parlamentar",
        DELEGATION,
    ),
    "DELEGATION_JOINT_COMMITTEE": (
        "Delegação do Parlamento Europeu a comissão parlamentar mista",
        DELEGATION,
    ),
}
RoleClass = Relationship.RoleClass
# EP role vocabulary (``def/ep-roles/``) → (role as shown, shared role class).
ROLES: dict[str, tuple[str, str]] = {
    "MEMBER": ("Membro", RoleClass.MEMBER),
    "MEMBER_SUBSTITUTE": ("Membro suplente", RoleClass.SUBSTITUTE),
    "CHAIR": ("Presidente", RoleClass.LEADERSHIP),
    "CHAIR_CO": ("Copresidente", RoleClass.LEADERSHIP),
    "CHAIR_VICE": ("Vice-Presidente", RoleClass.DEPUTY_LEADERSHIP),
    "PRESIDENT": ("Presidente", RoleClass.LEADERSHIP),
    "PRESIDENT_VICE": ("Vice-Presidente", RoleClass.DEPUTY_LEADERSHIP),
    "MEMBER_BUREAU": ("Membro da Mesa", RoleClass.OTHER),
    "TREASURER": ("Tesoureiro/a", RoleClass.OTHER),
    "ALLY": ("Membro aliado", RoleClass.OTHER),
}
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
PERSON_ID = re.compile(r"[0-9]{1,12}")
MEMBERSHIP_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{0,79}")
BODY_REF = re.compile(r"org/([0-9]{1,12})")
TERM_REF = re.compile(r"org/ep-([0-9]{1,2})")
DETAIL_PATH = re.compile(
    rf"{API_ROOT}/(?:meps/[0-9]{{1,12}}|corporate-bodies/(?:ep-)?[0-9]{{1,12}})"
)


class EuropeanParliamentError(ValueError):
    """Safe, payload-free failure for an incomplete or unexpected EP collection."""


@dataclass(frozen=True)
class EpTerm:
    number: int
    start: date
    end: date | None

    @property
    def code(self) -> str:
        return f"EP{self.number}"

    @property
    def label(self) -> str:
        return f"{self.number}.ª legislatura do Parlamento Europeu"

    def covers(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


@dataclass(frozen=True)
class Body:
    """One term's version of an EP political group, committee or delegation."""

    org_id: str
    name: str
    ep_class: str
    kind_label: str
    classification: str
    term: int | None
    start: date | None
    end: date | None


@dataclass(frozen=True)
class Membership:
    identifier: str
    # EP organisation id; empty for a mandate (the European Parliament itself).
    org_id: str
    ep_class: str
    role_code: str
    role: str
    role_class: str
    term: int | None
    start: date | None
    end: date | None


@dataclass(frozen=True)
class Mep:
    person_id: str
    name: str
    memberships: tuple[Membership, ...]


@dataclass(frozen=True)
class EpSnapshot:
    as_of: date
    retrieved_at: datetime
    terms: tuple[EpTerm, ...]
    bodies: tuple[Body, ...]
    meps: tuple[Mep, ...]
    skipped: dict[str, int]

    def counts(self) -> dict[str, int]:
        result: Counter[str] = Counter()
        classes = {body.org_id: body.classification for body in self.bodies}
        for mep in self.meps:
            for membership in mep.memberships:
                result[classes.get(membership.org_id, "mandate")] += 1
        return {
            "meps": len(self.meps),
            "terms": len(self.terms),
            "bodies": len(self.bodies),
            "mandates": result["mandate"],
            "groups": result[GROUP],
            "committees": result[COMMITTEE],
            "delegations": result[DELEGATION],
        }


@dataclass(frozen=True)
class _Response:
    status: int
    retry_after: str
    body: bytes


def validate_url(url: str) -> None:
    """Only the PT MEP list, MEP details and corporate-body/term details, as JSON-LD."""
    if len(url) > 512 or any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        raise EuropeanParliamentError("URL do Parlamento Europeu inválido.")
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise EuropeanParliamentError("URL do Parlamento Europeu inválido.") from exc
    if (
        parts.scheme != "https"
        or parts.netloc != API_HOST
        or parts.fragment
        or any(len(values) != 1 for values in query.values())
    ):
        raise EuropeanParliamentError("Destino fora das rotas autorizadas do Parlamento Europeu.")
    values = {key: found[0] for key, found in query.items()}
    path = parts.path
    if path == f"{API_ROOT}/meps":
        allowed = (
            set(values) == {"country-of-representation", "format", "offset", "limit"}
            and values["country-of-representation"] == COUNTRY
            and values["format"] == FORMAT
            and re.fullmatch(r"[0-9]{1,6}", values["offset"]) is not None
            and values["limit"] == str(PAGE_SIZE)
        )
    elif DETAIL_PATH.fullmatch(path):
        allowed = values == {"format": FORMAT}
    else:
        allowed = False
    if not allowed:
        raise EuropeanParliamentError("Destino fora das rotas autorizadas do Parlamento Europeu.")


def meps_url(offset: int) -> str:
    return (
        f"{API_BASE}/meps?country-of-representation={COUNTRY}&{FORMAT_QUERY}"
        f"&offset={offset}&limit={PAGE_SIZE}"
    )


def mep_url(person_id: str) -> str:
    return f"{API_BASE}/meps/{person_id}?{FORMAT_QUERY}"


def body_url(reference: str) -> str:
    """``reference`` is a numeric organisation id or ``ep-<n>`` for a parliamentary term."""
    return f"{API_BASE}/corporate-bodies/{reference}?{FORMAT_QUERY}"


def _get(url: str, *, deadline: float, max_bytes: int) -> _Response:
    """One pinned GET; 429/204 bodies are not read (they are empty)."""
    parts = urlsplit(url)
    try:
        with open_connection(API_HOST, deadline=deadline) as connection:
            connection.request(
                "GET",
                f"{parts.path}?{parts.query}",
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": FORMAT,
                    "Accept-Encoding": "identity",
                },
            )
            with connection.getresponse() as response:
                status = response.status
                retry_after = response.getheader("Retry-After", "") or ""
                if status != 200:
                    return _Response(status, retry_after, b"")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise EuropeanParliamentError("Resposta comprimida recusada.")
                size = response.getheader("Content-Length")
                if size is not None and (not size.isdecimal() or int(size) > max_bytes):
                    raise EuropeanParliamentError(
                        "Resposta do Parlamento Europeu demasiado extensa."
                    )
                content = bytearray()
                while chunk := response.read1(min(65536, max_bytes + 1 - len(content))):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise EuropeanParliamentError(
                            "Resposta do Parlamento Europeu demasiado extensa."
                        )
                if size is not None and len(content) != int(size):
                    raise EuropeanParliamentError("Resposta do Parlamento Europeu truncada.")
                return _Response(status, retry_after, bytes(content))
    except EuropeanParliamentError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise EuropeanParliamentError(
            "Não foi possível consultar o Parlamento Europeu em segurança."
        ) from exc


def _retry_after(value: str) -> int:
    """Seconds to wait after a 429; HTTP dates or garbage fall back to the observed minute."""
    seconds = int(value) if value.strip().isdecimal() else DEFAULT_RETRY_AFTER
    return max(1, min(seconds, MAX_RETRY_AFTER))


class _Session:
    """One sequential, paced and bounded collection run."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.requests = 0
        self.bytes = 0
        self.last = 0.0

    def _pace(self) -> None:
        if self.requests and REQUEST_INTERVAL:
            pause = self.last + REQUEST_INTERVAL - time.monotonic()
            if pause > 0:
                time.sleep(pause)

    def json(self, url: str, *, max_bytes: int, empty_ok: bool = False) -> JSONObject | None:
        validate_url(url)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._pace()
            self.requests += 1
            if self.requests > MAX_REQUESTS:
                raise EuropeanParliamentError("Limite de pedidos ao Parlamento Europeu excedido.")
            response = _get(url, deadline=self.deadline, max_bytes=max_bytes)
            self.last = time.monotonic()
            if response.status == 429 or response.status in SERVER_ERRORS:
                if attempt == MAX_ATTEMPTS:
                    break
                wait = (
                    _retry_after(response.retry_after)
                    if response.status == 429
                    else SERVER_ERROR_WAIT * attempt
                )
                if self.last + wait >= self.deadline:
                    raise EuropeanParliamentError(
                        "Tempo de recolha do Parlamento Europeu esgotado."
                    )
                time.sleep(wait)
                continue
            if response.status == 204 and empty_ok:
                return None
            if response.status != 200:
                raise EuropeanParliamentError(
                    f"O Parlamento Europeu devolveu HTTP {response.status}."
                )
            if not response.body:
                raise EuropeanParliamentError("Resposta vazia do Parlamento Europeu.")
            self.bytes += len(response.body)
            if self.bytes > MAX_TOTAL_BYTES:
                raise EuropeanParliamentError("Limite de dados do Parlamento Europeu excedido.")
            try:
                return _object(cast(JSONValue, json.loads(response.body)))
            except (ValueError, RecursionError) as exc:
                raise EuropeanParliamentError("JSON do Parlamento Europeu inválido.") from exc
        raise EuropeanParliamentError("O Parlamento Europeu recusou pedidos repetidamente.")


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        raise EuropeanParliamentError("Estrutura do Parlamento Europeu inesperada.")
    return value


def _optional_object(value: JSONValue) -> JSONObject:
    return {} if value is None else _object(value)


def _items(page: JSONObject) -> list[JSONValue]:
    data = page.get("data", [])
    if not isinstance(data, list):
        raise EuropeanParliamentError("Estrutura do Parlamento Europeu inesperada.")
    return data


def _single(page: JSONObject) -> JSONObject:
    data = _items(page)
    if len(data) != 1:
        raise EuropeanParliamentError("Registo do Parlamento Europeu ambíguo ou ausente.")
    return _object(data[0])


def _text(value: JSONValue) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EuropeanParliamentError("Campo textual do Parlamento Europeu inválido.")
    return " ".join(value.split())


def _day(value: JSONValue) -> date | None:
    text = _text(value)
    if not text:
        return None
    if DAY.fullmatch(text) is None:
        raise EuropeanParliamentError("Data do Parlamento Europeu inválida.")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise EuropeanParliamentError("Data do Parlamento Europeu inválida.") from exc


def _code(value: JSONValue) -> str:
    """The last segment of a vocabulary reference such as ``def/ep-roles/MEMBER``."""
    text = _text(value)
    if not text:
        return ""
    code = text.rsplit("/", 1)[-1]
    if CODE.fullmatch(code) is None:
        raise EuropeanParliamentError("Código de vocabulário do Parlamento Europeu inválido.")
    return code


def _label(item: JSONObject) -> str:
    """Preferred, else alternative label in Portuguese, else English, else the acronym."""
    for key in ("prefLabel", "altLabel"):
        labels = _optional_object(item.get(key))
        for language in LANGUAGES:
            if label := _text(labels.get(language)):
                return label
    return _text(item.get("label"))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _person_name(item: JSONObject) -> str:
    """Given and family names as published (the label upper-cases the family name)."""
    given, family = _text(item.get("givenName")), _text(item.get("familyName"))
    name = f"{given} {family}" if given and family else _text(item.get("label"))
    if not name:
        raise EuropeanParliamentError("Deputado/a sem nome publicado.")
    return _clip(name, NAME_LENGTH)


def parse_mep_ids(page: JSONObject) -> list[str]:
    ids: list[str] = []
    for value in _items(page):
        identifier = _text(_object(value).get("identifier"))
        if PERSON_ID.fullmatch(identifier) is None:
            raise EuropeanParliamentError("Identificador de deputado/a inválido.")
        ids.append(identifier)
    return ids


@dataclass(frozen=True)
class _RawMembership:
    identifier: str
    # Numeric organisation id, or ``ep-<n>`` for a mandate.
    target: str
    ep_class: str
    role_code: str
    start: date | None
    end: date | None


def _membership(value: JSONValue) -> tuple[_RawMembership | None, str]:
    """A kept membership, or the reason it is skipped."""
    item = _object(value)
    ep_class = _code(item.get("membershipClassification"))
    if ep_class == NATIONAL_PARTY:
        # National party membership: nothing else about it is read.
        return None, "national_party"
    identifier = _text(item.get("identifier"))
    if MEMBERSHIP_ID.fullmatch(identifier) is None:
        raise EuropeanParliamentError("Identificador de pertença do Parlamento Europeu inválido.")
    role_code = _code(item.get("role"))
    organisation = _text(item.get("organization"))
    if not organisation:
        return None, "no_organisation"
    if (term := TERM_REF.fullmatch(organisation)) and role_code == MANDATE:
        target = f"ep-{int(term[1])}"
    elif (body := BODY_REF.fullmatch(organisation)) and ep_class in BODY_TYPES:
        target = body[1]
    else:
        return None, "other_body"
    period = _optional_object(item.get("memberDuring"))
    start, end = _day(period.get("startDate")), _day(period.get("endDate"))
    if start is not None and end is not None and end < start:
        return None, "invalid_dates"
    return _RawMembership(identifier, target, ep_class, role_code, start, end), ""


def parse_mep(page: JSONObject, person_id: str) -> tuple[str, list[_RawMembership], Counter[str]]:
    """Name and kept memberships; every other personal field is left unread."""
    item = _single(page)
    if _text(item.get("identifier")) != person_id:
        raise EuropeanParliamentError("Registo de deputado/a não corresponde ao pedido.")
    memberships = item.get("hasMembership", [])
    if not isinstance(memberships, list):
        raise EuropeanParliamentError("Estrutura do Parlamento Europeu inesperada.")
    kept: dict[str, _RawMembership] = {}
    skipped: Counter[str] = Counter()
    for value in memberships:
        membership, reason = _membership(value)
        if membership is None:
            skipped[reason] += 1
            continue
        known = kept.get(membership.identifier)
        if known is not None and known != membership:
            raise EuropeanParliamentError("Pertença do Parlamento Europeu repetida e divergente.")
        kept[membership.identifier] = membership
    return _person_name(item), list(kept.values()), skipped


def parse_term(page: JSONObject, number: int) -> EpTerm:
    item = _single(page)
    if _text(item.get("identifier")) != f"ep-{number}":
        raise EuropeanParliamentError("Legislatura do Parlamento Europeu não corresponde.")
    period = _optional_object(item.get("temporal"))
    start, end = _day(period.get("startDate")), _day(period.get("endDate"))
    if start is None or (end is not None and end < start):
        raise EuropeanParliamentError("Datas de legislatura do Parlamento Europeu inválidas.")
    return EpTerm(number, start, end)


def parse_body(page: JSONObject, org_id: str) -> tuple[str, str, JSONObject]:
    """(label, classification code, temporal) of a corporate body."""
    item = _single(page)
    if _text(item.get("identifier")) != org_id:
        raise EuropeanParliamentError("Órgão do Parlamento Europeu não corresponde ao pedido.")
    return _label(item), _code(item.get("classification")), _optional_object(item.get("temporal"))


def _term_of(day: date | None, terms: dict[int, EpTerm]) -> int | None:
    if day is None:
        return None
    return next((term.number for term in terms.values() if term.covers(day)), None)


def _fetch_terms(session: _Session, highest: int) -> dict[int, EpTerm]:
    terms: dict[int, EpTerm] = {}
    for number in range(1, highest + 1):
        page = session.json(body_url(f"ep-{number}"), max_bytes=MAX_DETAIL_BYTES)
        terms[number] = parse_term(_object(page), number)
    return terms


def _fetch_body(
    session: _Session,
    org_id: str,
    memberships: list[_RawMembership],
    terms: dict[int, EpTerm],
) -> Body | None:
    """The body in Portuguese (else English); None when the EP classifies it as a party."""
    page = session.json(body_url(org_id), max_bytes=MAX_DETAIL_BYTES)
    label, ep_class, temporal = parse_body(_object(page), org_id)
    if ep_class == NATIONAL_PARTY:
        return None
    if not label:
        raise EuropeanParliamentError("Órgão do Parlamento Europeu sem designação publicada.")
    if ep_class not in BODY_TYPES:
        ep_class = min(membership.ep_class for membership in memberships)
    start, end = _day(temporal.get("startDate")), _day(temporal.get("endDate"))
    if start is not None and end is not None and end < start:
        start = end = None
    term = _term_of(start, terms)
    if term is None:
        known = sorted(m.start for m in memberships if m.start is not None)
        term = _term_of(known[0], terms) if known else None
    kind_label, classification = BODY_TYPES[ep_class]
    suffix = f" ({term}.ª legislatura do PE)" if term is not None else ""
    name = _clip(label, NAME_LENGTH - len(suffix)) + suffix
    return Body(org_id, name, ep_class, kind_label, classification, term, start, end)


def _kept(
    membership: _RawMembership, bodies: dict[str, Body], terms: dict[int, EpTerm]
) -> Membership | None:
    """A mandate or a membership of a fetched body; None when its body was dropped."""
    org_id, ep_class = "", ""
    role: str = MANDATE_ROLE
    role_class: str = RoleClass.MEMBER
    term: int | None
    if membership.target.startswith("ep-"):
        term = int(membership.target[3:])
    elif membership.target in bodies:
        body = bodies[membership.target]
        org_id, ep_class = body.org_id, body.ep_class
        term = body.term if body.term is not None else _term_of(membership.start, terms)
        # Unknown vocabulary keeps the source code; a missing role stays blank.
        role, role_class = ROLES.get(
            membership.role_code,
            (membership.role_code, RoleClass.OTHER if membership.role_code else ""),
        )
    else:
        return None
    return Membership(
        identifier=membership.identifier,
        org_id=org_id,
        ep_class=ep_class,
        role_code=membership.role_code,
        role=role,
        role_class=role_class,
        term=term,
        start=membership.start,
        end=membership.end,
    )


def fetch_snapshot(*, as_of: date) -> EpSnapshot:
    """Every PT MEP of every term, their kept memberships and the bodies they name."""
    session = _Session()
    retrieved_at = timezone.now()
    ids: list[str] = []
    for page_number in range(MAX_PAGES + 1):
        if page_number == MAX_PAGES:
            raise EuropeanParliamentError("Paginação do Parlamento Europeu inesperada.")
        page = session.json(
            meps_url(page_number * PAGE_SIZE), max_bytes=MAX_LIST_BYTES, empty_ok=True
        )
        found = parse_mep_ids(page) if page is not None else []
        ids.extend(found)
        if len(found) < PAGE_SIZE:
            break
    if not ids or len(set(ids)) != len(ids) or len(ids) > MAX_MEPS:
        raise EuropeanParliamentError("Lista de deputados do Parlamento Europeu inesperada.")
    skipped: Counter[str] = Counter()
    raw: list[tuple[str, str, list[_RawMembership]]] = []
    for person_id in ids:
        page = session.json(mep_url(person_id), max_bytes=MAX_DETAIL_BYTES)
        name, memberships, dropped = parse_mep(_object(page), person_id)
        skipped.update(dropped)
        raw.append((person_id, name, memberships))
    mandates = [
        int(item.target[3:]) for _, _, items in raw for item in items if item.target[:3] == "ep-"
    ]
    if not mandates or max(mandates) > MAX_TERMS:
        raise EuropeanParliamentError("Legislaturas do Parlamento Europeu inesperadas.")
    terms = _fetch_terms(session, max(mandates))
    by_body: dict[str, list[_RawMembership]] = {}
    for _, _, items in raw:
        for membership in items:
            if not membership.target.startswith("ep-"):
                by_body.setdefault(membership.target, []).append(membership)
    bodies: dict[str, Body] = {}
    for org_id in sorted(by_body, key=int):
        body = _fetch_body(session, org_id, by_body[org_id], terms)
        if body is None:
            skipped["national_party"] += len(by_body[org_id])
        else:
            bodies[org_id] = body
    meps = [
        Mep(person_id, name, tuple(filter(None, (_kept(m, bodies, terms) for m in items))))
        for person_id, name, items in raw
    ]
    return EpSnapshot(
        as_of=as_of,
        retrieved_at=retrieved_at,
        terms=tuple(terms[number] for number in sorted(terms)),
        bodies=tuple(bodies[org_id] for org_id in sorted(bodies, key=int)),
        meps=tuple(meps),
        skipped=dict(skipped),
    )


def _pt(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _period(start: date | None, end: date | None) -> str:
    if start and end:
        return f"de {_pt(start)} a {_pt(end)}"
    if start:
        return f"desde {_pt(start)}"
    if end:
        return f"até {_pt(end)}"
    return "sem datas indicadas na fonte"


def _iso(value: date | None) -> str:
    return value.isoformat() if value else "?"


def temporal_status(
    start: date | None, end: date | None, term_end: date | None, as_of: date
) -> str:
    """Ended once the source's end or the term has passed; future starts stay unknown."""
    if (end is not None and end < as_of) or (term_end is not None and term_end < as_of):
        return TemporalStatus.ENDED
    if start is not None and start > as_of:
        return TemporalStatus.UNKNOWN
    return TemporalStatus.CURRENT


@editorial_transaction()
def ep_institution() -> Entity:
    """The single European Parliament entity."""
    return official_entity(
        EP,
        INSTITUTION_ID,
        name=INSTITUTION_NAME,
        kind=Entity.Kind.ORGANISATION,
        classification=Entity.Classification.PARLIAMENT,
    )


@editorial_transaction()
def ep_term(term: EpTerm) -> Term:
    """The single Term per EP legislature; the source's dates win (an open end gets filled)."""
    row = Term.objects.select_for_update().filter(kind=Term.Kind.EP_TERM, code=term.code).first()
    if row is None:
        row = Term(
            kind=Term.Kind.EP_TERM,
            code=term.code,
            label=term.label,
            institution=ep_institution(),
            start_date=term.start,
            end_date=term.end,
        )
    elif (row.start_date, row.end_date) == (term.start, term.end):
        return row
    else:
        row.start_date, row.end_date = term.start, term.end
    row.full_clean()
    row.save()
    return row


def _revised(item: ObservationInput) -> ObservationInput:
    """Revision = digest of every substantive field, so any source change is a new revision."""
    digest = {
        field.name: getattr(getattr(item, field.name), "pk", getattr(item, field.name))
        for field in fields(item)
        if field.name not in {"revision", "retrieved_at"}
    }
    revision = hashlib.sha256(
        json.dumps(digest, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    return replace(item, revision=revision)


def _identities(external_ids: list[str]) -> dict[str, SourceIdentity]:
    found: dict[str, SourceIdentity] = {}
    for chunk in batched(external_ids, 5000, strict=False):
        for identity in SourceIdentity.objects.select_related("entity", "reviewed_by").filter(
            source=EP, external_id__in=chunk
        ):
            found[identity.external_id] = identity
    return found


def _structure(
    body: Body,
    identity: SourceIdentity,
    institution: Entity,
    terms: dict[int, Term],
    snapshot: EpSnapshot,
) -> ObservationInput:
    term = terms[body.term] if body.term is not None else None
    return _revised(
        ObservationInput(
            external_id=f"org:{body.org_id}",
            revision="",
            identity=identity,
            category=SourceObservation.Category.ORGANISATION_STRUCTURE,
            passage=f"{body.name}. Tipo: {body.kind_label}. Integra: {INSTITUTION_NAME}.",
            source_url=body_url(body.org_id),
            publisher=DATASET.publisher,
            reference=_clip(
                f"API PE v2 corporate-bodies/{body.org_id}; {body.ep_class}; "
                f"{_iso(body.start)}/{_iso(body.end)}",
                REFERENCE_LENGTH,
            ),
            title=DATASET.title,
            effective_start=body.start,
            effective_end=body.end,
            object=institution,
            kind=Relationship.Kind.PART_OF,
            dataset=DATASET.key,
            role=body.kind_label,
            term=term,
            temporal_status=temporal_status(
                body.start, body.end, term.end_date if term else None, snapshot.as_of
            ),
            retrieved_at=snapshot.retrieved_at,
        )
    )


def _claim(
    mep: Mep,
    membership: Membership,
    *,
    identity: SourceIdentity | None,
    institution: Entity,
    bodies: dict[str, Body],
    organisations: dict[str, Entity],
    terms: dict[int, Term],
    snapshot: EpSnapshot,
) -> ObservationInput:
    term = terms[membership.term] if membership.term is not None else None
    period = _period(membership.start, membership.end)
    if membership.org_id:
        body = bodies[membership.org_id]
        target = organisations[f"org:{body.org_id}"]
        target_name, target_ref = body.name, f"org/{body.org_id}"
        target_identifier = f"ep:org:{body.org_id}"
        kind = Relationship.Kind.MEMBERSHIP
        lines = [
            f"{body.kind_label}: {body.name}.",
            f"Deputado/a ao Parlamento Europeu: {mep.name}.",
            f"Função: {membership.role or 'não indicada'}, {period}.",
        ]
        ep_class = membership.ep_class
    else:
        target, target_name = institution, INSTITUTION_NAME
        target_ref, target_identifier = f"org/ep-{membership.term}", f"ep:{INSTITUTION_ID}"
        kind, ep_class = Relationship.Kind.PUBLIC_OFFICE, MANDATE
        lines = [
            f"{MANDATE_ROLE} \u2014 {term.label if term else 'legislatura não indicada'}.",
            f"Deputado/a: {mep.name}.",
            f"Mandato: {period}.",
        ]
    return _revised(
        ObservationInput(
            external_id=membership.identifier,
            revision="",
            identity=identity,
            subject_name="" if identity else mep.name,
            subject_reference="" if identity else f"{SCOPE_PREFIX}{mep.person_id}",
            category=SourceObservation.Category.PARLIAMENT_BODY,
            passage="\n".join(lines),
            source_url=PROFILE_URL.format(id=mep.person_id),
            publisher=DATASET.publisher,
            reference=_clip(
                f"API PE v2 meps/{mep.person_id}; {membership.identifier}; {target_ref}; "
                f"{ep_class}; {_iso(membership.start)}/{_iso(membership.end)}",
                REFERENCE_LENGTH,
            ),
            title=DATASET.title,
            effective_start=membership.start,
            effective_end=membership.end,
            object=target,
            object_name=target_name,
            object_identifier=target_identifier,
            kind=kind,
            dataset=DATASET.key,
            role=membership.role,
            role_class=membership.role_class,
            term=term,
            temporal_status=temporal_status(
                membership.start, membership.end, term.end_date if term else None, snapshot.as_of
            ),
            retrieved_at=snapshot.retrieved_at,
        )
    )


def apply_snapshot(snapshot: EpSnapshot) -> dict[str, int]:
    """Atomic: EP terms and bodies, structure claims and one complete scope per MEP."""
    with import_transaction():
        institution = ep_institution()
        terms = {term.number: ep_term(term) for term in snapshot.terms}
        rows = {
            f"org:{body.org_id}": (body.name, str(Entity.Kind.ORGANISATION), body.classification)
            for body in snapshot.bodies
        }
        known_bodies = set(_identities(list(rows)))
        organisations = official_entities_bulk(EP, rows)
        body_identities = _identities(list(rows))
        bodies = {body.org_id: body for body in snapshot.bodies}
        result: Counter[str] = Counter(bodies_created=len(rows) - len(known_bodies))
        result.update(
            sync_observations(
                source=SOURCE,
                scope=STRUCTURE_SCOPE,
                observations=tuple(
                    _structure(
                        body, body_identities[f"org:{body.org_id}"], institution, terms, snapshot
                    )
                    for body in snapshot.bodies
                ),
                as_of=snapshot.as_of,
            )
        )
        known_people = set(_identities([mep.person_id for mep in snapshot.meps]))
        scopes: dict[str, tuple[ObservationInput, ...]] = {}
        for mep in snapshot.meps:
            entity = resolve_person(
                EP,
                mep.person_id,
                name=mep.name,
                basis=f"Parlamento Europeu: deputado/a {mep.person_id}",
            )
            identity: SourceIdentity | None = None
            if entity is None:
                result["pending_review"] += 1
            else:
                identity = SourceIdentity.objects.select_related("entity", "reviewed_by").get(
                    source=EP, external_id=mep.person_id
                )
                if mep.person_id not in known_people:
                    result["persons_created"] += 1
            scopes[f"{SCOPE_PREFIX}{mep.person_id}"] = tuple(
                _claim(
                    mep,
                    membership,
                    identity=identity,
                    institution=institution,
                    bodies=bodies,
                    organisations=organisations,
                    terms=terms,
                    snapshot=snapshot,
                )
                for membership in mep.memberships
            )
        result.update(
            sync_scoped_snapshot(
                source=SOURCE, prefix=SCOPE_PREFIX, snapshots=scopes, as_of=snapshot.as_of
            )
        )
        return dict(result)
