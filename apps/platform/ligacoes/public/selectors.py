from collections import Counter, defaultdict
from typing import Any

from django.db import connection
from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Concat

from ligacoes.core.catalogue import IDENTIFIER_SCHEMES
from ligacoes.core.event_summaries import money_pair_sql
from ligacoes.core.identity import normalise_name
from ligacoes.core.models import (
    ANCHOR_SCHEMES,
    NAME_ONLY_SCHEMES,
    Entity,
    EntityAlias,
    Event,
    EventEntitySummary,
    EventPairSummary,
    EventParty,
    Evidence,
    Relationship,
    SourceIdentity,
    TemporalStatus,
)

PUBLIC_EVIDENCE_LIMIT = 10
PUBLIC_RELATIONSHIP_LIMIT = 100
# Internal ids (Government portal, EpT, Commission) are never shown.
PUBLIC_IDENTIFIER_SCHEMES = frozenset(
    scheme for scheme, info in IDENTIFIER_SCHEMES.items() if info.public
)
# Declarations of interests: what the person declared, not independently verified.
DECLARATION_DATASETS = frozenset({"ept_declaracoes", "ar_registo_interesses"})
SEARCH_TOKEN_LIMIT = 8
ALIAS_LIMIT = 6
CURRENT_OFFICE_LIMIT = 6


def public_relationships(at=None):
    """The single public-visibility rule shared by HTML, evidence and graph views."""
    public_evidence = Evidence.objects.filter(is_public=True, source__is_public=True)
    relationships = (
        Relationship.objects.filter(
            status=Relationship.Status.PUBLISHED,
            reviewed_at__isnull=False,
            subject__is_public=True,
            object__is_public=True,
        )
        .filter(Exists(public_evidence.filter(relationship_id=OuterRef("pk"))))
        .select_related("subject", "object")
    )
    if at is not None:
        relationships = relationships.filter(
            Q(start_date__isnull=True) | Q(start_date__lte=at),
            Q(end_date__isnull=True) | Q(end_date__gte=at),
        )
    return relationships.prefetch_related(
        Prefetch(
            "evidence",
            # Recheck approval when the second (prefetch) query runs, so a
            # concurrent withdrawal cannot attach newly unreviewed evidence.
            queryset=public_evidence.filter(relationship__in=relationships)
            .select_related("source")
            .order_by("pk")[:PUBLIC_EVIDENCE_LIMIT],
            to_attr="public_evidence",
        )
    )


def public_evidence():
    return Evidence.objects.filter(
        is_public=True,
        source__is_public=True,
        relationship__in=public_relationships(),
    ).select_related("source", "relationship__subject", "relationship__object")


def public_connection_counts(entity_ids):
    """Published connection counts per entity and relationship kind, in either direction."""
    ids = list(entity_ids)
    counts = defaultdict(Counter)
    if not ids:
        return counts
    relationships = public_relationships().prefetch_related(None).order_by()
    for field in ("subject_id", "object_id"):
        rows = (
            relationships.filter(**{f"{field}__in": ids})
            .values_list(field, "kind")
            .annotate(total=Count("pk"))
        )
        for entity_id, kind, total in rows:
            counts[entity_id][kind] += total
    return counts


def declared_condition():
    """A relationship stated in a person's own declaration of interests."""
    declared = Evidence.objects.filter(
        relationship_id=OuterRef("pk"),
        is_public=True,
        source__is_public=True,
        source__dataset__in=DECLARATION_DATASETS,
    )
    return Q(kind=Relationship.Kind.DECLARED_CLIENT) | Q(Exists(declared))


def with_declared(relationships):
    return relationships.annotate(
        declared=ExpressionWrapper(declared_condition(), output_field=BooleanField())
    )


def declared_kind_totals(relationships, totals):
    """Per kind, how many of ``relationships`` (``totals`` per kind) are declared interests.

    Joined from the few declaration sources instead of checked row by row, which would
    probe the evidence of every connection of a hub."""
    counts = {}
    if totals.get(Relationship.Kind.DECLARED_CLIENT):
        counts[Relationship.Kind.DECLARED_CLIENT] = totals[Relationship.Kind.DECLARED_CLIENT]
    rows = (
        relationships.exclude(kind=Relationship.Kind.DECLARED_CLIENT)
        .filter(
            evidence__is_public=True,
            evidence__source__is_public=True,
            evidence__source__dataset__in=DECLARATION_DATASETS,
        )
        .order_by()
        .values_list("kind")
        .annotate(total=Count("pk", distinct=True))
    )
    counts.update(rows)
    return counts


def current_offices(entity):
    """Public offices of ``entity`` its official source reports as current, newest first."""
    return (
        public_relationships()
        .filter(
            subject=entity,
            kind=Relationship.Kind.PUBLIC_OFFICE,
            temporal_status=TemporalStatus.CURRENT,
        )
        .select_related("term")
        .order_by(F("start_date").desc(nulls_last=True), "object__name", "pk")
    )


def search_tokens(query):
    """Accent- and case-free tokens of a search, as names and aliases are normalised."""
    return normalise_name(query).split()[:SEARCH_TOKEN_LIMIT]


def _words(field):
    # A leading space lets " token" match the start of any word, not the middle of one.
    return Concat(Value(" "), F(field), output_field=CharField())


def _matches_words(normalised, tokens):
    words = f" {normalised}"
    return all(f" {token}" in words for token in tokens)


def search_entities(query):
    """Public entities whose name or an official alias has a word starting with every token.

    Exact names rank first (``search_rank`` 0), then names starting with the query (1),
    then the rest (2). Aliases are matched as a set (hashed subplans), never per row.
    """
    tokens = search_tokens(query)
    if not tokens:
        # Same shape as a real search, so callers can still order by ``search_rank``.
        return Entity.objects.none().alias(search_rank=Value(2, output_field=IntegerField()))
    phrase = " ".join(tokens)
    conditions = [Q(words__contains=f" {token}") for token in tokens]
    matching = EntityAlias.objects.alias(words=_words("normalised")).filter(*conditions)
    exact = EntityAlias.objects.filter(normalised=phrase)
    prefixed = EntityAlias.objects.filter(normalised__startswith=phrase)
    rank = Case(
        When(Q(normalised_name=phrase) | Q(pk__in=exact.values("entity_id")), then=Value(0)),
        When(
            Q(normalised_name__startswith=phrase) | Q(pk__in=prefixed.values("entity_id")),
            then=Value(1),
        ),
        default=Value(2),
        output_field=IntegerField(),
    )
    return (
        Entity.objects.filter(is_public=True)
        .alias(words=_words("normalised_name"))
        .filter(Q(*conditions) | Q(pk__in=matching.values("entity_id")))
        .alias(search_rank=rank)
    )


def matched_aliases(entities, query):
    """Per entity whose own name misses a token of ``query``: the official alias that matched."""
    tokens = search_tokens(query)
    ids = [entity.pk for entity in entities if not _matches_words(entity.normalised_name, tokens)]
    if not tokens or not ids:
        return {}
    shown = {}
    rows = (
        EntityAlias.objects.filter(entity_id__in=ids, entity__is_public=True)
        .order_by("entity_id", "name", "pk")
        .values_list("entity_id", "name", "normalised")
    )
    for entity_id, name, normalised in rows:
        if entity_id not in shown and _matches_words(normalised, tokens):
            shown[entity_id] = name
    return shown


def public_aliases(entity):
    """Other names official sources publish for a public profile, with the publishing source.

    Internal source ids are never returned; name-only schemes carry no source label.
    """
    names: dict[str, tuple[str, list[str]]] = {}
    rows = (
        EntityAlias.objects.filter(entity=entity, entity__is_public=True)
        .exclude(normalised=entity.normalised_name)
        .order_by("name", "pk")
        .values_list("name", "normalised", "scheme")
    )
    for name, normalised, scheme in rows:
        shown = names.setdefault(normalised, (name, []))
        info = IDENTIFIER_SCHEMES.get(scheme)
        if info and info.label not in shown[1]:
            shown[1].append(info.label)
    return list(names.values())[:ALIAS_LIMIT]


def identity_provenance(entity_ids):
    """Name-only scheme of each public entity known by no official identifier.

    An entity with any anchoring identifier (shown or internal) is absent; so is one with
    no source identity at all (an editorial profile).
    """
    ids = list(entity_ids)
    if not ids:
        return {}
    schemes = defaultdict(set)
    rows = SourceIdentity.objects.filter(entity_id__in=ids, entity__is_public=True).values_list(
        "entity_id", "source"
    )
    for entity_id, scheme in rows:
        schemes[entity_id].add(scheme)
    return {
        entity_id: min(found & NAME_ONLY_SCHEMES)
        for entity_id, found in schemes.items()
        if found & NAME_ONLY_SCHEMES and not found & ANCHOR_SCHEMES
    }


def _public_conditions(prefix=""):
    """The public-event rule as ``filter()`` arguments for events reached through ``prefix``.

    ``""`` filters ``Event`` itself; ``"event__"`` filters ``EventParty`` rows by their
    event, without a second join to the event table. Two anti-joins rather than one
    ``OR``: an unresolved party is found through the ``entity IS NULL`` slice of
    ``event_party_entity_idx`` and a hidden entity through the (tiny) set of non-public
    entities, so neither has to scan every party row.
    """
    parties = EventParty.objects.filter(event_id=OuterRef("event_id" if prefix else "pk"))
    return (
        Q(**{f"{prefix}status": Event.Status.PUBLISHED, f"{prefix}source__is_public": True}),
        ~Exists(parties.filter(entity__isnull=True)),
        ~Exists(parties.filter(entity__is_public=False)),
    )


def _scope_conditions(kind=None, at=None, prefix=""):
    """Optionally one kind, and only events dated on or before ``at`` (undated kept)."""
    conditions = []
    if kind is not None:
        conditions.append(Q(**{f"{prefix}kind": kind}))
    if at is not None:
        conditions.append(
            Q(**{f"{prefix}date__lte": at})
            | Q(**{f"{prefix}date__isnull": True, f"{prefix}start_date__lte": at})
            | Q(**{f"{prefix}date__isnull": True, f"{prefix}start_date__isnull": True})
        )
    return conditions


def public_events():
    """The single public-visibility rule for events: every party must be a public entity."""
    return Event.objects.filter(*_public_conditions())


def _events(kind=None, at=None):
    """Public events, optionally one kind and only those dated on or before ``at``."""
    return public_events().filter(*_scope_conditions(kind, at))


def _event_summary(prefix=""):
    # Totals never mix currencies: only euro amounts are summed.
    return {
        "amount_total": Sum(f"{prefix}amount", filter=Q(**{f"{prefix}currency": "EUR"})),
        "first_date": Min(Coalesce(f"{prefix}date", f"{prefix}start_date")),
        "last_date": Max(Coalesce(f"{prefix}date", f"{prefix}end_date", f"{prefix}start_date")),
    }


_COUNTERPART_ORDER = (
    F("amount_total").desc(nulls_last=True),
    "-count",
    "counterpart_id",
    "kind",
    "role_of_entity",
    "role_of_counterpart",
)


_COUNTERPART_ROWS_SQL = """
SELECT DISTINCT own.event_ref, c.entity_id AS counterpart_id, own.kind, own.role_of_entity,
       c.role AS role_of_counterpart, own.amount, own.currency, own.first_date, own.last_date
FROM ({own}) AS own
CROSS JOIN LATERAL (
    SELECT c.entity_id, c.role FROM {party} c
    WHERE c.event_id = own.event_ref AND {conditions}
    OFFSET 0
) AS c
"""


def event_counterparts(entity, *, kind=None, at=None, counterpart=None, name=""):
    """Public counterparts of ``entity`` aggregated per kind and pair of roles, in SQL.

    ``counterpart`` keeps one counterpart and ``name`` those whose name contains it.
    Without a date the rows come from ``EventPairSummary`` (counterparts are still
    checked for visibility here); with one they are aggregated from the events.
    """
    if at is None:
        rows = EventPairSummary.objects.filter(
            entity=entity, entity__is_public=True, counterpart__is_public=True
        )
        if kind is not None:
            rows = rows.filter(kind=kind)
        if counterpart is not None:
            rows = rows.filter(counterpart=counterpart)
        if name:
            rows = rows.filter(counterpart__in=search_entities(name).values("pk"))
        return (
            rows.order_by()
            .values(
                "counterpart_id",
                "kind",
                role_of_entity=F("entity_role"),
                role_of_counterpart=F("counterpart_role"),
            )
            .annotate(
                count=Sum("event_count"),
                amount_total=Sum("amount_eur_sum"),
                first_date=Min("period_start"),
                last_date=Max("period_end"),
            )
            .order_by(*_COUNTERPART_ORDER)
        )
    # Aliases can list one entity twice under a role on an event, which would join one event
    # to a pair several times and sum its amount once per row. Every row below is therefore
    # distinct per (event, entity, role, counterpart, counterpart role) before summing.
    # ``SUM(DISTINCT amount)`` would instead merge different events of equal price.
    #
    # The entity's own rows (``event_party_entity_idx``) come first, as a ``DISTINCT``
    # subquery, and the counterpart rows of just those events are fetched by a ``LATERAL``
    # index probe per event (``OFFSET 0`` keeps the planner from flattening it back into a
    # join). Left to itself the planner underestimates the entity's events and hash-joins
    # every party row of the table, which takes seconds for a hub.
    own = (
        EventParty.objects.filter(
            *_public_conditions("event__"), *_scope_conditions(kind, at, "event__"), entity=entity
        )
        .order_by()
        .values(
            event_ref=F("event_id"),
            kind=F("event__kind"),
            role_of_entity=F("role"),
            amount=F("event__amount"),
            currency=F("event__currency"),
            first_date=Coalesce("event__date", "event__start_date"),
            last_date=Coalesce("event__date", "event__end_date", "event__start_date"),
        )
        .distinct()
    )
    own_sql, params = own.query.sql_with_params()
    # Visible events have no unresolved party, so ``<>`` (which drops NULLs) equals
    # excluding the entity itself.
    conditions = ["c.entity_id <> %s"]
    params = [*params, entity.pk]
    if counterpart is not None:
        conditions.append("c.entity_id = %s")
        params.append(counterpart.pk)
    if name:
        names_sql, names_params = (
            search_entities(name).order_by().values("pk").query.sql_with_params()
        )
        conditions.append(f"c.entity_id IN ({names_sql})")
        params.extend(names_params)
    rows = _COUNTERPART_ROWS_SQL.format(
        own=own_sql,
        party=connection.ops.quote_name(EventParty._meta.db_table),
        conditions=" AND ".join(conditions),
    )
    return _PairRows(rows, params)


class _PairRows:
    """Live counterpart rows: per-pair aggregates of an already deduplicated row query.

    Iterable and sliceable like the queryset of the stored path (each slice runs one
    ``LIMIT``/``OFFSET`` statement), yielding the same dictionaries.
    """

    # Mirrors ``_COUNTERPART_ORDER``. Totals never mix currencies, as in ``_event_summary``,
    # and only a money relation sums (see ``event_summaries.MONEY_ROLE_PAIRS``).
    HEAD = """
SELECT counterpart_id, kind, role_of_entity, role_of_counterpart, COUNT(*) AS count,
       SUM(amount) FILTER (WHERE currency = 'EUR' AND {money}) AS amount_total,
       MIN(first_date) AS first_date, MAX(last_date) AS last_date
FROM (
""".format(money=money_pair_sql("role_of_entity", "role_of_counterpart"))
    TAIL = """
) AS pairs
GROUP BY counterpart_id, kind, role_of_entity, role_of_counterpart
ORDER BY amount_total DESC NULLS LAST, count DESC, counterpart_id, kind,
         role_of_entity, role_of_counterpart
"""

    COUNT_HEAD = "SELECT kind, COUNT(DISTINCT counterpart_id) FROM (\n"
    COUNT_TAIL = "\n) AS pairs GROUP BY kind"

    def __init__(self, sql, params):
        self._sql = sql
        self._params = params

    def _run(self, start=0, stop=None):
        limit = "" if stop is None else f" LIMIT {stop - start}"
        sql = self.HEAD + self._sql + self.TAIL + limit
        if start:
            sql += f" OFFSET {start}"
        with connection.cursor() as cursor:
            cursor.execute(sql, self._params)
            names = [column.name for column in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def __iter__(self):
        return iter(self._run())

    def __getitem__(self, key):
        if not isinstance(key, slice) or key.step is not None:
            raise TypeError("event counterparts support plain slices only")
        start, stop = key.start or 0, key.stop
        if start < 0 or (stop is not None and stop < 0):
            raise ValueError("negative slices are not supported")
        if stop is not None and stop <= start:
            return []
        return self._run(start, stop)

    def distinct_counterparts(self):
        """Distinct counterparts per kind among these rows, in one aggregate statement."""
        sql = self.COUNT_HEAD + self._sql + self.COUNT_TAIL
        with connection.cursor() as cursor:
            cursor.execute(sql, self._params)
            return dict(cursor.fetchall())


def distinct_counterparts(rows):
    """Distinct counterparts per event kind (every kind present) of ``event_counterparts`` rows."""
    if isinstance(rows, _PairRows):
        counted = rows.distinct_counterparts()
    else:
        counted = rows.order_by().aggregate(
            **{
                kind: Count("counterpart_id", distinct=True, filter=Q(kind=kind))
                for kind in Event.Kind.values
            }
        )
    return {kind: counted.get(kind, 0) for kind in Event.Kind.values}


def entity_events(entity, *, kind=None, counterpart=None, at=None):
    """Public events with ``entity`` as a party, optionally shared with ``counterpart``."""
    events = _events(kind, at).filter(
        Exists(EventParty.objects.filter(event_id=OuterRef("pk"), entity=entity))
    )
    if counterpart is not None:
        events = events.filter(
            Exists(EventParty.objects.filter(event_id=OuterRef("pk"), entity=counterpart))
        )
    return events


def event_summary(events):
    """Per-kind count, euro total and period of an event queryset."""
    return events.order_by().values("kind").annotate(count=Count("pk"), **_event_summary())


def _stored_totals(entity):
    return EventEntitySummary.objects.filter(entity=entity, entity__is_public=True)


def event_breakdown(entity, at):
    """Count, euro total and period per (dataset, kind) of the entity's public events up to ``at``.

    One statement that ``event_totals`` and ``event_datasets`` both derive from, so a
    dated profile scans the events once rather than once per summary.
    """
    return list(
        entity_events(entity, at=at)
        .order_by()
        .values("dataset", "kind")
        .annotate(count=Count("pk"), **_event_summary())
    )


def _kind_totals(rows) -> list[dict[str, Any]]:
    """Fold ``event_breakdown`` rows into per-kind totals, ordered by kind."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["kind"]].append(row)
    totals: list[dict[str, Any]] = []
    for kind in sorted(grouped):
        group = grouped[kind]
        amounts = [row["amount_total"] for row in group if row["amount_total"] is not None]
        firsts = [row["first_date"] for row in group if row["first_date"] is not None]
        lasts = [row["last_date"] for row in group if row["last_date"] is not None]
        totals.append(
            {
                "kind": kind,
                "count": sum(row["count"] for row in group),
                "amount_total": sum(amounts) if amounts else None,
                "first_date": min(firsts, default=None),
                "last_date": max(lasts, default=None),
            }
        )
    return totals


def event_totals(entity, *, at=None, breakdown=None):
    """Per-kind public event totals for a profile summary.

    Without a date they come from ``EventEntitySummary``; a date needs the events, either
    aggregated here or from an ``event_breakdown`` the caller already has.
    """
    if at is not None:
        return _kind_totals(event_breakdown(entity, at) if breakdown is None else breakdown)
    return (
        _stored_totals(entity)
        .order_by()
        .values("kind")
        .annotate(
            count=Sum("event_count"),
            amount_total=Sum("amount_eur_sum"),
            first_date=Min("period_start"),
            last_date=Max("period_end"),
        )
        .order_by("kind")
    )


def event_scope_totals(entity, *, counterpart=None, at=None):
    """Per-kind totals of the events ``entity`` shares with ``counterpart`` (or all)."""
    if counterpart is None:
        return event_totals(entity, at=at)
    return event_summary(entity_events(entity, counterpart=counterpart, at=at)).order_by("kind")


def event_datasets(entity, *, at=None, breakdown=None):
    """Public events of ``entity`` per catalogue dataset (``breakdown`` as in ``event_totals``)."""
    if at is not None:
        counts = Counter()
        for row in event_breakdown(entity, at) if breakdown is None else breakdown:
            counts[row["dataset"]] += row["count"]
        return [{"dataset": dataset, "count": count} for dataset, count in counts.items()]
    return _stored_totals(entity).order_by().values("dataset").annotate(count=Sum("event_count"))


def evidence_datasets(relationships):
    """Public relationships among ``relationships`` per dataset of their public evidence,
    with the distinct sources behind them (a source belongs to one dataset, so per-dataset
    source counts add up to the profile's total)."""
    return (
        public_evidence()
        .filter(relationship__in=relationships.prefetch_related(None).order_by().values("pk"))
        .order_by()
        .values(dataset=F("source__dataset"))
        .annotate(
            count=Count("relationship_id", distinct=True),
            sources=Count("source_id", distinct=True),
        )
    )


def public_identities(entity):
    """Identifiers of a public profile under schemes that may be shown publicly."""
    return SourceIdentity.objects.filter(
        entity=entity, entity__is_public=True, source__in=PUBLIC_IDENTIFIER_SCHEMES
    ).order_by("source", "external_id")
