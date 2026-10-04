"""Shape published relationships for reading: grouped by kind, placed on a shared time axis."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Case, F, IntegerField, Q, Value, When
from django.urls import reverse
from django.utils import timezone

from ligacoes.core.catalogue import DATASETS, IDENTIFIER_SCHEMES, identifier_url
from ligacoes.core.models import (
    ANCHOR_SCHEMES,
    Entity,
    Event,
    EventParty,
    IdentityScheme,
    Relationship,
    TemporalStatus,
)

from .selectors import (
    DECLARATION_DATASETS,
    event_counterparts,
    event_totals,
    identity_provenance,
    matched_aliases,
    public_connection_counts,
    public_relationships,
    search_entities,
    with_declared,
)

Kind = Relationship.Kind
# Reading order: public roles first, then economic ties, organisational structure and
# personal background.
KIND_ORDER = [
    Kind.PUBLIC_OFFICE,
    Kind.DIRECTORSHIP,
    Kind.SHAREHOLDING,
    Kind.EMPLOYMENT,
    Kind.PROFESSIONAL_ACTIVITY,
    Kind.DECLARED_CLIENT,
    Kind.MEMBERSHIP,
    Kind.PART_OF,
    Kind.SUCCESSION,
    Kind.EDUCATION,
    Kind.FAMILY,
]
# Entities known only by a name in one source: never verified, never linked by name.
PROVENANCE = {
    IdentityScheme.DECLARED_NAME: "Sem identificador oficial — nome como declarado na fonte",
    IdentityScheme.SCOPED_NAME: "Identificada apenas pelo nome publicado na fonte",
}
# Money first, then contacts: the declaration order of Event.Kind.
EVENT_KIND_ORDER = list(Event.Kind.values)
COUNTERPART_LIMIT = 10
MIN_AXIS_YEARS = 4
MAX_TICKS = 8


def ordered_for(entity, relationships, split=True):
    """Deterministic reading order shared by the list and the graph: officially documented
    connections before declared interests, then kind, counterpart and time.

    ``split=False`` skips the declared check, for a profile known to have no declared
    interest (the order is then the same)."""
    if split:
        relationships = with_declared(relationships)
    return relationships.annotate(
        kind_rank=Case(
            *(When(kind=kind, then=Value(rank)) for rank, kind in enumerate(KIND_ORDER)),
            default=Value(len(KIND_ORDER)),
            output_field=IntegerField(),
        ),
        counterpart_name=Case(
            When(subject=entity, then=F("object__name")), default=F("subject__name")
        ),
    ).order_by(
        *(["declared"] if split else []),
        "kind_rank",
        "counterpart_name",
        F("start_date").asc(nulls_first=True),
        "pk",
    )


def counterpart_filter(entity, query):
    matching = search_entities(query).values("pk")
    return Q(subject=entity, object__in=matching) | Q(object=entity, subject__in=matching)


def profile_relationships(entity, at, query="", kind="", declared=False, split=True):
    """Published connections of one profile, in the reading order shared by list and graph.

    ``query`` keeps counterparts matching a search, ``kind`` one relationship kind and
    ``declared`` only declared interests; ``split`` as in ``ordered_for``."""
    relationships = public_relationships(at).filter(Q(subject=entity) | Q(object=entity))
    if query:
        relationships = relationships.filter(counterpart_filter(entity, query))
    if kind:
        relationships = relationships.filter(kind=kind)
    listed = ordered_for(entity, relationships, split or declared).select_related("term")
    return listed.filter(declared=True) if declared else listed


@dataclass(frozen=True)
class Axis:
    """Whole calendar years from January 1 of `first` to December 31 of `last`, inclusive."""

    first: int
    last: int
    ticks: tuple[tuple[int, float], ...] = ()

    def position(self, day, after=False):
        """Percentage position of the start of `day`, or of its end when `after` is set."""
        span = (date(self.last, 12, 31) - date(self.first, 1, 1)).days + 1
        days = (day - date(self.first, 1, 1)).days + (1 if after else 0)
        return round(min(max(days / span, 0), 1) * 100, 2)


def tick_step(years):
    """Smallest 1-2-5 step (in years) that keeps the axis at MAX_TICKS labels or fewer."""
    scale = 1
    while True:
        for base in (1, 2, 5):
            if (years - 1) // (base * scale) + 1 <= MAX_TICKS:
                return base * scale
        scale *= 10


def build_axis(days):
    days = [day for day in days if day]
    if not days:
        return None
    first, last = min(days).year, max(days).year
    if last - first + 1 < MIN_AXIS_YEARS:
        first = max(last - MIN_AXIS_YEARS + 1, 1)
    step = tick_step(last - first + 1)
    axis = Axis(first, last)
    ticks = tuple(
        (year, axis.position(date(year, 1, 1)))
        for year in range(first + (-first) % step, last + 1, step)
    )
    return Axis(first, last, ticks)


@dataclass(frozen=True)
class Span:
    """Known bounds are solid; an undocumented bound is drawn as an open, lighter region.

    A current connection without an end date runs, solid, to today (``ongoing``)."""

    left: float
    width: float
    open_start: bool
    open_end: bool
    ongoing: bool = False


def build_span(axis, start, end, current_until=None):
    if axis is None:
        return None
    left = axis.position(start) if start else 0.0
    ongoing = end is None and current_until is not None
    if end:
        right = axis.position(end, after=True)
    elif ongoing:
        right = axis.position(current_until, after=True)
    else:
        right = 100.0
    # No minimum width: a very short period is drawn as its two (overlapping) date markers
    # rather than stretched past a documented end date.
    return Span(
        left, max(round(right - left, 2), 0.0), start is None, end is None and not ongoing, ongoing
    )


def relationship_span(axis, relationship, today):
    current = relationship.temporal_status == TemporalStatus.CURRENT
    return build_span(
        axis, relationship.start_date, relationship.end_date, today if current else None
    )


@dataclass
class Row:
    counterpart: object
    outgoing: bool
    periods: list = field(default_factory=list)
    counterpart_connections: int = 0

    @property
    def others(self):
        """Other published connections of the counterpart, excluding this profile's own."""
        return max(self.counterpart_connections - len(self.periods), 0)


@dataclass
class Group:
    kind: str
    label: str
    total: int
    declared: bool = False
    rows: list = field(default_factory=list)

    @property
    def anchor(self):
        return f"tipo-{self.kind}-declarado" if self.declared else f"tipo-{self.kind}"


def declaration_date(relationship):
    """Latest publication date of the declarations behind ``relationship``, if any."""
    days = [
        evidence.source.published_at
        for evidence in relationship.public_evidence
        if evidence.source.dataset in DECLARATION_DATASETS and evidence.source.published_at
    ]
    return timezone.localdate(max(days)) if days else None


def build_groups(entity, relationships, totals, declared_totals, axis, counterpart_counts, today):
    """One row per counterpart and kind; repeated mandates become periods of the same row.

    Declared interests form their own groups, labelled as declared by the person."""
    groups = {}
    rows = {}
    for relationship in relationships:
        outgoing = relationship.subject_id == entity.pk
        counterpart = relationship.object if outgoing else relationship.subject
        declared = bool(getattr(relationship, "declared", False))
        key = (declared, relationship.kind)
        group = groups.get(key)
        if group is None:
            declared_total = declared_totals.get(relationship.kind, 0)
            total = totals.get(relationship.kind, 0)
            group = groups[key] = Group(
                relationship.kind,
                relationship.get_kind_display(),
                declared_total if declared else total - declared_total,
                declared,
            )
        row_key = (*key, counterpart.pk, outgoing)
        row = rows.get(row_key)
        if row is None:
            row = rows[row_key] = Row(
                counterpart,
                outgoing,
                counterpart_connections=sum(counterpart_counts[counterpart.pk].values()),
            )
            group.rows.append(row)
        relationship.span = relationship_span(axis, relationship, today)
        relationship.declared_on = declaration_date(relationship) if declared else None
        row.periods.append(relationship)
    return list(groups.values())


@dataclass(frozen=True)
class Summary:
    total: int
    counterparts: int
    sources: int
    first: date | None
    last: date | None
    breakdown: tuple
    declared: int = 0


def build_summary(totals, declared_totals, counterparts, sources, days):
    """Totals are per-kind counts of every published connection; ``days`` their date bounds."""
    return Summary(
        sum(totals.values()),
        counterparts,
        sources,
        min(days, default=None),
        max(days, default=None),
        breakdown(totals),
        sum(declared_totals.values()),
    )


def breakdown(totals):
    """(kind, label, count, percentage) in reading order, skipping kinds with no connections."""
    total = sum(totals.values())
    labels = dict(Kind.choices)
    return tuple(
        (kind, labels[kind], totals[kind], round(totals[kind] * 100 / total, 2))
        for kind in KIND_ORDER
        if totals.get(kind)
    )


@dataclass(frozen=True)
class Listing:
    """A directory entry with its published connection count and kind breakdown.

    ``alias`` is the official alternative name that matched a search; ``provenance`` the
    caveat of an entity known by no official identifier."""

    entity: object
    total: int
    breakdown: tuple
    alias: str = ""
    provenance: str = ""


def listings(entities, query=""):
    entities = list(entities)
    counts = public_connection_counts(entity.pk for entity in entities)
    aliases = matched_aliases(entities, query) if query else {}
    provenance = identity_provenance(entity.pk for entity in entities)
    return [
        Listing(
            entity,
            sum(counts[entity.pk].values()),
            breakdown(counts[entity.pk]),
            aliases.get(entity.pk, ""),
            PROVENANCE.get(provenance.get(entity.pk, ""), ""),
        )
        for entity in entities
    ]


THOUSANDS = "\u202f"
NBSP = "\u00a0"


def money(amount: Decimal | None, currency: str = "EUR") -> str:
    """pt-PT amount: narrow spaces between thousands, decimal comma, currency after."""
    if amount is None:
        return ""
    whole, cents = f"{amount:,.2f}".split(".")
    symbol = "€" if currency == "EUR" else currency
    return f"{whole.replace(',', THOUSANDS)},{cents}{NBSP}{symbol}"


@dataclass(frozen=True)
class Identifier:
    label: str
    value: str
    url: str | None
    hint: bool


def identifiers(identities) -> list[Identifier]:
    """Official identifiers first; hints (Wikidata) last and marked as such."""
    shown = [
        Identifier(
            IDENTIFIER_SCHEMES[identity.source].label,
            identity.external_id,
            identifier_url(identity.source, identity.external_id),
            identity.source not in ANCHOR_SCHEMES,
        )
        for identity in identities
        # Project keys for institutions are not identifiers published by the source.
        if not identity.external_id.startswith("institution:")
    ]
    return sorted(shown, key=lambda item: (item.hint, item.label, item.value))


@dataclass(frozen=True)
class DatasetUse:
    """A catalogue dataset behind a profile; an empty key groups uncatalogued documents."""

    key: str
    title: str
    publisher: str
    relationships: int
    events: int


def dataset_uses(evidence_rows, event_rows) -> list[DatasetUse]:
    counts: dict[str, list[int]] = {}
    for position, rows in enumerate((evidence_rows, event_rows)):
        for row in rows:
            key = row["dataset"] if row["dataset"] in DATASETS else ""
            counts.setdefault(key, [0, 0])[position] += row["count"]
    uses = [
        DatasetUse(
            key,
            DATASETS[key].title if key else "Outros documentos citados",
            DATASETS[key].publisher if key else "",
            relationships,
            events,
        )
        for key, (relationships, events) in counts.items()
    ]
    return sorted(uses, key=lambda use: (not use.key, -use.relationships - use.events, use.title))


def events_url(entity, *, kind="", counterpart=None, at=None) -> str:
    """Drill-down list of one profile's public events, keeping the observed date."""
    query = urlencode(
        {
            key: value
            for key, value in (
                ("tipo", kind),
                ("com", counterpart.slug if counterpart else ""),
                ("at", at.isoformat() if at else ""),
            )
            if value
        }
    )
    url = reverse("public:entity_events", kwargs={"slug": entity.slug})
    return f"{url}?{query}" if query else url


@dataclass(frozen=True)
class Counterpart:
    entity: Entity
    role: str
    own_role: str
    count: int
    amount: str
    first: date | None
    last: date | None
    url: str


@dataclass(frozen=True)
class EventSection:
    kind: str
    label: str
    count: int
    amount: str
    first: date | None
    last: date | None
    url: str
    counterparts: tuple[Counterpart, ...]
    more: bool


def _counterparts_by_kind(entity, kinds, at):
    """Per kind, the top ``COUNTERPART_LIMIT + 1`` counterpart rows in their usual order.

    A date makes every call aggregate the entity's events live, so one call serves all
    kinds and the rows (already aggregated, in one global order) are split here. Without
    a date the stored summaries answer each kind cheaply.
    """
    limit = COUNTERPART_LIMIT + 1
    if at is None:
        return {kind: list(event_counterparts(entity, kind=kind)[:limit]) for kind in kinds}
    rows = {kind: [] for kind in kinds}
    for row in event_counterparts(entity, at=at):
        kept = rows.get(row["kind"])
        if kept is not None and len(kept) < limit:
            kept.append(row)
    return rows


def event_sections(entity, at=None, breakdown=None) -> list[EventSection]:
    """Per event kind: totals and the counterparts with the largest amounts or counts.

    ``breakdown`` is an ``event_breakdown`` the caller already computed for ``at``.
    """
    totals = {row["kind"]: row for row in event_totals(entity, at=at, breakdown=breakdown)}
    kinds = [kind for kind in EVENT_KIND_ORDER if kind in totals]
    rows = _counterparts_by_kind(entity, kinds, at)
    entities = Entity.objects.filter(is_public=True).in_bulk(
        {row["counterpart_id"] for kind in kinds for row in rows[kind][:COUNTERPART_LIMIT]}
    )
    roles = dict(EventParty.Role.choices)
    labels = dict(Event.Kind.choices)
    sections = []
    for kind in kinds:
        total = totals[kind]
        counterparts = tuple(
            Counterpart(
                counterpart,
                roles.get(row["role_of_counterpart"], row["role_of_counterpart"]),
                roles.get(row["role_of_entity"], row["role_of_entity"]),
                row["count"],
                money(row["amount_total"]),
                row["first_date"],
                row["last_date"],
                events_url(entity, kind=kind, counterpart=counterpart, at=at),
            )
            for row in rows[kind][:COUNTERPART_LIMIT]
            if (counterpart := entities.get(row["counterpart_id"])) is not None
        )
        sections.append(
            EventSection(
                kind,
                labels[kind],
                total["count"],
                money(total["amount_total"]),
                total["first_date"],
                total["last_date"],
                events_url(entity, kind=kind, at=at),
                counterparts,
                len(rows[kind]) > COUNTERPART_LIMIT,
            )
        )
    return sections
