"""Complete constitutional-Government archive states, without invented office boundaries."""

import hashlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import GOVERNMENT_OFFICE, ObservationInput, sync_observations
from .government import revised
from .identity import (
    OfficeContext,
    normalise_name,
    official_entity,
    scoped_person,
    scoped_person_id,
)
from .models import (
    EnrichmentSource,
    Entity,
    Evidence,
    IdentityScheme,
    Relationship,
    Source,
    SourceIdentity,
    SourceObservation,
    TemporalStatus,
    Term,
    import_transaction,
)
from .official_http import OfficialHTTPError, download
from .services import publish_imported

GOVERNMENTS = tuple(f"gc{number:02d}" for number in range(1, 21))
ORIGIN = "https://www.historico.portugal.gov.pt"
ROOT = "/pt/o-governo/arquivo-historico/governos-constitucionais"
INDEX_URL = ORIGIN + ROOT + ".aspx"
ARCHIVE = DATASETS["gov_arquivo_historico"]
GOVERNMENT = EnrichmentSource.GOVERNMENT
MAX_BYTES = 1024 * 1024
MAX_REQUESTS = 100
MAX_TOTAL_BYTES = 32 * MAX_BYTES
TOTAL_TIMEOUT = 300
REQUEST_INTERVAL = 0.15
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class GovernmentArchiveError(ValueError):
    """An incomplete or unsafe archive response must not replace a snapshot."""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["_Node | str"] = field(default_factory=list)

    def text(self) -> str:
        parts = (child.text() if isinstance(child, _Node) else child for child in self.children)
        return " ".join(" ".join(parts).split())

    def nodes(self, tag: str = ""):
        for child in self.children:
            if isinstance(child, _Node):
                if not tag or child.tag == tag:
                    yield child
                yield from child.nodes(tag)

    def has_class(self, value: str) -> bool:
        return value in self.attrs.get("class", "").split()

    def direct(self, tag: str):
        return [child for child in self.children if isinstance(child, _Node) and child.tag == tag]


class _HTML(HTMLParser):
    def __init__(self, content: bytes):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.count = 0
        if len(content) > MAX_BYTES:
            raise GovernmentArchiveError("Página do arquivo demasiado extensa.")
        try:
            self.feed(content.decode("utf-8-sig"))
            self.close()
        except (UnicodeError, RecursionError) as exc:
            raise GovernmentArchiveError("HTML do arquivo inválido.") from exc

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 20000 or len(self.stack) > 100:
            raise GovernmentArchiveError("Estrutura do arquivo demasiado extensa.")
        # Never retain photographs, hidden input values or other personal fields.
        node = _Node(
            tag,
            {
                key: value
                for key, value in attrs
                if key in {"class", "id", "href"} and value is not None
            },
        )
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in {"script", "style", "cite"}:
            self.stack[-1].children.append(data)


def composition_url(government: str) -> str:
    if government not in GOVERNMENTS:
        raise GovernmentArchiveError("Seleção de Governo inválida (gc01 a gc20).")
    return f"{ORIGIN}{ROOT}/{government}/composicao.aspx"


def allowed_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.historico.portugal.gov.pt"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
            or "\\" in url
            or any(ord(char) < 33 for char in url)
            or len(url) > 2048
        ):
            return False
        if parsed.path == ROOT + ".aspx":
            return not parsed.query
        match = re.fullmatch(re.escape(ROOT) + r"/(gc\d{2})/composicao\.aspx", parsed.path)
        if not match or match[1] not in GOVERNMENTS:
            return False
        if not parsed.query:
            return True
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
        if set(query) != {"date"} or len(query["date"]) != 1:
            return False
        value = query["date"][0]
        return (
            bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
            and date.fromisoformat(value).year >= 1976
        )
    except ValueError:
        return False


def parse_index(content: bytes) -> dict[str, str]:
    root = _HTML(content).root
    result = {}
    for node in root.nodes("li"):
        for link in node.direct("a"):
            url = urlsplit(urljoin(INDEX_URL, link.attrs.get("href", "")))
            if url.scheme != "https" or url.netloc != urlsplit(ORIGIN).netloc:
                continue
            match = re.fullmatch(re.escape(ROOT) + r"/(gc\d{2})\.aspx", url.path)
            if match and match[1] in GOVERNMENTS:
                spans = node.direct("span")
                name = spans[0].text() if spans else ""
                if not name or len(name) > 160:
                    raise GovernmentArchiveError("Designação de Governo ausente no índice.")
                result[match[1]] = name
    if not result:
        raise GovernmentArchiveError("Índice dos Governos Constitucionais ausente.")
    return result


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    role: str
    portfolio: str
    source_url: str
    observed_on: date | None

    @property
    def key(self) -> tuple[str, str, str]:
        return normalise_name(self.name), self.role, self.portfolio


@dataclass(frozen=True)
class ArchivePage:
    members: tuple[ArchiveMember, ...]
    dates: tuple[date, ...]


@dataclass(frozen=True)
class GovernmentArchiveSnapshot:
    government: str
    name: str
    start_date: date
    end_date: date | None
    members: tuple[ArchiveMember, ...]
    pages: tuple[str, ...]
    as_of: date


def parse_page(content: bytes, *, government: str, source_url: str) -> ArchivePage:
    base = composition_url(government)
    if not allowed_url(source_url) or urlsplit(source_url).path != urlsplit(base).path:
        raise GovernmentArchiveError("Página fora do Governo pedido.")
    root = _HTML(content).root
    dates = set()
    for link in root.nodes("a"):
        url = urljoin(base, link.attrs.get("href", ""))
        target = urlsplit(url)
        if (
            target.netloc == urlsplit(base).netloc
            and target.path == urlsplit(base).path
            and "date=" in target.query
        ):
            if not allowed_url(url):
                raise GovernmentArchiveError("Ligação de composição datada inválida.")
            query = parse_qs(target.query)
            dates.add(date.fromisoformat(query["date"][0]))
    containers = [
        node for node in root.nodes("div") if node.attrs.get("id", "").endswith("governmentPeople")
    ]
    if len(containers) != 1:
        raise GovernmentArchiveError("Bloco da composição do arquivo ausente ou repetido.")
    observed = parse_qs(urlsplit(source_url).query).get("date", [None])[0]
    observed_on = date.fromisoformat(observed) if observed else None
    members = []

    def cards(node: _Node, portfolio: str = "") -> None:
        subtitle = [span.text() for span in node.direct("span") if span.has_class("subTitle")]
        current_portfolio = subtitle[0] if subtitle else portfolio
        if node.direct("cite"):
            names = [
                span.text()
                for span in node.direct("span")
                if not span.attrs.get("class") and span.text()
            ]
            roles = [
                heading.text()
                for heading in node.direct("h3")
                if heading.has_class("mainForecolor") and heading.text()
            ]
            if (
                len(names) != 1
                or not roles
                or any(len(value) > 240 for value in [*names, *roles, current_portfolio])
            ):
                raise GovernmentArchiveError("Cartão de membro do Governo incompleto.")
            for role in roles:
                members.append(
                    ArchiveMember(names[0], role, current_portfolio, source_url, observed_on)
                )
            if node.has_class("firstHistory"):
                current_portfolio = "Primeiro-Ministro"
        for child in node.children:
            if isinstance(child, _Node) and child.tag in {"div", "ul", "li"}:
                cards(child, current_portfolio)

    cards(containers[0])
    # Reshuffle-day pages can repeat the same secretary under outgoing and incoming
    # ministers. Identical visible assertions are one presence, not two offices.
    members = list(dict.fromkeys(members))
    if len(members) > 200 or len({member.key for member in members}) != len(members):
        raise GovernmentArchiveError(
            f"Composição do arquivo excessiva ou repetida: {source_url}; "
            f"cartões={len(members)}; únicos={len({member.key for member in members})}."
        )
    return ArchivePage(tuple(members), tuple(sorted(dates)))


class _Collector:
    def __init__(self):
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.cache: dict[str, bytes] = {}
        self.total_bytes = 0
        self.last_request = 0.0

    def fetch(self, url: str) -> bytes:
        if url in self.cache:
            return self.cache[url]
        if not allowed_url(url) or len(self.cache) >= MAX_REQUESTS:
            raise GovernmentArchiveError("Limite ou destino do arquivo inválido.")
        pause = max(0.0, self.last_request + REQUEST_INTERVAL - time.monotonic())
        if time.monotonic() + pause >= self.deadline:
            raise GovernmentArchiveError("Tempo máximo de recolha do arquivo excedido.")
        time.sleep(pause)
        self.last_request = time.monotonic()
        try:
            content = download(
                url, allowed=allowed_url, max_bytes=MAX_BYTES, deadline=self.deadline
            )
        except OfficialHTTPError as exc:
            raise GovernmentArchiveError(str(exc)) from exc
        self.total_bytes += len(content)
        if self.total_bytes > MAX_TOTAL_BYTES:
            raise GovernmentArchiveError("Limite total de recolha do arquivo excedido.")
        self.cache[url] = content
        return content


def fetch_snapshot(*, government: str, as_of: date | None = None) -> GovernmentArchiveSnapshot:
    base = composition_url(government)
    collector = _Collector()
    index = parse_index(collector.fetch(INDEX_URL))
    if government not in index:
        raise GovernmentArchiveError("Governo pedido ausente do índice oficial.")
    default = parse_page(collector.fetch(base), government=government, source_url=base)
    if not default.dates:
        raise GovernmentArchiveError("Datas de posse e remodelações ausentes.")
    pending = set(default.dates)
    seen: set[date] = set()
    members = list(default.members)
    pages = [base]
    while pending - seen:
        day = min(pending - seen)
        url = f"{base}?date={day.isoformat()}"
        page = parse_page(collector.fetch(url), government=government, source_url=url)
        members.extend(page.members)
        pages.append(url)
        seen.add(day)
        pending.update(page.dates)
    if not members:
        raise GovernmentArchiveError("Nenhuma composição histórica contém membros.")
    number = GOVERNMENTS.index(government)
    end = None
    if number + 1 < len(GOVERNMENTS):
        successor = GOVERNMENTS[number + 1]
        successor_url = composition_url(successor)
        successor_page = parse_page(
            collector.fetch(successor_url), government=successor, source_url=successor_url
        )
        if successor_page.dates:
            end = min(successor_page.dates)
    start = min(seen)
    if end is not None and end <= start:
        raise GovernmentArchiveError("Datas dos Governos sucessivos incoerentes.")
    return GovernmentArchiveSnapshot(
        government,
        index[government],
        start,
        end,
        tuple(members),
        tuple(pages),
        as_of or timezone.localdate(),
    )


def _passage(member: ArchiveMember) -> str:
    text = f"{member.name} — {member.role}."
    if member.portfolio:
        text += f" Pasta: {member.portfolio}."
    if member.observed_on:
        text += (
            f" Presença documentada na composição de {member.observed_on.isoformat()}; "
            "não indica datas individuais de posse ou cessação."
        )
    else:
        text += " Composição sem data individual de posse ou cessação."
    return text


def apply_snapshot(snapshot: GovernmentArchiveSnapshot) -> dict[str, int]:
    """One office per person/role/portfolio; every advertised state remains exact evidence."""
    composition_url(snapshot.government)
    if not snapshot.members:
        raise GovernmentArchiveError("Não é possível aplicar uma composição histórica vazia.")
    with import_transaction():
        institution = official_entity(
            GOVERNMENT,
            f"government:{snapshot.government}",
            name=snapshot.name,
            kind=Entity.Kind.ORGANISATION,
            classification=Entity.Classification.GOVERNMENT,
        )
        # Terms use inclusive days, as in the modern Government importer.
        last = snapshot.end_date - timedelta(days=1) if snapshot.end_date else None
        term, _ = Term.objects.update_or_create(
            kind=Term.Kind.GOVERNMENT,
            code=snapshot.government,
            defaults={
                "label": snapshot.name,
                "institution": institution,
                "start_date": snapshot.start_date,
                "end_date": last,
            },
        )
        groups: dict[tuple[str, str, str], list[ArchiveMember]] = defaultdict(list)
        first_presence: dict[str, date] = {}
        for member in snapshot.members:
            groups[member.key].append(member)
            if member.observed_on is not None:
                name_key = member.key[0]
                first_presence[name_key] = min(
                    first_presence.get(name_key, member.observed_on), member.observed_on
                )
        observations = []
        records = {}
        people = {}
        for key, members in sorted(groups.items()):
            # Prefer a dated state over the unqualified default composition.
            members.sort(
                key=lambda member: (member.observed_on is None, member.observed_on or date.max)
            )
            first = members[0]
            if key[0] not in people:
                person = scoped_person(
                    f"gov_archive:{snapshot.government}",
                    first.name,
                    basis=(
                        f"Composição oficial do {snapshot.name}; identidade limitada "
                        "a este Governo salvo corroborada por outro cargo oficial."
                    ),
                    # Identity corroboration uses the first evidenced presence only;
                    # it does not turn that observation into an appointment boundary.
                    offices=[OfficeContext(institution, first_presence.get(key[0]), None)],
                )
                people[key[0]] = SourceIdentity.objects.select_related("entity").get(
                    source=IdentityScheme.SCOPED_NAME,
                    external_id=scoped_person_id(f"gov_archive:{snapshot.government}", first.name),
                    entity=person,
                )
            external_id = "office:" + hashlib.sha256("\0".join(key).encode()).hexdigest()
            dates = sorted({member.observed_on for member in members if member.observed_on})
            passage = _passage(first)
            if len(dates) > 1:
                passage += (
                    " Outras presenças documentadas: "
                    + ", ".join(day.isoformat() for day in dates[1:])
                    + "."
                )
            role_class = (
                Relationship.RoleClass.DEPUTY_LEADERSHIP
                if re.match(r"(?:Sub)?secretári[oa]|Ministr[oa] Adjunt[oa]", first.role, re.I)
                else Relationship.RoleClass.LEADERSHIP
            )
            observations.append(
                revised(
                    ObservationInput(
                        external_id=external_id,
                        revision="",
                        category=GOVERNMENT_OFFICE,
                        passage=passage,
                        source_url=first.source_url,
                        publisher=ARCHIVE.publisher,
                        reference=f"{snapshot.government}; composição histórica",
                        title=f"{ARCHIVE.title} — {snapshot.name}",
                        identity=people[key[0]],
                        subject_name=first.name,
                        object=institution,
                        kind=Relationship.Kind.PUBLIC_OFFICE,
                        dataset=ARCHIVE.key,
                        role=first.role,
                        role_class=role_class,
                        term=term,
                        temporal_status=(
                            TemporalStatus.ENDED if snapshot.end_date else TemporalStatus.UNKNOWN
                        ),
                    )
                )
            )
            records[external_id] = members
        scope = f"government-archive:{snapshot.government}"
        result = sync_observations(
            source=GOVERNMENT,
            scope=scope,
            observations=tuple(observations),
            as_of=snapshot.as_of,
        )
        current = SourceObservation.objects.filter(
            source=GOVERNMENT, scope=scope, is_current=True
        ).select_related("relationship", "evidence__source")
        sources: dict[str, Source] = {}
        for observation in current:
            relation = observation.relationship
            if relation is None or relation.status == Relationship.Status.REJECTED:
                continue
            added = False
            for member in records[observation.external_id]:
                if member.source_url == observation.source_url:
                    continue
                source = sources.get(member.source_url)
                if source is None:
                    source = (
                        Source.objects.filter(
                            dataset=ARCHIVE.key,
                            url=member.source_url,
                            title=observation.title,
                            publisher=ARCHIVE.publisher,
                        )
                        .order_by("pk")
                        .first()
                    )
                    if source is None:
                        source = Source.objects.create(
                            dataset=ARCHIVE.key,
                            url=member.source_url,
                            title=observation.title,
                            publisher=ARCHIVE.publisher,
                            is_public=True,
                        )
                    elif not source.is_public:
                        source.is_public = True
                        source.save()
                    sources[member.source_url] = source
                _, created = Evidence.objects.get_or_create(
                    relationship=relation,
                    source=source,
                    excerpt=_passage(member),
                    page_reference=f"{snapshot.government}; composição histórica",
                    defaults={"is_public": True},
                )
                added |= created
            if added:
                # Adding evidence invalidates the first publication; approve the complete set.
                publish_imported(relation)
        return result
