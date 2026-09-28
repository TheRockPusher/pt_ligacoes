"""Shape published relationships for reading: grouped by kind, placed on a shared time axis."""

from dataclasses import dataclass, field
from datetime import date

from django.db.models import Case, F, IntegerField, Q, Value, When

from ligacoes.core.models import Relationship

from .selectors import public_connection_counts

Kind = Relationship.Kind
# Reading order: public roles first, then economic ties, then personal background.
KIND_ORDER = [
    Kind.PUBLIC_OFFICE,
    Kind.DIRECTORSHIP,
    Kind.SHAREHOLDING,
    Kind.EMPLOYMENT,
    Kind.PROFESSIONAL_ACTIVITY,
    Kind.MEMBERSHIP,
    Kind.EDUCATION,
    Kind.FAMILY,
]
MIN_AXIS_YEARS = 4
MAX_TICKS = 8


def ordered_for(entity, relationships):
    """Deterministic reading order shared by the list and the graph: kind, counterpart, time."""
    return relationships.annotate(
        kind_rank=Case(
            *(When(kind=kind, then=Value(rank)) for rank, kind in enumerate(KIND_ORDER)),
            default=Value(len(KIND_ORDER)),
            output_field=IntegerField(),
        ),
        counterpart_name=Case(
            When(subject=entity, then=F("object__name")), default=F("subject__name")
        ),
    ).order_by("kind_rank", "counterpart_name", F("start_date").asc(nulls_first=True), "pk")


def counterpart_filter(entity, query):
    return Q(subject=entity, object__name__icontains=query) | Q(
        object=entity, subject__name__icontains=query
    )


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
    """Known bounds are solid; an undocumented bound is drawn as an open, lighter region."""

    left: float
    width: float
    open_start: bool
    open_end: bool


def build_span(axis, start, end):
    if axis is None:
        return None
    left = axis.position(start) if start else 0.0
    right = axis.position(end, after=True) if end else 100.0
    # No minimum width: a very short period is drawn as its two (overlapping) date markers
    # rather than stretched past a documented end date.
    return Span(left, max(round(right - left, 2), 0.0), start is None, end is None)


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
    rows: list = field(default_factory=list)


def build_groups(entity, relationships, totals, axis, counterpart_counts):
    """One row per counterpart and kind; repeated mandates become periods of the same row."""
    groups = {}
    rows = {}
    for relationship in relationships:
        outgoing = relationship.subject_id == entity.pk
        counterpart = relationship.object if outgoing else relationship.subject
        group = groups.get(relationship.kind)
        if group is None:
            group = groups[relationship.kind] = Group(
                relationship.kind,
                relationship.get_kind_display(),
                totals.get(relationship.kind, 0),
            )
        key = (relationship.kind, counterpart.pk, outgoing)
        row = rows.get(key)
        if row is None:
            row = rows[key] = Row(
                counterpart,
                outgoing,
                counterpart_connections=sum(counterpart_counts[counterpart.pk].values()),
            )
            group.rows.append(row)
        relationship.span = build_span(axis, relationship.start_date, relationship.end_date)
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


def build_summary(entity, facts, sources):
    """Facts are (subject_id, object_id, kind, start, end) for every published connection."""
    totals = {}
    counterparts = set()
    days = []
    for subject_id, object_id, kind, start, end in facts:
        totals[kind] = totals.get(kind, 0) + 1
        counterparts.add(object_id if subject_id == entity.pk else subject_id)
        days.extend(day for day in (start, end) if day)
    total = len(facts)
    return (
        Summary(
            total,
            len(counterparts),
            sources,
            min(days, default=None),
            max(days, default=None),
            breakdown(totals),
        ),
        totals,
        days,
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
    """A directory entry with its published connection count and kind breakdown."""

    entity: object
    total: int
    breakdown: tuple


def listings(entities):
    entities = list(entities)
    counts = public_connection_counts(entity.pk for entity in entities)
    return [
        Listing(entity, sum(counts[entity.pk].values()), breakdown(counts[entity.pk]))
        for entity in entities
    ]
