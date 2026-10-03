"""“Como estão ligados?”: the shortest documented paths between two public entities.

A bidirectional breadth-first search over published relationships and, on request,
aggregated public event pairs. Each level is one set-based query per side; frontier,
visited and row caps plus a statement timeout bound the work, and every cut is reported.
"""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from urllib.parse import urlencode
from uuid import UUID

import psycopg.errors
from django.db import OperationalError, connection, transaction
from django.db.models import F, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from ligacoes.core.models import Entity, Event, EventPairSummary, EventParty, Relationship

from .profile import PROVENANCE
from .selectors import (
    _events,
    event_counterparts,
    identity_provenance,
    matched_aliases,
    public_connection_counts,
    public_relationships,
    search_entities,
    with_declared,
)
from .views import QUERY_LIMIT, date_bad_request, follow_merges, selected_date

DEFAULT_DEPTH = 4
MAX_DEPTH = 6
MAX_PATHS = 3
# Structural hubs join almost everything to everything; crossing them says little.
HUB_CLASSIFICATIONS = frozenset(
    {
        Entity.Classification.PARLIAMENT,
        Entity.Classification.GOVERNMENT,
        Entity.Classification.PARLIAMENTARY_GROUP,
        Entity.Classification.EU_INSTITUTION,
    }
)
HUB_DEGREE = 300
FRONTIER_LIMIT = 2_000
VISITED_LIMIT = 20_000
LEVEL_ROW_LIMIT = 20_000
EVENT_NEIGHBOURS = 25
SEARCH_TIMEOUT_MS = 5_000
OPTION_LIMIT = 10
OPTION_MIN_LENGTH = 2
# Best-ranked name matches re-ordered by published connections, so that the prominent
# holder of a common surname is offered first.
OPTION_CANDIDATES = 100
HOP_RELATIONSHIP_LIMIT = 5
HOP_EVENT_LIMIT = 5
HUB_DISPLAY_LIMIT = 20
INCLUDE_EVENTS = "eventos"
INCLUDE_HUBS = "centrais"
PICKERS = (("de", "De"), ("para", "Para"))

RELATIONSHIP = "relationship"
EVENT = "event"
BY_CLASSIFICATION = "classification"
BY_DEGREE = "degree"

Pair = frozenset[UUID]
Neighbour = tuple[UUID, UUID, str, str]

# Table names come from model metadata and the event rule is compiled by the ORM
# (``public_events`` via ``_events``); every value is a bound parameter.
EVENT_PAIRS_SQL = """
SELECT own_id, other_id, classification, total FROM (
    SELECT own.entity_id AS own_id, other.entity_id AS other_id,
        entity.classification AS classification,
        ROW_NUMBER() OVER (
            PARTITION BY own.entity_id
            ORDER BY COUNT(DISTINCT own.event_id) DESC, other.entity_id
        ) AS node_rank,
        COUNT(*) OVER (PARTITION BY own.entity_id) AS total
    FROM {party} own
    JOIN {party} other ON other.event_id = own.event_id AND other.entity_id <> own.entity_id
    JOIN {entity} entity ON entity.id = other.entity_id
    WHERE own.entity_id = ANY(%s) AND own.event_id IN ({events})
    GROUP BY own.entity_id, other.entity_id, entity.classification
) ranked
WHERE node_rank <= %s
ORDER BY own_id, node_rank
LIMIT %s
"""

# The same ranking from ``EventPairSummary`` (no date): every counterpart is rechecked.
STORED_EVENT_PAIRS_SQL = """
SELECT own_id, other_id, classification, total FROM (
    SELECT pair.entity_id AS own_id, pair.counterpart_id AS other_id,
        other.classification AS classification,
        ROW_NUMBER() OVER (
            PARTITION BY pair.entity_id
            ORDER BY SUM(pair.event_count) DESC, pair.counterpart_id
        ) AS node_rank,
        COUNT(*) OVER (PARTITION BY pair.entity_id) AS total
    FROM {summary} pair
    JOIN {entity} own ON own.id = pair.entity_id AND own.is_public
    JOIN {entity} other ON other.id = pair.counterpart_id AND other.is_public
    WHERE pair.entity_id = ANY(%s)
    GROUP BY pair.entity_id, pair.counterpart_id, other.classification
) ranked
WHERE node_rank <= %s
ORDER BY own_id, node_rank
LIMIT %s
"""


@dataclass
class Side:
    """One end of the search; predecessors point back towards this side's root."""

    parents: dict[UUID, list[UUID]]
    depths: dict[UUID, int]
    frontier: list[UUID]
    depth: int = 0

    @classmethod
    def rooted(cls, root: UUID) -> "Side":
        return cls({root: []}, {root: 0}, [root])

    def chains(self, node: UUID) -> Iterator[list[UUID]]:
        """Recorded shortest chains from ``node`` back to the root."""
        predecessors = self.parents[node]
        if not predecessors:
            yield [node]
            return
        for predecessor in predecessors:
            for chain in self.chains(predecessor):
                yield [node, *chain]


@dataclass(frozen=True)
class Search:
    paths: list[tuple[UUID, ...]]
    links: dict[Pair, str]
    excluded: dict[UUID, str]
    truncated: bool
    timed_out: bool
    events_capped: bool

    @property
    def length(self) -> int | None:
        return len(self.paths[0]) - 1 if self.paths else None


class PathSearch:
    def __init__(
        self, *, at: date | None, max_depth: int, include_events: bool, include_hubs: bool
    ) -> None:
        self.at = at
        self.max_depth = max_depth
        self.include_events = include_events
        self.include_hubs = include_hubs
        self.endpoints: frozenset[UUID] = frozenset()
        self.links: dict[Pair, str] = {}
        self.excluded: dict[UUID, str] = {}
        self.truncated = False
        self.events_capped = False

    def result(self, paths: list[tuple[UUID, ...]], *, timed_out: bool = False) -> Search:
        return Search(
            paths=paths,
            links=self.links,
            excluded=self.excluded,
            truncated=self.truncated or timed_out,
            timed_out=timed_out,
            events_capped=self.events_capped,
        )

    def run(self, start: UUID, end: UUID) -> Search:
        self.endpoints = frozenset({start, end})
        forward, backward = Side.rooted(start), Side.rooted(end)
        meeting: list[UUID] = []
        while (
            forward.depth + backward.depth < self.max_depth
            and forward.frontier
            and backward.frontier
        ):
            # Expanding the smaller frontier keeps each level's query small.
            if len(forward.frontier) <= len(backward.frontier):
                meeting = self.expand(forward, backward)
            else:
                meeting = self.expand(backward, forward)
            if meeting:
                break
            if len(forward.depths) + len(backward.depths) > VISITED_LIMIT:
                self.truncated = True
                break
        return self.result(self.join(forward, backward, meeting))

    def expand(self, side: Side, other: Side) -> list[UUID]:
        frontier = side.frontier
        if len(frontier) > FRONTIER_LIMIT:
            frontier = frontier[:FRONTIER_LIMIT]
            self.truncated = True
        found: dict[UUID, list[UUID]] = {}
        for node, neighbour, via, classification in self.neighbours(frontier):
            if neighbour in side.depths or neighbour in self.excluded:
                continue
            if self.structural(neighbour, classification):
                self.excluded[neighbour] = BY_CLASSIFICATION
                continue
            predecessors = found.setdefault(neighbour, [])
            if node not in predecessors and len(predecessors) < MAX_PATHS:
                predecessors.append(node)
                # Relationship rows come first, so a documented relationship wins.
                self.links.setdefault(frozenset((node, neighbour)), via)
        side.depth += 1
        if not self.include_hubs:
            last_level = side.depth + other.depth >= self.max_depth
            # On the last level only meeting points matter; nothing else is expanded.
            self.drop_busy(found, meeting_only=other.depths if last_level else None)
        for node, predecessors in found.items():
            side.parents[node] = predecessors
            side.depths[node] = side.depth
        side.frontier = sorted(found)
        return sorted(node for node in found if node in other.depths)

    def structural(self, node: UUID, classification: str) -> bool:
        return (
            not self.include_hubs
            and node not in self.endpoints
            and classification in HUB_CLASSIFICATIONS
        )

    def drop_busy(
        self, found: dict[UUID, list[UUID]], *, meeting_only: dict[UUID, int] | None
    ) -> None:
        candidates = [
            node
            for node in found
            if node not in self.endpoints and (meeting_only is None or node in meeting_only)
        ]
        if not candidates:
            return
        counts = public_connection_counts(candidates)
        for node in candidates:
            if sum(counts[node].values()) > HUB_DEGREE:
                del found[node]
                self.excluded[node] = BY_DEGREE

    def neighbours(self, frontier: list[UUID]) -> list[Neighbour]:
        members = set(frontier)
        rows = list(
            public_relationships(self.at)
            .prefetch_related(None)
            .order_by()
            .filter(Q(subject_id__in=frontier) | Q(object_id__in=frontier))
            .values_list(
                "subject_id", "object_id", "subject__classification", "object__classification"
            )
            .distinct()[: LEVEL_ROW_LIMIT + 1]
        )
        if len(rows) > LEVEL_ROW_LIMIT:
            rows = rows[:LEVEL_ROW_LIMIT]
            self.truncated = True
        neighbours: list[Neighbour] = []
        for subject, obj, subject_class, object_class in rows:
            if subject in members:
                neighbours.append((subject, obj, RELATIONSHIP, object_class))
            if obj in members:
                neighbours.append((obj, subject, RELATIONSHIP, subject_class))
        if self.include_events:
            neighbours.extend(self.event_neighbours(frontier))
        return neighbours

    def event_neighbours(self, frontier: list[UUID]) -> list[Neighbour]:
        """Counterparts in public events, the busiest ``EVENT_NEIGHBOURS`` per node."""
        quote = connection.ops.quote_name
        entity = quote(Entity._meta.db_table)
        if self.at is None:
            sql = STORED_EVENT_PAIRS_SQL.format(
                summary=quote(EventPairSummary._meta.db_table), entity=entity
            )
            params = [list(frontier), EVENT_NEIGHBOURS, LEVEL_ROW_LIMIT + 1]
        else:
            events_sql, events_params = (
                _events(at=self.at).order_by().values("pk").query.sql_with_params()
            )
            sql = EVENT_PAIRS_SQL.format(
                party=quote(EventParty._meta.db_table), entity=entity, events=events_sql
            )
            params = [list(frontier), *events_params, EVENT_NEIGHBOURS, LEVEL_ROW_LIMIT + 1]
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        if len(rows) > LEVEL_ROW_LIMIT:
            rows = rows[:LEVEL_ROW_LIMIT]
            self.truncated = True
        neighbours: list[Neighbour] = []
        for own, other, classification, total in rows:
            if total > EVENT_NEIGHBOURS:
                self.events_capped = True
            neighbours.append((own, other, EVENT, classification))
        return neighbours

    def join(self, forward: Side, backward: Side, meeting: list[UUID]) -> list[tuple[UUID, ...]]:
        if not meeting:
            return []
        shortest = min(forward.depths[node] + backward.depths[node] for node in meeting)
        paths: list[tuple[UUID, ...]] = []
        for node in meeting:
            if forward.depths[node] + backward.depths[node] != shortest:
                continue
            for head in forward.chains(node):
                for tail in backward.chains(node):
                    path = (*reversed(head), *tail[1:])
                    if len(set(path)) == len(path):
                        paths.append(path)
                    if len(paths) == MAX_PATHS:
                        return paths
        return paths


def find_paths(
    start: UUID,
    end: UUID,
    *,
    at: date | None = None,
    max_depth: int = DEFAULT_DEPTH,
    include_events: bool = False,
    include_hubs: bool = False,
) -> Search:
    search = PathSearch(
        at=at, max_depth=max_depth, include_events=include_events, include_hubs=include_hubs
    )
    try:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(f"SET LOCAL statement_timeout = {SEARCH_TIMEOUT_MS:d}")
            return search.run(start, end)
    except OperationalError as error:
        if not isinstance(error.__cause__, psycopg.errors.QueryCanceled):
            raise
        return search.result([], timed_out=True)


@dataclass(frozen=True)
class EventLink:
    kind: str
    label: str
    role_of_start: str
    role_of_end: str
    count: int
    amount_total: Decimal | None
    first_date: date | None
    last_date: date | None


@dataclass(frozen=True)
class Hop:
    start: Entity
    end: Entity
    relationships: list[Relationship]
    more: int
    events: list[EventLink]
    events_url: str

    @property
    def kind(self) -> str:
        return self.relationships[0].kind if self.relationships else ""

    @property
    def label(self) -> str:
        if self.relationships:
            return self.relationships[0].get_kind_display()
        return self.events[0].label if self.events else "Registos oficiais"


@dataclass(frozen=True)
class Path:
    nodes: list[Entity]
    hops: list[Hop]


def relationship_claims(pairs: list[Pair], at: date | None) -> dict[Pair, list[Relationship]]:
    """Every published relationship behind each hop, in one query."""
    if not pairs:
        return {}
    condition = Q()
    for pair in pairs:
        first, second = sorted(pair)
        condition |= Q(subject_id=first, object_id=second) | Q(subject_id=second, object_id=first)
    relationships = (
        with_declared(public_relationships(at))
        .select_related("term")
        .filter(condition)
        .order_by(F("start_date").desc(nulls_last=True), "kind", "pk")
    )
    claims: dict[Pair, list[Relationship]] = defaultdict(list)
    for relationship in relationships:
        # Only approved evidence was prefetched by the shared visibility selector.
        if relationship.public_evidence:
            claims[frozenset((relationship.subject_id, relationship.object_id))].append(
                relationship
            )
    return claims


def event_links(start: Entity, end: Entity, at: date | None) -> list[EventLink]:
    rows = event_counterparts(start, at=at, counterpart=end)[:HOP_EVENT_LIMIT]
    return [
        EventLink(
            kind=row["kind"],
            label=Event.Kind(row["kind"]).label,
            role_of_start=EventParty.Role(row["role_of_entity"]).label,
            role_of_end=EventParty.Role(row["role_of_counterpart"]).label,
            count=row["count"],
            amount_total=row["amount_total"],
            first_date=row["first_date"],
            last_date=row["last_date"],
        )
        for row in rows
    ]


def events_url(start: Entity, end: Entity) -> str:
    url = reverse("public:entity_events", kwargs={"slug": start.slug})
    return f"{url}?{urlencode({'com': end.slug})}"


def build_paths(search: Search, at: date | None) -> list[Path]:
    """Readable hops for each path: relationships with evidence, or aggregated events."""
    if not search.paths:
        return []
    ids = {node for path in search.paths for node in path}
    entities = Entity.objects.filter(is_public=True).in_bulk(list(ids))
    if not ids.issubset(entities):
        # An entity was withdrawn while the search ran: show nothing rather than guess.
        return []
    pairs = {frozenset(pair) for path in search.paths for pair in pairwise(path)}
    claims = relationship_claims(
        [pair for pair in pairs if search.links.get(pair) == RELATIONSHIP], at
    )
    events: dict[Pair, list[EventLink]] = {}
    paths: list[Path] = []
    for path in search.paths:
        hops: list[Hop] = []
        for first, second in pairwise(path):
            start, end = entities[first], entities[second]
            pair = frozenset((first, second))
            if search.links.get(pair) == RELATIONSHIP:
                listed = claims.get(pair, [])
                if not listed:
                    # Withdrawn while the search ran: never show a hop without its source.
                    return []
                hops.append(
                    Hop(
                        start,
                        end,
                        listed[:HOP_RELATIONSHIP_LIMIT],
                        max(len(listed) - HOP_RELATIONSHIP_LIMIT, 0),
                        [],
                        "",
                    )
                )
            else:
                if pair not in events:
                    events[pair] = event_links(start, end, at)
                hops.append(Hop(start, end, [], 0, events[pair], events_url(start, end)))
        paths.append(Path([entities[node] for node in path], hops))
    return paths


def excluded_hubs(search: Search) -> list[tuple[Entity, str]]:
    ids = list(search.excluded)
    if not ids:
        return []
    shown = Entity.objects.filter(pk__in=ids, is_public=True).order_by("name", "pk")[
        :HUB_DISPLAY_LIMIT
    ]
    return [
        (
            entity,
            entity.get_classification_display()
            if search.excluded[entity.pk] == BY_CLASSIFICATION
            else f"mais de {HUB_DEGREE} ligações publicadas",
        )
        for entity in shown
    ]


@dataclass(frozen=True)
class Option:
    """A picker suggestion: the entity, the official alias that matched and its provenance."""

    entity: Entity
    alias: str
    provenance: str


def entity_matches(query: str) -> list[Option]:
    """The same search as the directory: exact names, then names starting with the query,
    then the most connected."""
    if len(query) < OPTION_MIN_LENGTH:
        return []
    ranks = dict(
        search_entities(query)
        .annotate(rank=F("search_rank"))
        .order_by("search_rank", "name", "pk")
        .values_list("pk", "rank")[:OPTION_CANDIDATES]
    )
    candidates = Entity.objects.filter(is_public=True).in_bulk(list(ranks))
    counts = public_connection_counts(ranks)
    entities = sorted(
        candidates.values(),
        key=lambda entity: (
            ranks[entity.pk],
            -sum(counts[entity.pk].values()),
            entity.name,
            entity.pk,
        ),
    )[:OPTION_LIMIT]
    aliases = matched_aliases(entities, query)
    provenance = identity_provenance(entity.pk for entity in entities)
    return [
        Option(
            entity,
            aliases.get(entity.pk, ""),
            PROVENANCE.get(provenance.get(entity.pk, ""), ""),
        )
        for entity in entities
    ]


@dataclass(frozen=True)
class Picker:
    field: str
    legend: str
    entity: Entity | None
    query: str
    options: list[Option]
    missing: bool


def picker(request: HttpRequest, field: str, legend: str) -> Picker:
    slug = request.GET.get(field, "")
    raw = request.GET.get(f"{field}_q", "")
    if len(raw) > QUERY_LIMIT:
        raise ValueError("Pesquisa demasiado longa.")
    query = raw.strip()
    entity = Entity.objects.filter(slug=slug, is_public=True).first() if slug else None
    return Picker(field, legend, entity, query, entity_matches(query), bool(slug) and not entity)


def selected_depth(request: HttpRequest) -> int:
    value = request.GET.get("max", "")
    if not value:
        return DEFAULT_DEPTH
    if not (value.isascii() and value.isdigit() and 1 <= int(value) <= MAX_DEPTH):
        raise ValueError("Número de passos inválido.")
    return int(value)


def selected_includes(request: HttpRequest) -> set[str]:
    values = set(request.GET.getlist("incluir"))
    if not values <= {INCLUDE_EVENTS, INCLUDE_HUBS}:
        raise ValueError("Opção inválida.")
    return values


@require_GET
@follow_merges("de", "para")
def path_finder(request: HttpRequest) -> HttpResponse:
    try:
        at = selected_date(request)
        depth = selected_depth(request)
        includes = selected_includes(request)
        pickers = [picker(request, field, legend) for field, legend in PICKERS]
    except ValueError:
        return date_bad_request(request)
    start, end = pickers[0].entity, pickers[1].entity
    include_events = INCLUDE_EVENTS in includes
    include_hubs = INCLUDE_HUBS in includes
    search = None
    paths: list[Path] = []
    if start and end and start.pk != end.pk:
        search = find_paths(
            start.pk,
            end.pk,
            at=at,
            max_depth=depth,
            include_events=include_events,
            include_hubs=include_hubs,
        )
        paths = build_paths(search, at)
    hubs_url = ""
    if search and search.excluded and not include_hubs:
        query = request.GET.copy()
        query.setlist("incluir", [*sorted(includes), INCLUDE_HUBS])
        hubs_url = f"{reverse('public:path_finder')}?{query.urlencode()}#resultado"
    return render(
        request,
        "public/path_finder.html",
        {
            "pickers": pickers,
            "start": start,
            "end": end,
            "same": bool(start and end and start.pk == end.pk),
            "search": search,
            "paths": paths,
            "excluded": excluded_hubs(search) if search else [],
            "excluded_total": len(search.excluded) if search else 0,
            "hubs_url": hubs_url,
            "selected_date": at.isoformat() if at else "",
            "depth": depth,
            "depth_choices": range(1, MAX_DEPTH + 1),
            "include_events": include_events,
            "include_hubs": include_hubs,
            "hub_classifications": [
                Entity.Classification(value).label for value in sorted(HUB_CLASSIFICATIONS)
            ],
            "hub_degree": HUB_DEGREE,
            "event_neighbours": EVENT_NEIGHBOURS,
            "option_limit": OPTION_LIMIT,
            "option_min_length": OPTION_MIN_LENGTH,
        },
    )


@require_GET
def entity_options(request: HttpRequest) -> HttpResponse:
    """Autocomplete for the pickers: public entities only, never more than ``OPTION_LIMIT``."""
    field = request.GET.get("campo", PICKERS[0][0])
    if field not in dict(PICKERS):
        return date_bad_request(request)
    raw = request.GET.get("q", request.GET.get(f"{field}_q", ""))
    if len(raw) > QUERY_LIMIT:
        return date_bad_request(request)
    query = raw.strip()
    return render(
        request,
        "public/_path_entities.html",
        {
            "field": field,
            "query": query,
            "options": entity_matches(query),
            "option_limit": OPTION_LIMIT,
            "option_min_length": OPTION_MIN_LENGTH,
        },
    )
