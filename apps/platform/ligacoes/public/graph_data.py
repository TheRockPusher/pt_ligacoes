"""Ego-graph payload of a profile map: published relationships and aggregated event ties."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.urls import reverse

from ligacoes.core.models import Entity, Event, EventParty

from .profile import counterpart_filter, ordered_for
from .selectors import (
    PUBLIC_RELATIONSHIP_LIMIT,
    event_counterparts,
    public_connection_counts,
    public_relationships,
)

# Largest counterparts by amount, then count; the rest become one "+N" node per event kind.
EVENT_EDGE_LIMIT = 15

# Singular and plural nouns for edge labels ("12 contratos", then the euro total).
EVENT_NOUNS: dict[str, tuple[str, str]] = {
    Event.Kind.CONTRACT: ("contrato", "contratos"),
    Event.Kind.SUBSIDY: ("apoio", "apoios"),
    Event.Kind.EU_FUNDING: ("financiamento europeu", "financiamentos europeus"),
    Event.Kind.MEETING: ("reunião", "reuniões"),
    Event.Kind.HEARING: ("audição", "audições"),
    Event.Kind.GIFT: ("oferta", "ofertas"),
    Event.Kind.HOSPITALITY: ("hospitalidade", "hospitalidades"),
    Event.Kind.TRAVEL: ("deslocação", "deslocações"),
}

# Thousands (mil), millions (M) and thousand millions (mil M), as pt-PT writes them.
AMOUNT_SCALES = ((Decimal(10**3), "mil €"), (Decimal(10**6), "M€"), (Decimal(10**9), "mil M€"))
NBSP = "\u00a0"
CENT = Decimal("0.01")
SEPARATOR = " \u00b7 "  # middle dot between count and total


def format_number(value: Decimal | int) -> str:
    """pt-PT digits: decimal comma, thousands grouped by a space only from five digits."""
    integer, _, fraction = (f"{value:f}" if isinstance(value, Decimal) else str(value)).partition(
        "."
    )
    fraction = fraction.rstrip("0")
    if len(integer) > 4:
        groups = []
        while integer:
            groups.insert(0, integer[-3:])
            integer = integer[:-3]
        integer = NBSP.join(groups)
    return f"{integer},{fraction}" if fraction else integer


def format_amount(amount: Decimal) -> str:
    """Compact euro total, e.g. ``950 €``, ``12,5 mil €``, ``1,4 M€``, ``2,1 mil M€``."""
    scaled, unit = amount.quantize(Decimal(1), ROUND_HALF_UP), "€"
    for scale, name in AMOUNT_SCALES:
        # Promote on the rounded value: 999 960 € is "1 M€", not "1000 mil €".
        if scaled < 1000:
            break
        scaled, unit = (amount / scale).quantize(Decimal("0.1"), ROUND_HALF_UP), name
    return f"{format_number(scaled)} {unit}"


def events_label(kind: str, count: int, amount: Decimal | None) -> str:
    one, many = EVENT_NOUNS.get(kind, ("registo", "registos"))
    label = f"{format_number(count)} {one if count == 1 else many}"
    return f"{label}{SEPARATOR}{format_amount(amount)}" if amount is not None else label


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _events_url(entity: Entity, at: date | None, **filters: str) -> str:
    params = {**filters, **({"at": at.isoformat()} if at else {})}
    return f"{reverse('public:entity_events', kwargs={'slug': entity.slug})}?{urlencode(params)}"


def _relationship_edges(entity: Entity, at: date | None, query: str):
    relationships = public_relationships(at).filter(Q(subject=entity) | Q(object=entity))
    if query:
        relationships = relationships.filter(counterpart_filter(entity, query))
    # Same order as the profile list, so the map draws the list's first page.
    listed = list(
        ordered_for(entity, relationships.select_related("term"))[: PUBLIC_RELATIONSHIP_LIMIT + 1]
    )
    endpoints: dict[Any, Entity] = {}
    edges = []
    for relationship in listed[:PUBLIC_RELATIONSHIP_LIMIT]:
        # Only approved evidence was prefetched by the shared visibility selector.
        if not relationship.public_evidence:
            continue
        evidence = relationship.public_evidence[0]
        for endpoint in (relationship.subject, relationship.object):
            endpoints[endpoint.pk] = endpoint
        term = relationship.term
        edges.append(
            {
                "data": {
                    "id": str(relationship.pk),
                    "source": str(relationship.subject_id),
                    "target": str(relationship.object_id),
                    "label": relationship.get_kind_display(),
                    "kind": relationship.kind,
                    "role": relationship.role,
                    "term": term.label if term else None,
                    "start": _iso(relationship.start_date),
                    "end": _iso(relationship.end_date),
                    "start_precision": relationship.start_precision,
                    "end_precision": relationship.end_precision,
                    "temporal_status": relationship.temporal_status,
                    "url": reverse("public:evidence_detail", kwargs={"pk": evidence.pk}),
                }
            }
        )
    return endpoints, edges, len(listed) > PUBLIC_RELATIONSHIP_LIMIT


def _event_edges(entity: Entity, at: date | None, query: str):
    rows = event_counterparts(entity, at=at, name=query)
    top = list(rows[: EVENT_EDGE_LIMIT + 1])
    shown = top[:EVENT_EDGE_LIMIT]
    counterparts = Entity.objects.in_bulk({row["counterpart_id"] for row in shown})
    endpoints: dict[Any, Entity] = {}
    edges = []
    for row in shown:
        counterpart = counterparts.get(row["counterpart_id"])
        # Hidden since the aggregate ran: drop the edge rather than reveal the entity.
        if counterpart is None or not counterpart.is_public:
            continue
        endpoints[counterpart.pk] = counterpart
        kind = row["kind"]
        edges.append(
            {
                "data": {
                    "id": ":".join(
                        (
                            "events",
                            kind,
                            str(counterpart.pk),
                            row["role_of_entity"],
                            row["role_of_counterpart"],
                        )
                    ),
                    "source": str(entity.pk),
                    "target": str(counterpart.pk),
                    "label": events_label(kind, row["count"], row["amount_total"]),
                    "kind": "events",
                    "event_kind": kind,
                    "event_kind_label": Event.Kind(kind).label,
                    "count": row["count"],
                    "amount": f"{row['amount_total'].quantize(CENT):f}"
                    if row["amount_total"] is not None
                    else None,
                    "first": _iso(row["first_date"]),
                    "last": _iso(row["last_date"]),
                    "entity_role": EventParty.Role(row["role_of_entity"]).label,
                    "counterpart_role": EventParty.Role(row["role_of_counterpart"]).label,
                    "url": _events_url(entity, at, tipo=kind, com=counterpart.slug),
                }
            }
        )
    more_nodes, more_edges = [], []
    if len(top) > EVENT_EDGE_LIMIT:
        # Distinct counterparts per kind, aggregated over the same public rows in SQL.
        totals = rows.order_by().aggregate(
            **{
                kind: Count("counterpart_id", distinct=True, filter=Q(kind=kind))
                for kind in Event.Kind.values
            }
        )
        drawn: dict[str, set[Any]] = {}
        for row in shown:
            drawn.setdefault(row["kind"], set()).add(row["counterpart_id"])
        for kind in Event.Kind.values:
            hidden = totals[kind] - len(drawn.get(kind, ()))
            if hidden <= 0:
                continue
            node_id = f"events-more:{entity.pk}:{kind}"
            label = f"+{format_number(hidden)} {'entidade' if hidden == 1 else 'entidades'}"
            url = _events_url(entity, at, tipo=kind)
            data = {"event_kind": kind, "event_kind_label": Event.Kind(kind).label, "url": url}
            more_nodes.append(
                {"data": {"id": node_id, "label": label, "kind": "more", "count": hidden, **data}}
            )
            more_edges.append(
                {
                    "data": {
                        "id": f"{node_id}:edge",
                        "source": str(entity.pk),
                        "target": node_id,
                        "label": label,
                        "kind": "more",
                        "count": hidden,
                        **data,
                    }
                }
            )
    return endpoints, edges + more_edges, more_nodes


def entity_node(entity: Entity, connections: int) -> dict:
    return {
        "data": {
            "id": str(entity.pk),
            "label": entity.name,
            "kind": entity.kind,
            "classification": entity.classification,
            "classification_label": Entity.Classification(entity.classification).label
            if entity.classification
            else "",
            "url": reverse("public:entity_detail", kwargs={"slug": entity.slug}),
            "connections": connections,
        }
    }


def graph_payload(entity: Entity, *, at: date | None, query: str) -> dict:
    """Cytoscape elements around a public ``entity``, built only from public selectors.

    At most ``PUBLIC_RELATIONSHIP_LIMIT`` relationship edges (``truncated`` reports more),
    ``EVENT_EDGE_LIMIT`` event edges and one "+N entidades" node per event kind with more
    counterparts. ``query`` narrows both to counterparts whose name contains it.
    """
    related, relationship_edges, truncated = _relationship_edges(entity, at, query)
    counterparts, event_edges, more_nodes = _event_edges(entity, at, query)
    nodes = {entity.pk: entity, **related, **counterparts}
    counts = public_connection_counts(nodes)
    return {
        "nodes": [
            *(entity_node(node, sum(counts[pk].values())) for pk, node in nodes.items()),
            *more_nodes,
        ],
        "edges": relationship_edges + event_edges,
        "truncated": truncated,
    }
