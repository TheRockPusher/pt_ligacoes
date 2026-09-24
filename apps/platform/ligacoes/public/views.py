import re
from datetime import date
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from ligacoes.core.models import Entity

from .selectors import PUBLIC_RELATIONSHIP_LIMIT, public_evidence, public_relationships


def selected_date(request):
    value = request.GET.get("at", "")
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, flags=re.ASCII):
        raise ValueError("Use uma data válida no formato AAAA-MM-DD.")
    return date.fromisoformat(value)


def date_bad_request(request):
    return render(request, "400.html", status=400)


@require_GET
def index(request):
    query = request.GET.get("q", "")
    if len(query) > 100:
        return render(request, "400.html", status=400)
    query = query.strip()
    entities = Entity.objects.filter(is_public=True).order_by("name", "pk")
    if query:
        entities = entities.filter(name__icontains=query)
    page_obj = Paginator(entities, 24).get_page(request.GET.get("page"))
    template = (
        "public/_profiles.html"
        if request.headers.get("HX-Request") == "true"
        else "public/index.html"
    )
    response = render(request, template, {"page_obj": page_obj, "query": query})
    response.headers["Vary"] = "HX-Request"
    return response


@require_GET
def entity_detail(request, slug):
    entity = get_object_or_404(Entity, slug=slug, is_public=True)
    try:
        at = selected_date(request)
    except ValueError:
        return date_bad_request(request)
    relationships = list(
        public_relationships(at).filter(Q(subject=entity) | Q(object=entity))[
            : PUBLIC_RELATIONSHIP_LIMIT + 1
        ]
    )
    graph_url = reverse("public:graph", kwargs={"slug": entity.slug})
    if at:
        graph_url += "?" + urlencode({"at": at.isoformat()})
    return render(
        request,
        "public/entity_detail.html",
        {
            "entity": entity,
            "relationships": [
                relationship
                for relationship in relationships[:PUBLIC_RELATIONSHIP_LIMIT]
                if relationship.public_evidence
            ],
            "relationships_truncated": len(relationships) > PUBLIC_RELATIONSHIP_LIMIT,
            "selected_date": at.isoformat() if at else "",
            "date_error": "",
            "graph_url": graph_url,
        },
    )


@require_GET
def evidence_detail(request, pk):
    evidence = get_object_or_404(public_evidence(), pk=pk)
    return render(request, "public/evidence_detail.html", {"evidence": evidence})


@require_GET
def graph(request, slug):
    entity = get_object_or_404(Entity, slug=slug, is_public=True)
    try:
        at = selected_date(request)
    except ValueError:
        return date_bad_request(request)
    relationships = list(
        public_relationships(at).filter(Q(subject=entity) | Q(object=entity))[
            : PUBLIC_RELATIONSHIP_LIMIT + 1
        ]
    )
    nodes = {str(entity.pk): entity}
    edges = []
    for relationship in relationships[:PUBLIC_RELATIONSHIP_LIMIT]:
        # Only approved evidence was prefetched by the shared visibility selector.
        if not relationship.public_evidence:
            continue
        evidence = relationship.public_evidence[0]
        for endpoint in (relationship.subject, relationship.object):
            nodes[str(endpoint.pk)] = endpoint
        edges.append(
            {
                "data": {
                    "id": str(relationship.pk),
                    "source": str(relationship.subject_id),
                    "target": str(relationship.object_id),
                    "label": relationship.get_kind_display(),
                    "url": reverse("public:evidence_detail", kwargs={"pk": evidence.pk}),
                }
            }
        )
    return JsonResponse(
        {
            "nodes": [
                {
                    "data": {
                        "id": pk,
                        "label": endpoint.name,
                        "kind": endpoint.kind,
                        "url": reverse("public:entity_detail", kwargs={"slug": endpoint.slug}),
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
