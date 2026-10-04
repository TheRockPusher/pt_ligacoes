import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import wraps
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.db.models import Case, Count, F, Max, Min, Prefetch, Q, When
from django.db.models.functions import Coalesce
from django.http import HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from ligacoes.core.catalogue import DATASETS, Dataset
from ligacoes.core.models import Entity, Event, EventParty, Relationship

from .graph_data import graph_payload
from .profile import (
    EVENT_KIND_ORDER,
    PROVENANCE,
    build_axis,
    build_groups,
    build_summary,
    dataset_uses,
    event_sections,
    events_url,
    identifiers,
    listings,
    money,
    profile_relationships,
    relationship_span,
)
from .selectors import (
    CURRENT_OFFICE_LIMIT,
    DECLARATION_DATASETS,
    PUBLIC_RELATIONSHIP_LIMIT,
    current_offices,
    declared_kind_totals,
    event_breakdown,
    event_counterparts,
    event_datasets,
    event_scope_totals,
    evidence_datasets,
    identity_provenance,
    merged_target,
    public_aliases,
    public_connection_counts,
    public_evidence,
    public_identities,
    public_relationships,
    search_entities,
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


def follow_merges(*params):
    """301 to the same view when the path ``slug`` or a query ``params`` slug was merged into
    another public entity; every other slug reaches the view unchanged."""

    def decorate(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            moved = False
            target_kwargs = dict(kwargs)
            query = request.GET.copy()
            if target := merged_target(kwargs.get("slug", "")):
                target_kwargs["slug"] = target.slug
                moved = True
            for field in params:
                if target := merged_target(query.get(field, "")):
                    query[field] = target.slug
                    moved = True
            if not moved:
                return view(request, *args, **kwargs)
            url = reverse(request.resolver_match.view_name, kwargs=target_kwargs)
            return HttpResponsePermanentRedirect(f"{url}?{query.urlencode()}" if query else url)

        return wrapper

    return decorate


def search_query(request):
    query = request.GET.get("q", "")
    if len(query) > QUERY_LIMIT:
        raise ValueError("Pesquisa demasiado longa.")
    return query.strip()


def selected_kind(request):
    kind = request.GET.get("tipo", "")
    if kind and kind not in Relationship.Kind.values:
        raise ValueError("Tipo de ligação desconhecido.")
    return kind


def declared_only(request):
    value = request.GET.get("declarado", "")
    if value not in ("", "1"):
        raise ValueError("Filtro de declarações inválido.")
    return value == "1"


def profile_query(*, at=None, query="", kind="", declared=False):
    """Query string of a profile view: observed date, search, one kind or declarations."""
    return urlencode(
        {
            key: value
            for key, value in (
                ("at", at.isoformat() if at else ""),
                ("q", query),
                ("tipo", kind),
                ("declarado", "1" if declared else ""),
            )
            if value
        }
    )


def profile_url(entity, **filters):
    params = profile_query(**filters)
    url = reverse("public:entity_detail", kwargs={"slug": entity.slug})
    return f"{url}{'?' + params if params else ''}#ligacoes"


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
    entities = (
        search_entities(query).order_by("search_rank", "name", "pk")
        if query
        else Entity.objects.filter(is_public=True).order_by("name", "pk")
    )
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
        "profiles": listings(page_obj, query),
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


@require_GET
@follow_merges()
def entity_detail(request, slug):
    entity = get_object_or_404(Entity, slug=slug, is_public=True)
    try:
        at = selected_date(request)
        query = search_query(request)
        kind = selected_kind(request)
        declared = declared_only(request)
    except ValueError:
        return date_bad_request(request)
    scope = public_relationships(at).filter(Q(subject=entity) | Q(object=entity))
    # Aggregated in SQL: a hub (Parliament, Government) has tens of thousands of connections.
    plain = scope.prefetch_related(None).order_by()
    totals = dict(plain.values_list("kind").annotate(total=Count("pk")))
    declared_totals = declared_kind_totals(plain, totals)
    bounds = plain.aggregate(
        counterparts=Count(
            Case(When(subject=entity, then=F("object_id")), default=F("subject_id")),
            distinct=True,
        ),
        first_start=Min("start_date"),
        first_end=Min("end_date"),
        last_start=Max("start_date"),
        last_end=Max("end_date"),
    )
    days = [day for day in bounds.values() if isinstance(day, date)]
    evidence_rows = list(evidence_datasets(scope))
    sources = sum(row["sources"] for row in evidence_rows)
    summary = build_summary(totals, declared_totals, bounds["counterparts"], sources, days)
    listed = profile_relationships(entity, at, query, kind, declared, split=bool(declared_totals))
    page_obj = Paginator(listed, PUBLIC_RELATIONSHIP_LIMIT).get_page(request.GET.get("page"))
    # Only approved evidence was prefetched by the shared visibility selector.
    relationships = [relationship for relationship in page_obj if relationship.public_evidence]
    today = timezone.localdate()
    axis = build_axis([*days, at, today]) if days else None
    counterparts = {
        relationship.object_id if relationship.subject_id == entity.pk else relationship.subject_id
        for relationship in relationships
    }
    page_query = profile_query(at=at, query=query, kind=kind, declared=declared)
    graph_url = reverse("public:graph", kwargs={"slug": entity.slug})
    if page_query:
        graph_url += "?" + page_query
    breakdown = event_breakdown(entity, at) if at is not None else None
    groups = build_groups(
        entity,
        relationships,
        totals,
        declared_totals,
        axis,
        public_connection_counts(counterparts),
        today,
    )
    offices = (
        list(current_offices(entity)[: CURRENT_OFFICE_LIMIT + 1])
        if entity.kind == Entity.Kind.PERSON
        else []
    )
    current_total = (
        current_offices(entity).prefetch_related(None).order_by().count()
        if len(offices) > CURRENT_OFFICE_LIMIT
        else len(offices)
    )
    return render(
        request,
        "public/entity_detail.html",
        {
            "entity": entity,
            "summary": summary,
            "provenance": PROVENANCE.get(identity_provenance([entity.pk]).get(entity.pk, ""), ""),
            "aliases": public_aliases(entity),
            "identifiers": identifiers(public_identities(entity)),
            "current_offices": offices[:CURRENT_OFFICE_LIMIT],
            "current_more": current_total - min(len(offices), CURRENT_OFFICE_LIMIT),
            "current_url": profile_url(entity, kind=Relationship.Kind.PUBLIC_OFFICE),
            "kind_links": [
                (value, label, count, profile_url(entity, at=at, kind=value))
                for value, label, count, _share in summary.breakdown
            ],
            "declared_url": profile_url(entity, at=at, declared=True),
            "groups": groups,
            "has_ongoing": any(
                relationship.span and relationship.span.ongoing for relationship in relationships
            ),
            "relationships": relationships,
            "page_obj": page_obj,
            "axis": axis,
            "at_position": axis.position(at) if axis and at else None,
            "query": query,
            "kind": kind,
            "kind_label": Relationship.Kind(kind).label if kind else "",
            "declared": declared,
            "clear_url": profile_url(entity),
            "all_dates_url": profile_url(entity, query=query, kind=kind, declared=declared),
            "selected_date": at.isoformat() if at else "",
            "page_query": page_query,
            "graph_url": graph_url,
            "path_url": reverse("public:path_finder") + "?" + urlencode({"de": entity.slug}),
            "event_sections": event_sections(entity, at, breakdown),
            "events_url": events_url(entity, at=at),
            "datasets": dataset_uses(
                evidence_rows, event_datasets(entity, at=at, breakdown=breakdown)
            ),
            "entity_kinds": Entity.Kind.choices,
        },
    )


@require_GET
@follow_merges("com")
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
    today = timezone.localdate()
    axis = build_axis([*bounds, today]) if bounds else None
    return render(
        request,
        "public/evidence_detail.html",
        {
            "evidence": evidence,
            "dataset": DATASETS.get(evidence.source.dataset),
            "declared": relationship.kind == Relationship.Kind.DECLARED_CLIENT
            or evidence.source.dataset in DECLARATION_DATASETS,
            "axis": axis,
            "span": relationship_span(axis, relationship, today),
        },
    )


@require_GET
@follow_merges()
def graph(request, slug):
    entity = get_object_or_404(Entity, slug=slug, is_public=True)
    try:
        at = selected_date(request)
        query = search_query(request)
        kind = selected_kind(request)
        declared = declared_only(request)
    except ValueError:
        return date_bad_request(request)
    return JsonResponse(graph_payload(entity, at=at, query=query, kind=kind, declared=declared))


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
