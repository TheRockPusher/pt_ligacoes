"""Official dated records between parties, published automatically when fully anchored."""

import datetime as dt
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import batched
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .catalogue import DATASETS
from .event_summaries import event_entities, rebuild_event_summaries
from .identity import anchor_schemes, authorised_reviewer
from .models import (
    Entity,
    Event,
    EventParty,
    Evidence,
    Source,
    SourceIdentity,
    editorial_transaction,
)


@dataclass(frozen=True)
class PartyInput:
    role: str
    name: str
    entity: Entity | None = None
    identifier: str = ""


@dataclass(frozen=True)
class EventInput:
    record_id: str
    kind: str
    title: str
    date: dt.date | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    amount: Decimal | None = None
    currency: str = "EUR"
    amount_label: str = ""
    record_url: str = ""
    details: dict = field(default_factory=dict)
    parties: tuple[PartyInput, ...] = ()


# Fields a changed record rewrites; parties are replaced alongside.
CHANGED_FIELDS = (
    "scope",
    "kind",
    "title",
    "date",
    "start_date",
    "end_date",
    "amount",
    "currency",
    "amount_label",
    "record_url",
    "details",
    "source",
    "fingerprint",
    "status",
    "published_at",
    "as_of",
    "retrieved_at",
)
SEEN_FIELDS = ("status", "published_at", "as_of", "retrieved_at")
CURRENT = (Event.Status.DRAFT, Event.Status.PUBLISHED)


def dataset_source(dataset: str) -> Source:
    """The public Source row citing a catalogue dataset; an editor may hide it."""
    entry = DATASETS.get(dataset)
    if entry is None:
        raise ValidationError("Conjunto de dados fora do catálogo.")
    source = (
        Source.objects.filter(
            dataset=dataset, url=entry.url, title=entry.title, publisher=entry.publisher
        )
        # Claim evidence sources are per-retrieval rows; never reuse one for events.
        .filter(~Exists(Evidence.objects.filter(source=OuterRef("pk"))))
        .order_by("pk")
        .first()
    )
    if source is None:
        source = Source.objects.create(
            title=entry.title,
            url=entry.url,
            publisher=entry.publisher,
            dataset=dataset,
            is_public=True,
        )
    return source


def _parties(item: EventInput) -> tuple[PartyInput, ...]:
    """Distinct parties in canonical order; one published party maps to one entity."""
    if not item.parties:
        raise ValidationError("Um evento exige pelo menos um interveniente.")
    unique: dict[tuple[str, str, str], PartyInput] = {}
    for party in item.parties:
        key = (party.role, party.name, party.identifier)
        known = unique.setdefault(key, party)
        if (known.entity.pk if known.entity else None) != (
            party.entity.pk if party.entity else None
        ):
            raise ValidationError("O mesmo interveniente não pode apontar entidades diferentes.")
    return tuple(unique[key] for key in sorted(unique))


def _fingerprint(item: EventInput, parties: tuple[PartyInput, ...]) -> str:
    payload = {
        "kind": item.kind,
        "title": item.title,
        "date": item.date.isoformat() if item.date else None,
        "start_date": item.start_date.isoformat() if item.start_date else None,
        "end_date": item.end_date.isoformat() if item.end_date else None,
        "amount": None if item.amount is None else f"{item.amount:.2f}",
        "currency": item.currency,
        "amount_label": item.amount_label,
        "record_url": item.record_url,
        "details": item.details,
        "parties": [
            [
                party.role,
                party.name,
                party.identifier,
                str(party.entity.pk) if party.entity else None,
            ]
            for party in parties
        ],
    }
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Os detalhes do evento têm de ser JSON simples.") from exc
    return hashlib.sha256(text.encode()).hexdigest()


def _build(
    item: EventInput,
    parties: tuple[PartyInput, ...],
    *,
    dataset: str,
    scope: str,
    source: Source,
    as_of: dt.date,
    now: dt.datetime,
) -> tuple[Event, list[EventParty]]:
    if item.amount is not None and item.amount < 0:
        raise ValidationError("O montante de um evento não pode ser negativo.")
    event = Event(
        dataset=dataset,
        scope=scope,
        record_id=item.record_id,
        kind=item.kind,
        title=item.title,
        date=item.date,
        start_date=item.start_date,
        end_date=item.end_date,
        amount=item.amount,
        currency=item.currency,
        amount_label=item.amount_label,
        record_url=item.record_url,
        details=item.details,
        source=source,
        fingerprint=_fingerprint(item, parties),
        as_of=as_of,
        retrieved_at=now,
    )
    # Per-row FK/constraint queries are left to the database; field rules checked here.
    event.full_clean(exclude=["source"], validate_unique=False, validate_constraints=False)
    rows = []
    for party in parties:
        row = EventParty(
            event=event,
            entity=party.entity,
            role=party.role,
            name=party.name,
            identifier=party.identifier,
        )
        row.full_clean(
            exclude=["event", "entity"], validate_unique=False, validate_constraints=False
        )
        rows.append(row)
    return event, rows


def _publishable(parties: Iterable[PartyInput]) -> set[object]:
    ids = {party.entity.pk for party in parties if party.entity is not None}
    return set(
        Entity.objects.filter(pk__in=ids, is_public=True)
        .filter(
            Exists(SourceIdentity.objects.filter(entity=OuterRef("pk"), source__in=anchor_schemes))
        )
        .values_list("pk", flat=True)
    )


def _status(event: Event, previous: Event | None, published: bool, now: dt.datetime) -> None:
    if published:
        event.status = Event.Status.PUBLISHED
        event.published_at = (
            previous.published_at
            if previous is not None and previous.status == Event.Status.PUBLISHED
            else now
        )
    else:
        event.status = Event.Status.DRAFT
        event.published_at = None


def sync_events(
    *,
    dataset: str,
    scope: str,
    events: Iterable[EventInput],
    as_of: dt.date,
    complete: bool = True,
    batch_size: int = 5000,
) -> dict[str, int]:
    """Stream a scoped snapshot of official records in batches under one editorial lock.

    Records are upserted by ``(dataset, record_id)``; withdrawn records keep their state.
    """
    if not scope or len(scope) > 64 or batch_size < 1:
        raise ValidationError("Âmbito de eventos inválido.")
    result = dict.fromkeys(("created", "changed", "unchanged", "ceased", "published", "draft"), 0)
    with editorial_transaction():
        if Event.objects.filter(dataset=dataset, scope=scope, as_of__gt=as_of).exists():
            raise ValidationError(
                "Não é possível substituir uma observação mais recente por uma anterior."
            )
        source = dataset_source(dataset)
        # Every record seen in this run carries this exact marker; absence is detected
        # without holding the complete list of record ids in memory.
        now = timezone.now()
        touched: set[UUID] = set()
        for chunk in batched(events, batch_size, strict=False):
            normalised: dict[str, tuple[EventInput, tuple[PartyInput, ...]]] = {}
            for item in chunk:
                if item.record_id in normalised:
                    raise ValidationError("Identificador de registo repetido na observação.")
                normalised[item.record_id] = (item, _parties(item))
            publishable = _publishable(
                party for _, parties in normalised.values() for party in parties
            )
            existing = {
                event.record_id: event
                for event in Event.objects.filter(
                    dataset=dataset, record_id__in=list(normalised)
                ).order_by()
            }
            created: list[Event] = []
            changed: list[Event] = []
            seen: list[Event] = []
            withdrawn: list[object] = []
            party_rows: list[EventParty] = []
            # Events whose parties change or whose published state flips move summaries.
            replaced: list[object] = []
            flipped: list[object] = []
            for record_id, (item, parties) in normalised.items():
                event, rows = _build(
                    item,
                    parties,
                    dataset=dataset,
                    scope=scope,
                    source=source,
                    as_of=as_of,
                    now=now,
                )
                previous = existing.get(record_id)
                if previous is not None and previous.status == Event.Status.WITHDRAWN:
                    withdrawn.append(previous.pk)
                    result["unchanged"] += 1
                    continue
                if previous is not None and previous.retrieved_at == now:
                    raise ValidationError("Identificador de registo repetido na observação.")
                published = all(
                    party.entity is not None and party.entity.pk in publishable for party in parties
                )
                result["published" if published else "draft"] += 1
                if previous is None:
                    _status(event, None, published, now)
                    created.append(event)
                    party_rows.extend(rows)
                    result["created"] += 1
                elif (
                    previous.fingerprint != event.fingerprint
                    or previous.scope != scope
                    or previous.source_id != source.pk
                ):
                    event.pk = previous.pk
                    _status(event, previous, published, now)
                    for row in rows:
                        row.event = event
                    changed.append(event)
                    party_rows.extend(rows)
                    result["changed"] += 1
                else:
                    was_published = previous.status == Event.Status.PUBLISHED
                    _status(previous, previous, published, now)
                    if was_published != published:
                        flipped.append(previous.pk)
                    previous.as_of = as_of
                    previous.retrieved_at = now
                    seen.append(previous)
                    result["unchanged"] += 1
            Event.objects.bulk_create(created, batch_size=1000)
            if changed:
                replaced = [event.pk for event in changed]
                touched |= event_entities(replaced)
                EventParty.objects.filter(event__in=replaced).delete()
                Event.objects.bulk_update(changed, CHANGED_FIELDS, batch_size=1000)
            EventParty.objects.bulk_create(party_rows, batch_size=1000)
            if seen:
                Event.objects.bulk_update(seen, SEEN_FIELDS, batch_size=1000)
            touched |= {row.entity_id for row in party_rows if row.entity_id is not None}
            touched |= event_entities(flipped)
            # Anchors an event relied on are frozen so later imports cannot move its parties.
            relied = {row.entity_id for row in party_rows if row.entity_id is not None}
            relied |= event_entities(flipped)
            if relied:
                SourceIdentity.objects.filter(
                    entity_id__in=relied, source__in=anchor_schemes, used_at__isnull=True
                ).update(used_at=now)
            if withdrawn:
                Event.objects.filter(pk__in=withdrawn).update(as_of=as_of)
        if complete:
            ceasing = Event.objects.filter(
                dataset=dataset, scope=scope, status__in=CURRENT, retrieved_at__lt=now
            )
            touched |= event_entities(
                ceasing.filter(status=Event.Status.PUBLISHED).values_list("pk", flat=True)
            )
            result["ceased"] = ceasing.update(
                status=Event.Status.CEASED, published_at=None, as_of=as_of
            )
        rebuild_event_summaries(entities=touched, dataset=dataset)
    return result


@editorial_transaction()
def withdraw_event(event: Event, reviewer: object) -> Event:
    """Retire an event; later imports never republish or modify it."""
    actor = authorised_reviewer(reviewer, "core.withdraw_event")
    current = Event.objects.select_for_update().get(pk=event.pk)
    if current.status != Event.Status.WITHDRAWN:
        was_published = current.status == Event.Status.PUBLISHED
        Event.objects.filter(pk=current.pk).update(
            status=Event.Status.WITHDRAWN,
            published_at=None,
            withdrawn_by=actor,
            withdrawn_at=timezone.now(),
        )
        if was_published:
            rebuild_event_summaries(entities=event_entities([current.pk]), dataset=current.dataset)
        current.refresh_from_db()
    return current
