from collections import Counter, defaultdict

from django.db.models import Count, Exists, OuterRef, Prefetch, Q

from ligacoes.core.models import Evidence, Relationship

PUBLIC_EVIDENCE_LIMIT = 10
PUBLIC_RELATIONSHIP_LIMIT = 100


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
