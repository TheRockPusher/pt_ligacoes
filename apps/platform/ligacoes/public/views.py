import re
from datetime import date
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from ligacoes.core.models import Entity

from .profile import (
    build_axis,
    build_groups,
    build_span,
    build_summary,
    counterpart_filter,
    listings,
    ordered_for,
)
from .selectors import (
    PUBLIC_RELATIONSHIP_LIMIT,
    public_connection_counts,
    public_evidence,
    public_relationships,
)

DIRECTORY_PAGE_SIZE = 24
ENTRY_POINT_LIMIT = 6
QUERY_LIMIT = 100


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


@require_GET
def index(request):
    try:
        query = search_query(request)
    except ValueError:
        return render(request, "400.html", status=400)
    kind = request.GET.get("tipo", "")
    if kind and kind not in Entity.Kind.values:
        return render(request, "400.html", status=400)
    entities = Entity.objects.filter(is_public=True).order_by("name", "pk")
    if query:
        entities = entities.filter(name__icontains=query)
    kind_counts = dict(entities.order_by().values_list("kind").annotate(total=Count("pk")))
    kinds = [
        (value, label, kind_counts.get(value, 0))
        for value, label in Entity.Kind.choices
        if kind_counts.get(value)
    ]
    if kind:
        entities = entities.filter(kind=kind)
    page_obj = Paginator(entities, DIRECTORY_PAGE_SIZE).get_page(request.GET.get("page"))
    is_htmx = request.headers.get("HX-Request") == "true"
    context = {
        "page_obj": page_obj,
        "profiles": listings(page_obj),
        "query": query,
        "kind": kind,
        "kinds": kinds,
        "kinds_total": sum(kind_counts.values()),
        "filter_query": urlencode(
            {key: value for key, value in (("q", query), ("tipo", kind)) if value}
        ),
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
    return ordered_for(entity, relationships)


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
    return render(
        request,
        "public/entity_detail.html",
        {
            "entity": entity,
            "summary": summary,
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
            "entity_kinds": Entity.Kind.choices,
        },
    )


@require_GET
def evidence_detail(request, pk):
    evidence = get_object_or_404(public_evidence(), pk=pk)
    relationship = evidence.relationship
    bounds = [day for day in (relationship.start_date, relationship.end_date) if day]
    axis = build_axis([*bounds, timezone.localdate()]) if bounds else None
    return render(
        request,
        "public/evidence_detail.html",
        {
            "evidence": evidence,
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
    relationships = list(profile_relationships(entity, at, query)[: PUBLIC_RELATIONSHIP_LIMIT + 1])
    nodes = {entity.pk: entity}
    edges = []
    for relationship in relationships[:PUBLIC_RELATIONSHIP_LIMIT]:
        # Only approved evidence was prefetched by the shared visibility selector.
        if not relationship.public_evidence:
            continue
        evidence = relationship.public_evidence[0]
        for endpoint in (relationship.subject, relationship.object):
            nodes[endpoint.pk] = endpoint
        edges.append(
            {
                "data": {
                    "id": str(relationship.pk),
                    "source": str(relationship.subject_id),
                    "target": str(relationship.object_id),
                    "label": relationship.get_kind_display(),
                    "kind": relationship.kind,
                    "start": relationship.start_date.isoformat()
                    if relationship.start_date
                    else None,
                    "end": relationship.end_date.isoformat() if relationship.end_date else None,
                    "url": reverse("public:evidence_detail", kwargs={"pk": evidence.pk}),
                }
            }
        )
    counts = public_connection_counts(nodes)
    return JsonResponse(
        {
            "nodes": [
                {
                    "data": {
                        "id": str(pk),
                        "label": endpoint.name,
                        "kind": endpoint.kind,
                        "url": reverse("public:entity_detail", kwargs={"slug": endpoint.slug}),
                        "connections": sum(counts[pk].values()),
                    }
                }
                for pk, endpoint in nodes.items()
            ],
            "edges": edges,
            "truncated": len(relationships) > PUBLIC_RELATIONSHIP_LIMIT,
        }
    )


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
