"""Derived per-entity and per-pair aggregates of public events, kept in step in SQL.

The public pages aggregate the events of one profile on every request; for an entity with
hundreds of thousands of events that means reading the same number of random event rows.
``EventEntitySummary`` and ``EventPairSummary`` store exactly what ``public_events()``
yields, grouped per dataset so a dataset can be rebuilt on its own. They are recomputed
inside the transaction that changes an event, a party, an entity's visibility or a
dataset source's visibility. Each rebuild materialises eligible events and distinct
parties once, then aggregates all affected entities and pairs. Nothing here decides
visibility; the rule is compiled from ``public.selectors.public_events``.
"""

from collections.abc import Collection, Iterable, Iterator
from itertools import batched
from typing import Any
from uuid import UUID

from django.db import connection, transaction

from .models import EventEntitySummary, EventPairSummary, EventParty

# A pair's amount is summed only where the pair is the money relation itself: contracting
# authority and supplier, or grantor and beneficiary, in either direction. Every other pair
# of roles (a bidder, co-beneficiaries, an intermediary, attendees ...) shares the record
# without having received or paid its amount, so its amount stays NULL. Entity totals keep
# the amounts of an entity's own records.
MONEY_ROLE_PAIRS = frozenset(
    {
        ("buyer", "supplier"),
        ("supplier", "buyer"),
        ("grantor", "beneficiary"),
        ("beneficiary", "grantor"),
    }
)


def money_pair_sql(entity_role: str, counterpart_role: str) -> str:
    """SQL condition: the two role columns are a money relation (constants only)."""
    pairs = ", ".join(f"('{one}', '{other}')" for one, other in sorted(MONEY_ROLE_PAIRS))
    return f"({entity_role}, {counterpart_role}) IN ({pairs})"


ENTITY_SQL = """
INSERT INTO {summary} (entity_id, dataset, kind, event_count, amount_eur_sum,
                       period_start, period_end)
SELECT own.entity_id, e.dataset, e.kind, COUNT(*),
       SUM(e.amount) FILTER (WHERE e.currency = 'EUR'),
       MIN(COALESCE(e.date, e.start_date)),
       MAX(COALESCE(e.date, e.end_date, e.start_date))
FROM (
    SELECT DISTINCT o.entity_id, o.event_id FROM pg_temp.event_summary_parties o
    WHERE {scope}
) own
JOIN pg_temp.event_summary_events e ON e.id = own.event_id
GROUP BY own.entity_id, e.dataset, e.kind
"""

PAIR_SQL = """
INSERT INTO {summary} (entity_id, counterpart_id, dataset, kind, entity_role,
                       counterpart_role, event_count, amount_eur_sum, period_start, period_end)
SELECT p.entity_id, p.counterpart_id, e.dataset, e.kind, p.entity_role, p.counterpart_role,
       COUNT(*),
       SUM(e.amount) FILTER (WHERE e.currency = 'EUR' AND {money}),
       MIN(COALESCE(e.date, e.start_date)),
       MAX(COALESCE(e.date, e.end_date, e.start_date))
FROM (
    SELECT o.entity_id AS entity_id, c.entity_id AS counterpart_id, o.event_id AS event_id,
                    o.role AS entity_role, c.role AS counterpart_role
    FROM pg_temp.event_summary_parties o
    JOIN pg_temp.event_summary_parties c
      ON c.event_id = o.event_id AND c.entity_id <> o.entity_id
    WHERE {scope}
) p
JOIN pg_temp.event_summary_events e ON e.id = p.event_id
GROUP BY p.entity_id, p.counterpart_id, e.dataset, e.kind, p.entity_role, p.counterpart_role
"""

DELETE_SQL = "DELETE FROM {summary} o WHERE {scope}{dataset}"


def _scopes(entities: Collection[UUID] | None) -> Iterator[tuple[str, list[Any]]]:
    """Bound aggregate statements without re-evaluating event eligibility per chunk."""
    if entities is not None:
        for chunk in batched(sorted(entities, key=str), 2000, strict=False):
            yield "o.entity_id = ANY(%s)", [list(chunk)]
        return
    for index in range(64):
        low = f"{index * 4:02x}000000-0000-0000-0000-000000000000"
        if index == 63:
            yield "o.entity_id >= %s::uuid", [low]
        else:
            high = f"{(index + 1) * 4:02x}000000-0000-0000-0000-000000000000"
            yield "o.entity_id >= %s::uuid AND o.entity_id < %s::uuid", [low, high]


def rebuild_event_summaries(
    *, entities: Collection[UUID] | None = None, dataset: str | None = None
) -> None:
    """Recompute the summary rows of ``entities`` (default all) in ``dataset`` (default all).

    ``entities`` must include every party of any event that changed, before and after,
    because a change moves the counterpart rows of all of them. Runs in the caller's
    transaction.
    """
    # Imported here: the public package depends on ``core``, and the visibility rule
    # must stay defined once, in the selectors.
    from ligacoes.public.selectors import public_events

    if entities is not None and not entities:
        return
    events = public_events()
    if dataset is not None:
        events = events.filter(dataset=dataset)
    events_sql, events_params = (
        events.order_by()
        .values("id", "dataset", "kind", "amount", "currency", "date", "start_date", "end_date")
        .query.sql_with_params()
    )
    quote = connection.ops.quote_name
    party = quote(EventParty._meta.db_table)
    scope = "TRUE" if entities is None else "o.entity_id = ANY(%s)"
    scope_params = [] if entities is None else [list(entities)]
    delete_dataset = " AND o.dataset = %s" if dataset is not None else ""
    dataset_params = [] if dataset is None else [dataset]
    # Scope eligibility as well as deletion: counterpart roles are retained for every
    # selected event, but unrelated events never enter either aggregate.
    # Quoted model table and constant scope only; entity IDs remain bound parameters.
    event_scope = (
        ""
        if entities is None
        else (
            f" WHERE EXISTS (SELECT 1 FROM {party} o WHERE o.event_id = e.id AND {scope})"  # noqa: S608
        )
    )
    with transaction.atomic(), connection.cursor() as cursor:
        # ORM-compiled SQL and constant scopes only; all values remain bound parameters.
        cursor.execute(
            "CREATE TEMP TABLE event_summary_events ON COMMIT DROP AS "  # noqa: S608
            f"SELECT e.* FROM ({events_sql}) e{event_scope}",
            [*events_params, *scope_params],
        )
        # Only Django's quoted model table name is interpolated.
        cursor.execute(
            "CREATE TEMP TABLE event_summary_parties ON COMMIT DROP AS "  # noqa: S608
            f"SELECT DISTINCT o.entity_id, o.event_id, o.role FROM {party} o "
            "JOIN pg_temp.event_summary_events e ON e.id = o.event_id "
            "WHERE o.entity_id IS NOT NULL"
        )
        cursor.execute("CREATE INDEX ON pg_temp.event_summary_events (id)")
        cursor.execute("CREATE INDEX ON pg_temp.event_summary_parties (entity_id, event_id, role)")
        cursor.execute("CREATE INDEX ON pg_temp.event_summary_parties (event_id, entity_id, role)")
        # Fresh bulk imports are invisible to autovacuum until commit. Statistics on
        # these materialised inputs prevent tiny estimates and repeated nested probes.
        cursor.execute("ANALYZE pg_temp.event_summary_events")
        cursor.execute("ANALYZE pg_temp.event_summary_parties")
        for scope, scope_params in _scopes(entities):
            for model, sql in ((EventEntitySummary, ENTITY_SQL), (EventPairSummary, PAIR_SQL)):
                table = quote(model._meta.db_table)
                cursor.execute(
                    DELETE_SQL.format(summary=table, scope=scope, dataset=delete_dataset),
                    [*scope_params, *dataset_params],
                )
                cursor.execute(
                    sql.format(
                        summary=table,
                        scope=scope,
                        money=money_pair_sql("p.entity_role", "p.counterpart_role"),
                    ),
                    scope_params,
                )
        # Multiple datasets or entity visibility changes may rebuild in one transaction.
        cursor.execute("DROP TABLE pg_temp.event_summary_parties, pg_temp.event_summary_events")


def co_party_entities(entity_ids: Iterable[UUID]) -> set[UUID]:
    """The entities and everyone who shares an event with them, in any status."""
    ids = set(entity_ids)
    return ids | set(
        EventParty.objects.filter(event__parties__entity__in=ids, entity__isnull=False)
        .order_by()
        .values_list("entity_id", flat=True)
        .distinct()
    )


def event_entities(event_ids: Iterable[object]) -> set[UUID]:
    """Resolved entities among the parties of the given events."""
    ids = list(event_ids)
    if not ids:
        return set()
    return set(
        EventParty.objects.filter(event__in=ids, entity__isnull=False)
        .order_by()
        .values_list("entity_id", flat=True)
        .distinct()
    )
