"""Public sources page: every catalogued dataset, its reuse terms and live figures."""

from dataclasses import dataclass
from datetime import datetime

from django.core.cache import cache
from django.db.models import Count, Max
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from ligacoes.core.catalogue import DATASETS, IDENTIFIER_SCHEMES, Dataset

from .selectors import public_events, public_evidence

# Counting public events means scanning up to millions of rows, so the
# figures are shared for a short while instead of computed per request.
FIGURES_CACHE_KEY = "public:sources:figures:v1"
FIGURES_TTL_SECONDS = 600

# Per dataset: (published relationships, published events, latest retrieval).
type FigureRow = tuple[int, int, datetime | None]


@dataclass(frozen=True)
class Identifier:
    label: str
    public: bool


@dataclass(frozen=True)
class SourceEntry:
    dataset: Dataset
    identifiers: tuple[Identifier, ...]
    relationships: int
    events: int
    retrieved_at: datetime | None


@dataclass(frozen=True)
class PublisherGroup:
    publisher: str
    entries: tuple[SourceEntry, ...]


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def compute_figures() -> dict[str, FigureRow]:
    """Two grouped queries over the public selectors, keyed by catalogue dataset."""
    keys = list(DATASETS)
    relationships = (
        public_evidence()
        .filter(source__dataset__in=keys)
        .order_by()
        .values("source__dataset")
        .annotate(total=Count("relationship", distinct=True), latest=Max("source__retrieved_at"))
        .values_list("source__dataset", "total", "latest")
    )
    events = (
        public_events()
        .filter(dataset__in=keys)
        .order_by()
        .values("dataset")
        .annotate(total=Count("pk"), latest=Max("retrieved_at"))
        .values_list("dataset", "total", "latest")
    )
    figures: dict[str, FigureRow] = {}
    for key, total, latest in relationships:
        figures[key] = (total, 0, latest)
    for key, total, latest in events:
        linked, _, retrieved = figures.get(key, (0, 0, None))
        figures[key] = (linked, total, _latest(retrieved, latest))
    return figures


def cached_figures() -> tuple[dict[str, FigureRow], datetime]:
    cached: tuple[dict[str, FigureRow], datetime] | None = cache.get(FIGURES_CACHE_KEY)
    if cached is None:
        cached = (compute_figures(), timezone.now())
        cache.set(FIGURES_CACHE_KEY, cached, FIGURES_TTL_SECONDS)
    return cached


def _identifiers(dataset: Dataset) -> tuple[Identifier, ...]:
    identifiers: list[Identifier] = []
    for scheme in dataset.identifiers:
        info = IDENTIFIER_SCHEMES.get(scheme)
        if info is not None:
            identifiers.append(Identifier(info.label, info.public))
    return tuple(identifiers)


def _entry(dataset: Dataset, figures: dict[str, FigureRow]) -> SourceEntry:
    relationships, events, retrieved_at = figures.get(dataset.key, (0, 0, None))
    return SourceEntry(dataset, _identifiers(dataset), relationships, events, retrieved_at)


@require_GET
def sources_index(request: HttpRequest) -> HttpResponse:
    figures, computed_at = cached_figures()
    by_publisher: dict[str, list[SourceEntry]] = {}
    upcoming: list[SourceEntry] = []
    secondary: list[SourceEntry] = []
    for dataset in DATASETS.values():
        entry = _entry(dataset, figures)
        if dataset.status == "imported":
            by_publisher.setdefault(dataset.publisher, []).append(entry)
        elif dataset.status == "upcoming":
            upcoming.append(entry)
        else:
            secondary.append(entry)
    imported = [
        PublisherGroup(publisher, tuple(entries)) for publisher, entries in by_publisher.items()
    ]
    return render(
        request,
        "public/sources.html",
        {
            "imported": imported,
            "imported_count": sum(len(group.entries) for group in imported),
            "upcoming": upcoming,
            "secondary": secondary,
            "computed_at": computed_at,
            "refresh_minutes": FIGURES_TTL_SECONDS // 60,
        },
    )
