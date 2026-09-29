import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.db.models import Count, Prefetch, Q
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from ligacoes.core.catalogue import DATASETS, Dataset
from ligacoes.core.models import Entity, Event, EventParty

from .graph_data import graph_payload
from .profile import (
    EVENT_KIND_ORDER,
    build_axis,
    build_groups,
    build_span,
    build_summary,
    counterpart_filter,
    dataset_uses,
    event_sections,
    events_url,
    identifiers,
    listings,
    money,
    ordered_for,
)
from .selectors import (
    PUBLIC_RELATIONSHIP_LIMIT,
    event_breakdown,
    event_counterparts,
    event_datasets,
    event_scope_totals,
    evidence_datasets,
    public_connection_counts,
    public_evidence,
    public_identities,
    public_relationships,
)
from .selectors import (
    entity_events as public_entity_events,
)

DIRECTORY_PAGE_SIZE = 24
EVENT_PAGE_SIZE = 50
ENTRY_POINT_LIMIT = 6
QUERY_LIMIT = 100


class CountedPaginator(Paginator):
    """A paginator whose total was already aggregated, sparing a second COUNT query."""

    def __init__(self, object_list, per_page, total):
        super().__init__(object_list, per_page)
        self.total = total

    @cached_property
    def count(self):
        return self.total


@dataclass(frozen=True)
class EventRow:
    event: Event
    amount: str
    dataset: Dataset | None


def selected_date(request):
    value = request.GET.get("at", "")
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, flags=re.ASCII):
        raise ValueError("Use uma data válida no formato AAAA-MM-DD.")
    return date.fromisoformat(value)


def date_bad_request(request):
    return render(request, "400.html", status=400)


def search_query(request):
    query = request.GET.get("q", "")
    if len(query) > QUERY_LIMIT:
        raise ValueError("Pesquisa demasiado longa.")
    return query.strip()


def entry_points():
    """Institutions with the most published connections: where most readers start."""
    rows = (
        public_relationships()
        .prefetch_related(None)
        .exclude(object__kind=Entity.Kind.PERSON)
        .order_by()
        .values_list("object_id")
        .annotate(total=Count("pk"))
        .order_by("-total", "object_id")[:ENTRY_POINT_LIMIT]
    )
    ids = [entity_id for entity_id, _total in rows]
    entities = Entity.objects.in_bulk(ids)
    return listings([entities[entity_id] for entity_id in ids])


def directory_query(query, kind="", classification=""):
    return urlencode(
        {
            key: value
            for key, value in (("q", query), ("tipo", kind), ("classificacao", classification))
            if value
        }
    )


@require_GET
def index(request):
    try:
        query = search_query(request)
    except ValueError:
        return render(request, "400.html", status=400)
    kind = request.GET.get("tipo", "")
    classification = request.GET.get("classificacao", "")
    if (kind and kind not in Entity.Kind.values) or (
        classification and classification not in Entity.Classification.values
    ):
        return render(request, "400.html", status=400)
    entities = Entity.objects.filter(is_public=True).order_by("name", "pk")
    if query:
        entities = entities.filter(name__icontains=query)
    # One grouped scan yields every facet count and the selection's total.
    facets = list(
        entities.order_by().values_list("kind", "classification").annotate(total=Count("pk"))
    )
    kind_counts = Counter()
    classification_counts = Counter()
    selected = 0
    for value, group, total in facets:
        kind_counts[value] += total
        if not kind or value == kind:
            classification_counts[group] += total
            if not classification or group == classification:
                selected += total
    kinds = [
        (value, label, kind_counts[value], directory_query(query, value))
        for value, label in Entity.Kind.choices
        if kind_counts[value]
    ]
    classifications = [
        (value, label, classification_counts[value], directory_query(query, kind, value))
        for value, label in Entity.Classification.choices
        if classification_counts[value]
    ]
    if kind:
        entities = entities.filter(kind=kind)
    if classification:
        entities = entities.filter(classification=classification)
    page_obj = CountedPaginator(entities, DIRECTORY_PAGE_SIZE, selected).get_page(
        request.GET.get("page")
    )
    is_htmx = request.headers.get("HX-Request") == "true"
    context = {
        "page_obj": page_obj,
        "profiles": listings(page_obj),
        "query": query,
        "kind": kind,
        "kinds": kinds,
        "kinds_total": sum(kind_counts.values()),
        "all_kinds_query": directory_query(query),
        "classification": classification,
        "classifications": classifications,
        "classifications_total": sum(total for _value, _label, total, _url in classifications),
        "all_classifications_query": directory_query(query, kind),
        "filter_query": directory_query(query, kind, classification),
    }
    if not is_htmx:
        context["entry_points"] = entry_points()
    response = render(request, "public/_profiles.html" if is_htmx else "public/index.html", context)
    response.headers["Vary"] = "HX-Request"
    return response


def profile_relationships(entity, at, query):
    """Published connections of one profile, in the reading order shared by list and graph."""
    relationships = public_relationships(at).filter(Q(subject=entity) | Q(object=entity))
    if query:
        relationships = relationships.filter(counterpart_filter(entity, query))
    return ordered_for(entity, relationships).select_related("term")


@require_GET
def entity_detail(request, slug):
    entity = get_object_or_404(Entity, slug=slug, is_public=True)
    try:
        at = selected_date(request)
        query = search_query(request)
    except ValueError:
        return date_bad_request(request)
    scope = public_relationships(at).filter(Q(subject=entity) | Q(object=entity))
    facts = list(
        scope.prefetch_related(None)
        .order_by()
        .values_list("subject_id", "object_id", "kind", "start_date", "end_date")
    )
    sources = (
        public_evidence()
        .filter(relationship__in=scope.prefetch_related(None).order_by())
        .order_by()
        .values("source_id")
        .distinct()
        .count()
    )
    summary, totals, days = build_summary(entity, facts, sources)
    listed = profile_relationships(entity, at, query)
    page_obj = Paginator(listed, PUBLIC_RELATIONSHIP_LIMIT).get_page(request.GET.get("page"))
    # Only approved evidence was prefetched by the shared visibility selector.
    relationships = [relationship for relationship in page_obj if relationship.public_evidence]
    axis = build_axis([*days, at, timezone.localdate()]) if days else None
    counterparts = {
        relationship.object_id if relationship.subject_id == entity.pk else relationship.subject_id
        for relationship in relationships
    }
    page_query = urlencode(
        {key: value for key, value in (("at", at.isoformat() if at else ""), ("q", query)) if value}
    )
    graph_url = reverse("public:graph", kwargs={"slug": entity.slug})
    if page_query:
        graph_url += "?" + page_query
    breakdown = event_breakdown(entity, at) if at is not None else None
    return render(
        request,
        "public/entity_detail.html",
        {
            "entity": entity,
            "summary": summary,
            "identifiers": identifiers(public_identities(entity)),
            "groups": build_groups(
                entity, relationships, totals, axis, public_connection_counts(counterparts)
            ),
            "relationships": relationships,
            "page_obj": page_obj,
            "axis": axis,
            "at_position": axis.position(at) if axis and at else None,
            "query": query,
            "selected_date": at.isoformat() if at else "",
            "page_query": page_query,
            "graph_url": graph_url,
            "path_url": reverse("public:path_finder") + "?" + urlencode({"de": entity.slug}),
            "event_sections": event_sections(entity, at, breakdown),
            "events_url": events_url(entity, at=at),
            "datasets": dataset_uses(
                evidence_datasets(scope), event_datasets(entity, at=at, breakdown=breakdown)
            ),
            "entity_kinds": Entity.Kind.choices,
        },
    )


@require_GET
def entity_events(request, slug):
    """Individual public events of one profile, by kind and counterpart, newest first."""
    entity = get_object_or_404(Entity, slug=slug, is_public=True)
    try:
        at = selected_date(request)
    except ValueError:
        return date_bad_request(request)
    kind = request.GET.get("tipo", "")
    if kind and kind not in Event.Kind.values:
        return date_bad_request(request)
    counterpart = None
    if other := request.GET.get("com", ""):
        counterpart = get_object_or_404(
            Entity.objects.filter(is_public=True).exclude(pk=entity.pk), slug=other
        )
    scope = public_entity_events(entity, counterpart=counterpart, at=at)
    totals = {
        row["kind"]: row for row in event_scope_totals(entity, counterpart=counterpart, at=at)
    }
    kind_labels = dict(Event.Kind.choices)
    kinds = [
        (
            value,
            kind_labels[value],
            totals[value]["count"],
            events_url(entity, kind=value, counterpart=counterpart, at=at),
        )
        for value in EVENT_KIND_ORDER
        if value in totals
    ]
    shown = [row for value, row in totals.items() if not kind or value == kind]
    firsts = [row["first_date"] for row in shown if row["first_date"]]
    lasts = [row["last_date"] for row in shown if row["last_date"]]
    if counterpart is None:
        amounts = [row["amount_total"] for row in shown if row["amount_total"] is not None]
    else:
        # Shared records only add up where the two stand in a money relation (buyer and
        # supplier, grantor and beneficiary); a bidder merely shares the record's price.
        amounts = [
            row["amount_total"]
            for row in event_counterparts(entity, counterpart=counterpart, at=at)
            if row["amount_total"] is not None and (not kind or row["kind"] == kind)
        ]
    events = (
        (scope.filter(kind=kind) if kind else scope)
        .order_by(Coalesce("date", "start_date").desc(nulls_last=True), "pk")
        .prefetch_related(
            Prefetch(
                "parties",
                # Recheck visibility in the second query, as the shared selectors do.
                queryset=EventParty.objects.filter(entity__is_public=True)
                .select_related("entity")
                .order_by("role", "name", "pk"),
            )
        )
    )
    total = sum(row["count"] for row in shown)
    page_obj = CountedPaginator(events, EVENT_PAGE_SIZE, total).get_page(request.GET.get("page"))
    records = [
        EventRow(event, money(event.amount, event.currency), DATASETS.get(event.dataset))
        for event in page_obj
    ]
    base_url = events_url(entity, kind=kind, counterpart=counterpart, at=at)
    return render(
        request,
        "public/entity_events.html",
        {
            "entity": entity,
            "counterpart": counterpart,
            "kind": kind,
            "kind_label": kind_labels.get(kind, ""),
            "kinds": kinds,
            "all_kinds_url": events_url(entity, counterpart=counterpart, at=at),
            "without_counterpart_url": events_url(entity, kind=kind, at=at),
            "all_dates_url": events_url(entity, kind=kind, counterpart=counterpart),
            "page_url": base_url + ("&" if "?" in base_url else "?"),
            "page_obj": page_obj,
            "events": records,
            "total_amount": money(sum(amounts, Decimal(0))) if amounts else "",
            "first": min(firsts, default=None),
            "last": max(lasts, default=None),
            "selected_date": at.isoformat() if at else "",
        },
    )


@require_GET
def evidence_detail(request, pk):
    evidence = get_object_or_404(public_evidence().select_related("relationship__term"), pk=pk)
    relationship = evidence.relationship
    bounds = [day for day in (relationship.start_date, relationship.end_date) if day]
    axis = build_axis([*bounds, timezone.localdate()]) if bounds else None
    return render(
        request,
        "public/evidence_detail.html",
        {
            "evidence": evidence,
            "dataset": DATASETS.get(evidence.source.dataset),
            "axis": axis,
            "span": build_span(axis, relationship.start_date, relationship.end_date),
        },
    )


@require_GET
def graph(request, slug):
    entity = get_object_or_404(Entity, slug=slug, is_public=True)
    try:
        at = selected_date(request)
        query = search_query(request)
    except ValueError:
        return date_bad_request(request)
    return JsonResponse(graph_payload(entity, at=at, query=query))


@require_GET
def methodology(request):
    return render(request, "public/methodology.html")


@never_cache
@require_GET
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
