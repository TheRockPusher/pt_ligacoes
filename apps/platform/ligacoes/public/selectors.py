from collections import Counter, defaultdict

from django.db.models import Count, Exists, F, Max, Min, OuterRef, Prefetch, Q, Sum
from django.db.models.functions import Coalesce

from ligacoes.core.catalogue import IDENTIFIER_SCHEMES
from ligacoes.core.models import (
    Event,
    EventEntitySummary,
    EventPairSummary,
    EventParty,
    Evidence,
    Relationship,
    SourceIdentity,
)

PUBLIC_EVIDENCE_LIMIT = 10
PUBLIC_RELATIONSHIP_LIMIT = 100
# Internal ids (Government portal, EpT, Commission) are never shown.
PUBLIC_IDENTIFIER_SCHEMES = frozenset(
    scheme for scheme, info in IDENTIFIER_SCHEMES.items() if info.public
)


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
            rows = rows.filter(counterpart__name__icontains=name)
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
    # Starts from the entity's own party rows (``event_party_entity_idx``) and reaches the
    # other parties of the same events; ``event__parties`` below is the counterpart row.
    parties = (
        EventParty.objects.filter(
            *_public_conditions("event__"), *_scope_conditions(kind, at, "event__"), entity=entity
        )
        # Visible events have no unresolved party, so ``<>`` (which drops NULLs) equals
        # excluding the entity itself.
        .alias(
            other=F("event__parties__entity_id"),
            other_pk=F("event__parties__pk"),
            other_role=F("event__parties__role"),
        )
        .filter(~Q(other=entity.pk))
        # Aliases can repeat an entity and role on one event: count each pair of
        # (entity, role) rows once, keeping the lowest-id row on each side.
        .filter(
            ~Exists(
                EventParty.objects.filter(
                    event_id=OuterRef("event_id"),
                    entity_id=OuterRef("entity_id"),
                    role=OuterRef("role"),
                    pk__lt=OuterRef("pk"),
                )
            ),
            ~Exists(
                EventParty.objects.filter(
                    event_id=OuterRef("event_id"),
                    entity_id=OuterRef("other"),
                    role=OuterRef("other_role"),
                    pk__lt=OuterRef("other_pk"),
                )
            ),
        )
    )
    if counterpart is not None:
        parties = parties.filter(other=counterpart.pk)
    if name:
        parties = parties.alias(other_name=F("event__parties__entity__name")).filter(
            other_name__icontains=name
        )
    return (
        parties.values(
            counterpart_id=F("event__parties__entity_id"),
            kind=F("event__kind"),
            role_of_entity=F("role"),
            role_of_counterpart=F("event__parties__role"),
        )
        .annotate(count=Count("event_id", distinct=True), **_event_summary("event__"))
        .order_by(*_COUNTERPART_ORDER)
    )


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


def event_totals(entity, *, at=None):
    """Per-kind public event totals for a profile summary.

    Without a date they come from ``EventEntitySummary``; a date needs the events.
    """
    if at is not None:
        return event_summary(entity_events(entity, at=at)).order_by("kind")
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


def event_datasets(entity, *, at=None):
    """Public events of ``entity`` per catalogue dataset."""
    if at is not None:
        return entity_events(entity, at=at).order_by().values("dataset").annotate(count=Count("pk"))
    return _stored_totals(entity).order_by().values("dataset").annotate(count=Sum("event_count"))


def evidence_datasets(relationships):
    """Public relationships among ``relationships`` per dataset of their public evidence."""
    return (
        public_evidence()
        .filter(relationship__in=relationships.prefetch_related(None).order_by().values("pk"))
        .order_by()
        .values(dataset=F("source__dataset"))
        .annotate(count=Count("relationship_id", distinct=True))
    )


def public_identities(entity):
    """Identifiers of a public profile under schemes that may be shown publicly."""
    return SourceIdentity.objects.filter(
        entity=entity, entity__is_public=True, source__in=PUBLIC_IDENTIFIER_SCHEMES
    ).order_by("source", "external_id")
