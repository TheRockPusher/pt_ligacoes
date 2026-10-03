"""EU interest representation: Transparency Register organisations and their meetings.

Three official sources, one run:

- the EU Transparency Register XML export (organisations keyed by their TR id);
- the European Parliament's export of meetings declared by MEPs (Portuguese MEPs only);
- the European Commission's meetings of Commissioners' cabinets (Portuguese Commissioners).

The register is always read: it names the organisations a meeting cites by TR id and
identifies self-employed registrants (natural persons), whose names are never kept.
Organisations are created only from TR ids (Portuguese head office, or cited by a meeting);
meeting attendees without a TR id stay unresolved name-only parties, so their meetings
remain drafts. Natural persons (EP attendees who are not organisations, Commission staff,
Commissioners) are never stored as parties.
"""

import csv
import hashlib
import html
import io
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast
from urllib.parse import urlencode, urlsplit

from django.utils.text import slugify

from .catalogue import DATASETS
from .events import EventInput, PartyInput, sync_events
from .identity import normalise_name, official_entities_bulk, official_entity
from .models import (
    Entity,
    Event,
    EventParty,
    IdentityScheme,
    IdentitySuggestion,
    SourceIdentity,
    import_transaction,
)
from .official_http import OfficialHTTPError, download

REGISTER = DATASETS["eu_registo_transparencia"].key
EP_MEETINGS = DATASETS["ep_reunioes"].key
EC_MEETINGS = DATASETS["ce_reunioes"].key
EU_TR = IdentityScheme.EU_TR
EP = IdentityScheme.EP
EC = IdentityScheme.EC

REGISTER_URL = "https://ec.europa.eu/transparencyregister/public/files/ODP/download/XML/latest"
REGISTER_HOSTS = {
    "ec.europa.eu": "/transparencyregister/public/files/ODP/download/XML/",
    "transparency-register.europa.eu": "/odplastorganisationxml_en",
}
EP_HOST = "www.europarl.europa.eu"
EP_EXPORT_PATH = "/meps/en/search-meetings"
EP_API_HOST = "data.europarl.europa.eu"
EP_API_PATH = "/api/v2/meps"
EC_HOST = "ec.europa.eu"
EC_PATH = "/transparency-initiative/meetings/data/meetings/dataxml"

# Portuguese Commissioners' cabinets, exactly as each mandate file labels them.
EC_FILES: dict[str, str] = {
    "meetingscommissionrepresentatives1419": "Cabinet of Commissioner Carlos Moedas",
    "meetingscommissionrepresentatives1924": "Cabinet of Commissioner Elisa Ferreira",
    "meetingscommissionrepresentatives2429": "Cabinet of Commissioner Maria Luís Albuquerque",
}

PORTUGAL = "PORTUGAL"
EP_FIRST_MONTH = (2019, 7)  # Declared meetings start with the 9th term.
EP_ROW_CAP = 1000
EP_COLUMNS = [
    "title",
    "member_id",
    "member_name",
    "meeting_date",
    "member_capacity",
    "procedure_reference",
    "attendees",
    "lobbyist_id",
]
MAX_REGISTER_BYTES = 320 * 1024 * 1024
MAX_EP_BYTES = 8 * 1024 * 1024
MAX_API_BYTES = 4 * 1024 * 1024
MAX_EC_BYTES = 48 * 1024 * 1024
TOTAL_TIMEOUT = 3600
REQUEST_INTERVAL = 1.0
NAME_LENGTH = 300
TITLE_LENGTH = 500

TR_ID = re.compile(r"[0-9]{6,15}-[0-9]{2}")
MEP_ID = re.compile(r"[0-9]{1,12}")
MONTH = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")
# Character references XML 1.0 forbids (C0 controls except tab, LF and CR).
INVALID_REFS = re.compile(
    rb"&#(?:[xX]0*(?:[0-8bBcCeE]|1[0-9a-fA-F])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));"
)
XML_11 = re.compile(rb"^(\s*<\?xml[^>]*version=['\"])1\.1(['\"])")

SELF_EMPLOYED = "self-employed"
COMPANY = (Entity.Kind.COMPANY, Entity.Classification.COMPANY)
ASSOCIATION = (Entity.Kind.ORGANISATION, Entity.Classification.ASSOCIATION)
UNIVERSITY = (Entity.Kind.UNIVERSITY, Entity.Classification.HIGHER_EDUCATION)
PUBLIC_BODY = (Entity.Kind.ORGANISATION, Entity.Classification.PUBLIC_BODY)
OTHER = (Entity.Kind.ORGANISATION, Entity.Classification.OTHER)
# Register categories (lowercase prefixes of the published labels).
CATEGORIES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("companies", COMPANY),
    ("professional consultancies", COMPANY),
    ("law firms", COMPANY),
    ("trade and business associations", ASSOCIATION),
    ("trade unions", ASSOCIATION),
    ("non-governmental organisations", ASSOCIATION),
    ("think tanks", ASSOCIATION),
    ("associations and networks of public authorities", PUBLIC_BODY),
)
UNIVERSITY_NAME = re.compile(r"\b(universi|universidade|université|universität)", re.IGNORECASE)
# Free-text attendee names kept (as unresolved organisations) only when they read as one;
# anything else may be a natural person and is dropped.
ORGANISATION_MARKERS = re.compile(
    r"(\b(s\.a|lda|ltd|limited|inc|llc|plc|gmbh|ag|sarl|sas|bv|nv|spa|srl|"
    r"asbl|aisbl|e\.?v|group|groupe|grupo|company|companhia|corporation|corp|holding|"
    r"association|associação|associacao|federation|federação|federacao|confederation|"
    r"confederação|union|união|council|conselho|institute|instituto|institut|foundation|"
    r"fundação|fundacao|fondation|stiftung|society|sociedade|société|network|rede|platform|"
    r"alliance|chamber|câmara|camara|agency|agência|agencia|ministry|ministério|"
    r"university|universidade|université|college|school|bank|banco|forum|centre|center|"
    r"centro|committee|comité|organisation|organization|organização|office|europe|european|"
    r"europeia|europeu|international|internacional|global|partners|consulting|associates|"
    r"services|industries|industry|energy|energia|airlines|telecom|pharma)\b|[0-9&@])",
    re.IGNORECASE,
)


class EuContactsError(ValueError):
    """Safe, payload-free failure for an incomplete or unexpected EU source."""


class EpMeetingsBlocked(EuContactsError):
    """The official EP export refused access; no challenge is bypassed."""


@dataclass(frozen=True)
class Registrant:
    tr_id: str
    name: str
    acronym: str
    category: str
    portuguese: bool

    @property
    def person(self) -> bool:
        return self.category.lower().startswith(SELF_EMPLOYED)


@dataclass(frozen=True)
class Attendee:
    """An organisation as published: TR id, name, or both."""

    tr_id: str
    name: str


@dataclass(frozen=True)
class Meeting:
    record_id: str
    day: date
    title: str
    actor_id: str
    actor_name: str
    attendees: tuple[Attendee, ...]
    record_url: str = ""
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    as_of: date
    datasets: frozenset[str]
    registrants: dict[str, Registrant]
    ep_months: dict[str, tuple[Meeting, ...]]
    ec_files: dict[str, tuple[Meeting, ...]]
    dropped: Counter[str]
    warnings: tuple[str, ...] = ()


# Parsing -----------------------------------------------------------------------------


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _name(value: str) -> str:
    name = _clean(value)
    return name if len(name) <= NAME_LENGTH else name[: NAME_LENGTH - 3].rstrip() + "..."


def _title(value: str) -> str:
    title = _clean(value) or "Reunião"
    return title if len(title) <= TITLE_LENGTH else title[: TITLE_LENGTH - 3].rstrip() + "..."


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def sanitise_xml(data: bytes) -> bytes:
    """Replace character references XML 1.0 forbids; the register publishes a few."""
    data = XML_11.sub(rb"\g<1>1.0\g<2>", data[:200]) + data[200:]
    return INVALID_REFS.sub(b" ", data)


DTD_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def refuse_dtd(data: bytes) -> bytes:
    """Reject documents declaring a DTD or entities before any XML parser sees them."""
    if DTD_DECLARATION.search(data):
        raise EuContactsError("XML com declarações DOCTYPE/ENTITY recusado.")
    return data


def _child_text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    return _clean(found.text if found is not None else "")


def classify(category: str, name: str) -> tuple[str, str]:
    """(kind, classification) of a new registrant, from its register category."""
    label = category.lower()
    if label.startswith("academic"):
        return UNIVERSITY if UNIVERSITY_NAME.search(name) else ASSOCIATION
    for prefix, result in CATEGORIES:
        if label.startswith(prefix):
            return result
    return OTHER


def parse_register(data: bytes) -> dict[str, Registrant]:
    """Every registrant (streamed); only identification and category are retained."""
    registrants: dict[str, Registrant] = {}
    try:
        # DTDs/entities are refused up front; Expat's built-in amplification limits apply.
        context = ET.iterparse(  # noqa: S314
            io.BytesIO(sanitise_xml(refuse_dtd(data))), events=("start", "end")
        )
        root: ET.Element | None = None
        for event, element in context:
            if root is None and event == "start":
                root = element
            if event != "end" or element.tag != "interestRepresentative":
                continue
            tr_id = _child_text(element, "identificationCode")
            name = _name(_child_text(element, "name/originalName"))
            if not TR_ID.fullmatch(tr_id) or not name:
                raise EuContactsError("Registo de Transparência com entrada inválida.")
            registrants[tr_id] = Registrant(
                tr_id=tr_id,
                name=name,
                acronym=_child_text(element, "acronym")[:80],
                category=_child_text(element, "registrationCategory"),
                portuguese=_child_text(element, "headOffice/country").upper() == PORTUGAL,
            )
            element.clear()
            if root is not None:
                root.clear()
    except ET.ParseError as exc:
        raise EuContactsError("XML do Registo de Transparência inválido.") from exc
    if not registrants:
        raise EuContactsError("Registo de Transparência vazio.")
    return registrants


def _digest(*parts: object) -> str:
    text = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _mep_slug(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", plain).strip("_").upper() or "MEP"


def mep_meetings_url(member_id: str, name: str) -> str:
    return f"https://{EP_HOST}/meps/pt/{member_id}/{_mep_slug(name)}/meetings/past"


def parse_ep_csv(
    data: bytes, *, members: frozenset[str], as_of: date, dropped: Counter[str]
) -> list[Meeting]:
    """Meetings of the given MEPs; test, foreign, undated and future rows are dropped."""
    try:
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        header = next(reader, None)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise EuContactsError("CSV de reuniões do Parlamento Europeu inválido.") from exc
    if header != EP_COLUMNS:
        raise EuContactsError("Estrutura inesperada no CSV de reuniões do Parlamento Europeu.")
    meetings: list[Meeting] = []
    try:
        for row in reader:
            if len(row) != len(EP_COLUMNS):
                raise EuContactsError("Linha inesperada no CSV de reuniões do Parlamento Europeu.")
            values = dict(zip(EP_COLUMNS, row, strict=True))
            member_id = values["member_id"].strip()
            if not MEP_ID.fullmatch(member_id) or member_id not in members:
                dropped["ep_other_rows"] += 1
                continue
            day = _day(values["meeting_date"])
            if day is None or day > as_of:
                dropped["ep_future_or_undated"] += 1
                continue
            ids = tuple(sorted(set(TR_ID.findall(values["lobbyist_id"]))))
            names = tuple(n for n in (_name(part) for part in values["attendees"].split("|")) if n)
            attendees = (
                *(Attendee(tr_id=tr_id, name="") for tr_id in ids),
                *(Attendee(tr_id="", name=name) for name in names),
            )
            title = _title(values["title"])
            member_name = _name(values["member_name"])
            meetings.append(
                Meeting(
                    record_id=_digest(member_id, day, title, sorted(names), ids),
                    day=day,
                    title=title,
                    actor_id=member_id,
                    actor_name=member_name,
                    attendees=attendees,
                    record_url=mep_meetings_url(member_id, member_name),
                    details=(
                        ("capacity", _clean(values["member_capacity"])),
                        ("procedure", _clean(values["procedure_reference"])),
                    ),
                )
            )
    except csv.Error as exc:
        raise EuContactsError("CSV de reuniões do Parlamento Europeu inválido.") from exc
    return meetings


def _unescaped(element: ET.Element, path: str) -> str:
    # The Commission escapes text twice ("&amp;#248;"): undo the second layer.
    return _clean(html.unescape(_child_text(element, path)))


def cabinet_id(label: str) -> str:
    return f"cabinet:{slugify(label)}"


def parse_ec_xml(data: bytes, *, key: str, as_of: date, dropped: Counter[str]) -> list[Meeting]:
    """Meetings of the file's Portuguese Commissioner's cabinet; staff names are not kept."""
    cabinet = EC_FILES[key]
    meetings: list[Meeting] = []
    try:
        # DTDs/entities are refused up front; Expat's built-in amplification limits apply.
        root = ET.fromstring(sanitise_xml(refuse_dtd(data)))  # noqa: S314
    except ET.ParseError as exc:
        raise EuContactsError("XML de reuniões da Comissão Europeia inválido.") from exc
    if root.tag != "meetings":
        raise EuContactsError("Estrutura inesperada no XML de reuniões da Comissão Europeia.")
    for element in root.iter("meeting"):
        # Exact label as published (no whitespace normalisation): another label is another cabinet.
        label = element.find("cabinet")
        if label is None or html.unescape(label.text or "") != cabinet:
            continue
        day = _day(_child_text(element, "date"))
        if day is None or day > as_of:
            dropped["ec_future_or_undated"] += 1
            continue
        attendees: list[Attendee] = []
        for entity in element.iterfind("entities/entity"):
            name = _name(_unescaped(entity, "name"))
            ids = sorted(set(TR_ID.findall(_child_text(entity, "id"))))
            if len(ids) == 1:
                attendees.append(Attendee(tr_id=ids[0], name=name))
            elif ids:
                # One cell with several ids cannot be paired with its names.
                attendees.extend(Attendee(tr_id=tr_id, name="") for tr_id in ids)
            elif name:
                attendees.append(Attendee(tr_id="", name=name))
        title = _title(_unescaped(element, "subject"))
        pairs = sorted((a.tr_id, a.name) for a in attendees)
        meetings.append(
            Meeting(
                record_id=_digest(key, cabinet, day, title, pairs),
                day=day,
                title=title,
                actor_id=cabinet_id(cabinet),
                actor_name=cabinet,
                attendees=tuple(attendees),
                details=(("location", _unescaped(element, "location")[:200]),),
            )
        )
    return meetings


def _unique(meetings: Iterable[Meeting], dropped: Counter[str], key: str) -> tuple[Meeting, ...]:
    """Exact duplicates are published twice by the source; keep one."""
    seen: dict[str, Meeting] = {}
    for meeting in meetings:
        if meeting.record_id in seen:
            dropped[key] += 1
            continue
        seen[meeting.record_id] = meeting
    return tuple(seen.values())


# Fetching ----------------------------------------------------------------------------


def _register_allowed(url: str) -> bool:
    parts = urlsplit(url)
    prefix = REGISTER_HOSTS.get(parts.hostname or "")
    return parts.scheme == "https" and prefix is not None and parts.path.startswith(prefix)


def _ep_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == EP_HOST and parts.path == EP_EXPORT_PATH


def _ep_api_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == EP_API_HOST and parts.path == EP_API_PATH


def _ec_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == EC_HOST and parts.path == EC_PATH


class _Session:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.requests = 0

    def get(self, url: str, *, allowed: Callable[[str], bool], max_bytes: int) -> bytes:
        if self.requests and REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL)
        self.requests += 1
        try:
            return download(url, allowed=allowed, max_bytes=max_bytes, deadline=self.deadline)
        except OfficialHTTPError as exc:
            # A 202 from the EP export is its anti-bot challenge: stop, never retry around it.
            if _ep_allowed(url) and str(exc) in {
                "A fonte oficial devolveu HTTP 202.",
                "A fonte oficial devolveu HTTP 403.",
            }:
                raise EpMeetingsBlocked(
                    "Exportação de reuniões do Parlamento Europeu bloqueada (HTTP 202/403); "
                    "nenhum bloqueio de acesso foi contornado."
                ) from exc
            raise EuContactsError(
                "Não foi possível obter a fonte europeia em segurança (bloqueio ou falha)."
            ) from exc


def _months(first: tuple[int, int], as_of: date) -> Iterator[tuple[date, date]]:
    year, month = first
    while (year, month) <= (as_of.year, as_of.month):
        start = date(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        yield start, date(year, month, 1) - timedelta(days=1)


def ep_export_url(start: date, end: date, members: Iterable[str]) -> str:
    query = [
        ("exportFormat", "CSV"),
        ("fromDate", start.strftime("%d/%m/%Y")),
        ("toDate", end.strftime("%d/%m/%Y")),
        *(("memberIds", member) for member in sorted(members, key=int)),
    ]
    return f"https://{EP_HOST}{EP_EXPORT_PATH}?{urlencode(query)}"


def portuguese_meps(session: _Session | None = None) -> frozenset[str]:
    """PT MEP ids already imported (``ep`` person identities), else the EP API list."""
    known = frozenset(
        external_id
        for external_id in SourceIdentity.objects.filter(
            source=EP, entity__kind=Entity.Kind.PERSON
        ).values_list("external_id", flat=True)
        if MEP_ID.fullmatch(external_id)
    )
    if known:
        return known
    session = session or _Session()
    query = urlencode(
        {
            "country-of-representation": "PT",
            "format": "application/ld+json",
            "offset": 0,
            "limit": 500,
        }
    )
    content = session.get(
        f"https://{EP_API_HOST}{EP_API_PATH}?{query}",
        allowed=_ep_api_allowed,
        max_bytes=MAX_API_BYTES,
    )
    try:
        payload = json.loads(content)
        items = cast(list[dict[str, object]], payload["data"])
        ids = frozenset(str(item["identifier"]) for item in items)
    except (ValueError, KeyError, TypeError) as exc:
        raise EuContactsError("Lista de deputados do Parlamento Europeu inválida.") from exc
    if not ids or len(ids) >= 500 or not all(MEP_ID.fullmatch(i) for i in ids):
        raise EuContactsError("Lista de deputados do Parlamento Europeu inválida.")
    return ids


def _ep_window(
    session: _Session,
    start: date,
    end: date,
    members: frozenset[str],
    as_of: date,
    dropped: Counter[str],
) -> list[Meeting]:
    content = session.get(
        ep_export_url(start, end, members), allowed=_ep_allowed, max_bytes=MAX_EP_BYTES
    )
    rows = content.count(b"\n")
    if rows >= EP_ROW_CAP:
        if len(members) == 1:
            raise EuContactsError("Exportação do Parlamento Europeu truncada (limite de linhas).")
        # The export silently caps at 1000 rows: split the month by member.
        meetings: list[Meeting] = []
        for member in sorted(members, key=int):
            meetings.extend(_ep_window(session, start, end, frozenset({member}), as_of, dropped))
        return meetings
    return parse_ep_csv(content, members=members, as_of=as_of, dropped=dropped)


def fetch_snapshot(
    *,
    datasets: frozenset[str],
    as_of: date,
    first_month: tuple[int, int] | None = None,
    skip_blocked_ep: bool = False,
) -> Snapshot:
    """Read complete sources; optionally exclude a blocked EP export without changing it."""
    session = _Session()
    dropped: Counter[str] = Counter()
    registrants = parse_register(
        session.get(REGISTER_URL, allowed=_register_allowed, max_bytes=MAX_REGISTER_BYTES)
    )
    ep_months: dict[str, tuple[Meeting, ...]] = {}
    warnings: list[str] = []
    if EP_MEETINGS in datasets:
        ep_dropped: Counter[str] = Counter()
        try:
            members = portuguese_meps(session)
            for start, end in _months(first_month or EP_FIRST_MONTH, as_of):
                meetings = _ep_window(session, start, end, members, as_of, ep_dropped)
                ep_months[start.strftime("%Y-%m")] = _unique(meetings, ep_dropped, "ep_duplicates")
        except EpMeetingsBlocked as exc:
            if not skip_blocked_ep:
                raise
            # Never apply a partial EP snapshot, including months collected before refusal.
            ep_months.clear()
            datasets = datasets - {EP_MEETINGS}
            warnings.append(f"{exc} Dataset {EP_MEETINGS} ignorado; dados existentes intactos.")
        else:
            dropped.update(ep_dropped)
    ec_files: dict[str, tuple[Meeting, ...]] = {}
    if EC_MEETINGS in datasets:
        for key in EC_FILES:
            content = session.get(
                f"https://{EC_HOST}{EC_PATH}?{urlencode({'name': key})}",
                allowed=_ec_allowed,
                max_bytes=MAX_EC_BYTES,
            )
            meetings = parse_ec_xml(content, key=key, as_of=as_of, dropped=dropped)
            ec_files[key] = _unique(meetings, dropped, "ec_duplicates")
    return Snapshot(
        as_of=as_of,
        datasets=datasets,
        registrants=registrants,
        ep_months=ep_months,
        ec_files=ec_files,
        dropped=dropped,
        warnings=tuple(warnings),
    )


# Applying ----------------------------------------------------------------------------


def _meetings(snapshot: Snapshot) -> Iterator[Meeting]:
    for group in (*snapshot.ep_months.values(), *snapshot.ec_files.values()):
        yield from group


def _cited_ids(snapshot: Snapshot) -> set[str]:
    cited = {a.tr_id for meeting in _meetings(snapshot) for a in meeting.attendees if a.tr_id}
    # Meetings imported by earlier runs keep their organisations resolvable.
    stored = EventParty.objects.filter(
        event__dataset__in=(EP_MEETINGS, EC_MEETINGS), identifier__startswith=f"{EU_TR}:"
    ).values_list("identifier", flat=True)
    cited.update(identifier.split(":", 1)[1] for identifier in stored.distinct())
    return cited


def resolve_organisations(snapshot: Snapshot) -> tuple[dict[str, Entity], Counter[str]]:
    """``eu_tr`` identity, else wait for a pending suggestion, else a new organisation."""
    counts: Counter[str] = Counter()
    wanted = {r.tr_id for r in snapshot.registrants.values() if r.portuguese} | _cited_ids(snapshot)
    candidates = sorted(
        tr_id
        for tr_id in wanted
        if tr_id in snapshot.registrants and not snapshot.registrants[tr_id].person
    )
    resolved = {
        identity.external_id: identity.entity
        for identity in SourceIdentity.objects.select_related("entity").filter(
            source=EU_TR, external_id__in=candidates
        )
    }
    pending = set(
        IdentitySuggestion.objects.filter(
            scheme=EU_TR,
            external_id__in=[i for i in candidates if i not in resolved],
            status=IdentitySuggestion.Status.PENDING,
        ).values_list("external_id", flat=True)
    )
    create: dict[str, tuple[str, str, str]] = {}
    for tr_id in candidates:
        if tr_id in resolved or tr_id in pending:
            continue
        registrant = snapshot.registrants[tr_id]
        create[tr_id] = (registrant.name, *classify(registrant.category, registrant.name))
    resolved.update(official_entities_bulk(EU_TR, create))
    counts["organisations"] = len(candidates)
    counts["organisations_created"] = len(create)
    counts["organisations_pending_review"] = len(pending)
    counts["organisations_portuguese"] = sum(
        1 for tr_id in candidates if snapshot.registrants[tr_id].portuguese
    )
    return resolved, counts


def _matches(name: str, registrant: Registrant | None, entity: Entity | None) -> bool:
    key = normalise_name(name)
    labels = [registrant.name, registrant.acronym] if registrant else []
    if entity is not None:
        labels.append(entity.name)
    return bool(key) and any(key == normalise_name(label) for label in labels if label)


def _organisation_parties(
    meeting: Meeting,
    *,
    registrants: dict[str, Registrant],
    organisations: dict[str, Entity],
    counts: Counter[str],
) -> list[PartyInput]:
    parties: list[PartyInput] = []
    ids = [a.tr_id for a in meeting.attendees if a.tr_id]
    covered: set[str] = set()
    free = [a.name for a in meeting.attendees if not a.tr_id]
    for tr_id in dict.fromkeys(ids):
        registrant = registrants.get(tr_id)
        entity = organisations.get(tr_id)
        # A name published with the id is covered by it (natural persons included).
        covered.update(a.name for a in meeting.attendees if a.tr_id == tr_id and a.name)
        covered.update(n for n in free if _matches(n, registrant, entity))
        if registrant is not None and registrant.person:
            counts["self_employed_dropped"] += 1
            continue
        name = entity.name if entity else registrant.name if registrant else tr_id
        parties.append(
            PartyInput(
                role=EventParty.Role.ATTENDEE,
                name=_name(name),
                entity=entity,
                identifier=f"{EU_TR}:{tr_id}",
            )
        )
    remaining = [n for n in free if n not in covered]
    # Unpaired names in a meeting that lists one id per name are the same organisations.
    if len(free) == len(set(ids)) and not any(a.tr_id and a.name for a in meeting.attendees):
        remaining = []
    for name in remaining:
        if ORGANISATION_MARKERS.search(name) is None:
            counts["possible_persons_dropped"] += 1
            continue
        parties.append(PartyInput(role=EventParty.Role.ATTENDEE, name=name))
    return parties


def _event(meeting: Meeting, actor: PartyInput, organisations: list[PartyInput]) -> EventInput:
    return EventInput(
        record_id=meeting.record_id,
        kind=Event.Kind.MEETING,
        title=meeting.title,
        date=meeting.day,
        record_url=meeting.record_url,
        details={key: value for key, value in meeting.details if value},
        parties=(actor, *organisations),
    )


def _ep_events(
    meetings: tuple[Meeting, ...],
    *,
    snapshot: Snapshot,
    organisations: dict[str, Entity],
    meps: dict[str, Entity],
    counts: Counter[str],
) -> Iterator[EventInput]:
    for meeting in meetings:
        parties = _organisation_parties(
            meeting,
            registrants=snapshot.registrants,
            organisations=organisations,
            counts=counts,
        )
        if not parties:
            counts["without_organisation"] += 1
            continue
        actor = PartyInput(
            role=EventParty.Role.ATTENDEE,
            name=meeting.actor_name,
            entity=meps.get(meeting.actor_id),
            identifier=f"{EP}:{meeting.actor_id}",
        )
        yield _event(meeting, actor, parties)


def _ec_events(
    meetings: tuple[Meeting, ...],
    *,
    snapshot: Snapshot,
    organisations: dict[str, Entity],
    counts: Counter[str],
) -> Iterator[EventInput]:
    cabinets: dict[str, Entity] = {}
    for meeting in meetings:
        parties = _organisation_parties(
            meeting,
            registrants=snapshot.registrants,
            organisations=organisations,
            counts=counts,
        )
        if not parties:
            counts["without_organisation"] += 1
            continue
        cabinet = cabinets.get(meeting.actor_id)
        if cabinet is None:
            cabinet = cabinets[meeting.actor_id] = official_entity(
                EC,
                meeting.actor_id,
                name=meeting.actor_name,
                kind=Entity.Kind.ORGANISATION,
                classification=Entity.Classification.EU_INSTITUTION,
            )
        actor = PartyInput(
            role=EventParty.Role.HOST,
            name=meeting.actor_name,
            entity=cabinet,
            identifier=f"{EC}:{meeting.actor_id}",
        )
        yield _event(meeting, actor, parties)


def apply_snapshot(snapshot: Snapshot) -> dict[str, int]:
    """Atomic: organisations first, then one complete event scope per month or file."""
    with import_transaction():
        organisations, counts = resolve_organisations(snapshot)
        members = sorted({m.actor_id for group in snapshot.ep_months.values() for m in group})
        meps = {
            identity.external_id: identity.entity
            for identity in SourceIdentity.objects.select_related("entity").filter(
                source=EP, external_id__in=members, entity__kind=Entity.Kind.PERSON
            )
        }
        result: Counter[str] = Counter()
        for month, meetings in snapshot.ep_months.items():
            result.update(
                sync_events(
                    dataset=EP_MEETINGS,
                    scope=month,
                    events=_ep_events(
                        meetings,
                        snapshot=snapshot,
                        organisations=organisations,
                        meps=meps,
                        counts=counts,
                    ),
                    as_of=snapshot.as_of,
                )
            )
        for key, meetings in snapshot.ec_files.items():
            result.update(
                sync_events(
                    dataset=EC_MEETINGS,
                    scope=key,
                    events=_ec_events(
                        meetings, snapshot=snapshot, organisations=organisations, counts=counts
                    ),
                    as_of=snapshot.as_of,
                )
            )
        result.update(counts)
        result.update(snapshot.dropped)
        return dict(result)


def parse_month(value: str) -> tuple[int, int]:
    match = MONTH.fullmatch(value)
    if match is None:
        raise ValueError("mês inválido")
    return int(match.group(1)), int(match.group(2))
