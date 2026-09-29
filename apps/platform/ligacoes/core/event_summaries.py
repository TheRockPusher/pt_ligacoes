"""Derived per-entity and per-pair aggregates of public events, kept in step in SQL.

The public pages aggregate the events of one profile on every request; for an entity with
hundreds of thousands of events that means reading the same number of random event rows.
``EventEntitySummary`` and ``EventPairSummary`` store exactly what ``public_events()``
yields, grouped per dataset so a dataset can be rebuilt on its own. They are recomputed
inside the transaction that changes an event, a party, an entity's visibility or a
dataset source's visibility: for every entity touched, its rows are deleted and reinserted
by two ``INSERT ... SELECT`` statements. Nothing here decides visibility; the rule is
compiled from ``public.selectors.public_events`` so there is one definition.
"""

from collections.abc import Collection, Iterable, Iterator
from itertools import batched
from typing import Any
from uuid import UUID

from django.db import connection

from .models import EventEntitySummary, EventPairSummary, EventParty

ENTITY_CHUNK = 2000
# A full or per-dataset rebuild runs per range of the entity id space: one statement each.
RANGES = 64

ENTITY_SQL = """
INSERT INTO {summary} (entity_id, dataset, kind, event_count, amount_eur_sum,
                       period_start, period_end)
SELECT own.entity_id, e.dataset, e.kind, COUNT(*),
       SUM(e.amount) FILTER (WHERE e.currency = 'EUR'),
       MIN(COALESCE(e.date, e.start_date)),
       MAX(COALESCE(e.date, e.end_date, e.start_date))
FROM (
    SELECT DISTINCT o.entity_id, o.event_id FROM {party} o
    WHERE o.entity_id IS NOT NULL AND {scope}
) own
JOIN {event} e ON e.id = own.event_id
WHERE e.id IN ({events}){dataset}
GROUP BY own.entity_id, e.dataset, e.kind
"""

PAIR_SQL = """
INSERT INTO {summary} (entity_id, counterpart_id, dataset, kind, entity_role,
                       counterpart_role, event_count, amount_eur_sum, period_start, period_end)
SELECT p.entity_id, p.counterpart_id, e.dataset, e.kind, p.entity_role, p.counterpart_role,
       COUNT(*),
       SUM(e.amount) FILTER (WHERE e.currency = 'EUR'),
       MIN(COALESCE(e.date, e.start_date)),
       MAX(COALESCE(e.date, e.end_date, e.start_date))
FROM (
    SELECT DISTINCT o.entity_id AS entity_id, c.entity_id AS counterpart_id, o.event_id AS event_id,
                    o.role AS entity_role, c.role AS counterpart_role
    FROM {party} o
    JOIN {party} c ON c.event_id = o.event_id AND c.entity_id <> o.entity_id
    WHERE o.entity_id IS NOT NULL AND {scope}
) p
JOIN {event} e ON e.id = p.event_id
WHERE e.id IN ({events}){dataset}
GROUP BY p.entity_id, p.counterpart_id, e.dataset, e.kind, p.entity_role, p.counterpart_role
"""

DELETE_SQL = "DELETE FROM {summary} o WHERE {scope}{dataset}"


def _ranges() -> Iterator[tuple[str, str | None]]:
    step = 256 // RANGES
    for index in range(RANGES):
        low = f"{index * step:02x}000000-0000-0000-0000-000000000000"
        high = f"{(index + 1) * step:02x}000000-0000-0000-0000-000000000000"
        yield low, high if index + 1 < RANGES else None


def _scopes(entities: Collection[UUID] | None) -> Iterator[tuple[str, list[Any]]]:
    """``WHERE`` fragments on ``o.entity_id`` that together cover the requested entities."""
    column = "o.entity_id"
    if entities is None:
        for low, high in _ranges():
            if high is None:
                yield f"{column} >= %s::uuid", [low]
            else:
                yield f"{column} >= %s::uuid AND {column} < %s::uuid", [low, high]
        return
    for chunk in batched(sorted(entities, key=str), ENTITY_CHUNK, strict=False):
        yield f"{column} = ANY(%s)", [list(chunk)]


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
    events_sql, events_params = public_events().order_by().values("pk").query.sql_with_params()
    quote = connection.ops.quote_name
    names = {
        "party": quote(EventParty._meta.db_table),
        "event": quote("core_event"),
        "events": events_sql,
    }
    dataset_sql = " AND e.dataset = %s" if dataset is not None else ""
    delete_dataset = " AND o.dataset = %s" if dataset is not None else ""
    extra: list[Any] = [dataset] if dataset is not None else []
    with connection.cursor() as cursor:
        for scope, scope_params in _scopes(entities):
            for model, sql in ((EventEntitySummary, ENTITY_SQL), (EventPairSummary, PAIR_SQL)):
                table = quote(model._meta.db_table)
                cursor.execute(
                    DELETE_SQL.format(summary=table, scope=scope, dataset=delete_dataset),
                    [*scope_params, *extra],
                )
                cursor.execute(
                    sql.format(summary=table, scope=scope, dataset=dataset_sql, **names),
                    [*scope_params, *events_params, *extra],
                )


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
