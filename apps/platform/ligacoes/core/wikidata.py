"""Wikidata crosswalk: secondary QID hints joined to entities only through official ids.

Wikidata (CC0) is never evidence: it never creates entities, claims or reviewed identities.
Five fixed SPARQL queries (verified 2026-09-28) read AR deputy ids (P6199), MEP ids of
Portuguese MEPs (P1186), Portuguese VAT numbers (P3608), LEIs (P1278) and EU Transparency
Register ids (P2657). A QID becomes an unreviewed ``wikidata`` identity only when its
official ids reach exactly one existing entity; ids from sources not yet linked become
pending editorial suggestions for that entity. Natural-person VAT numbers are dropped on
read and never kept or logged.
"""

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from .identity import valid_nipc
from .models import (
    Entity,
    IdentityScheme,
    IdentitySuggestion,
    SourceIdentity,
    import_transaction,
)
from .official_http import OfficialHTTPError, download
from .parliament_parse import JSONValue

SPARQL_URL = "https://query.wikidata.org/sparql"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000
# The query service stops queries at 60 s.
QUERY_TIMEOUT = 70
# Query service etiquette: sequential queries at least a second apart.
PAUSE = 1.0
BATCH = 5000
ITEM = re.compile(r"http://www\.wikidata\.org/entity/(Q[1-9][0-9]*)")
WIKIDATA = IdentityScheme.WIKIDATA
PARLIAMENT = IdentityScheme.PARLIAMENT
EP = IdentityScheme.EP
NIPC = IdentityScheme.NIPC
LEI = IdentityScheme.LEI
EU_TR = IdentityScheme.EU_TR
# Official schemes through which a QID may join an existing entity.
ANCHORS: tuple[str, ...] = (PARLIAMENT, NIPC, LEI, EU_TR)
DISCLAIMER = "Wikidata (fonte secundária, CC0)"


class WikidataError(ValueError):
    """Safe, payload-free failure for an unavailable or malformed Wikidata answer."""


@dataclass(frozen=True)
class Query:
    property: str
    scheme: str
    variable: str
    # Full-match format of the value (Wikidata P1793); group 1, if any, is the official id.
    pattern: re.Pattern[str]
    sparql: str


QUERIES: tuple[Query, ...] = (
    Query(
        "P6199",
        PARLIAMENT,
        "id",
        re.compile(r"[1-9][0-9]*"),
        "SELECT ?item ?id WHERE { ?item wdt:P6199 ?id }",
    ),
    Query(
        "P1186",
        EP,
        "id",
        re.compile(r"[1-9][0-9]{0,5}"),
        "SELECT DISTINCT ?item ?id WHERE { ?item wdt:P1186 ?id . "
        "{ ?item wdt:P27 wd:Q45 } UNION { ?item p:P39 ?st . ?st ps:P39 wd:Q27169 ; "
        "pq:P768 ?c . ?c wdt:P17 wd:Q45 } }",
    ),
    Query(
        "P3608",
        NIPC,
        "vat",
        re.compile(r"PT([0-9]{9})"),
        'SELECT ?item ?vat WHERE { ?item wdt:P3608 ?vat . FILTER(STRSTARTS(?vat,"PT")) }',
    ),
    Query(
        "P1278",
        LEI,
        "lei",
        re.compile(r"[0-9A-Z]{18}[0-9]{2}"),
        "SELECT DISTINCT ?item ?lei WHERE { ?item wdt:P1278 ?lei . "
        "{ ?item wdt:P17 wd:Q45 } UNION { ?item wdt:P159/wdt:P17 wd:Q45 } UNION "
        '{ ?item wdt:P3608 ?v . FILTER(STRSTARTS(?v,"PT")) } }',
    ),
    Query(
        "P2657",
        EU_TR,
        "tr",
        re.compile(r"[0-9]{8,13}-[0-9]{2}"),
        "SELECT DISTINCT ?item ?tr WHERE { ?item wdt:P2657 ?tr . "
        "{ ?item wdt:P17 wd:Q45 } UNION { ?item wdt:P159/wdt:P17 wd:Q45 } UNION "
        '{ ?item wdt:P3608 ?v . FILTER(STRSTARTS(?v,"PT")) } }',
    ),
)


@dataclass(frozen=True)
class CrosswalkSnapshot:
    # QID -> scheme -> official ids carried by the item.
    items: dict[str, dict[str, frozenset[str]]]
    rows: int
    dropped: int


@dataclass(frozen=True)
class Proposal:
    scheme: str
    external_id: str
    candidate_id: UUID
    qid: str
    basis: str
    new: bool


@dataclass(frozen=True)
class CrosswalkPlan:
    # QID -> entity for hints not stored yet.
    hints: dict[str, UUID]
    unchanged: int
    conflicts: int
    unmatched: int
    proposals: tuple[Proposal, ...]


def _allowed(url: str) -> bool:
    return url == SPARQL_URL


def _run(query: Query) -> JSONValue:
    try:
        content = download(
            SPARQL_URL,
            allowed=_allowed,
            max_bytes=MAX_BYTES,
            deadline=time.monotonic() + QUERY_TIMEOUT,
            method="POST",
            body=urlencode({"query": query.sparql}).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/sparql-results+json",
            },
        )
    except OfficialHTTPError as exc:
        raise WikidataError(
            f"Não foi possível consultar o Wikidata ({query.property}) em segurança."
        ) from exc
    try:
        return cast(JSONValue, json.loads(content))
    except (ValueError, RecursionError) as exc:
        raise WikidataError(f"JSON do Wikidata inválido ({query.property}).") from exc


def _value(binding: JSONValue, variable: str, query: Query) -> str:
    cell = binding.get(variable) if isinstance(binding, dict) else None
    value = cell.get("value") if isinstance(cell, dict) else None
    if not isinstance(value, str):
        raise WikidataError(f"Resultado do Wikidata malformado ({query.property}).")
    return value


def parse_results(query: Query, payload: JSONValue) -> tuple[list[tuple[str, str]], int]:
    """(QID, official id) pairs; off-format values and natural-person VAT are dropped."""
    results = payload.get("results") if isinstance(payload, dict) else None
    bindings = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(bindings, list):
        raise WikidataError(f"Resposta do Wikidata malformada ({query.property}).")
    if len(bindings) > MAX_ROWS:
        raise WikidataError(f"Resposta do Wikidata excessiva ({query.property}).")
    pairs: list[tuple[str, str]] = []
    dropped = 0
    for binding in bindings:
        item = ITEM.fullmatch(_value(binding, "item", query))
        value = query.pattern.fullmatch(_value(binding, query.variable, query))
        if item is None or value is None:
            dropped += 1
            continue
        official = value.group(1) if query.pattern.groups else value.group(0)
        if query.scheme == NIPC and not valid_nipc(official):
            dropped += 1
            continue
        pairs.append((item.group(1), official))
    return pairs, dropped


def fetch_snapshot() -> CrosswalkSnapshot:
    """Every fixed query, sequentially; any failure aborts the whole run."""
    collected: dict[str, dict[str, set[str]]] = {}
    rows = dropped = 0
    for index, query in enumerate(QUERIES):
        if index:
            time.sleep(PAUSE)
        pairs, skipped = parse_results(query, _run(query))
        rows += len(pairs)
        dropped += skipped
        for qid, official in pairs:
            collected.setdefault(qid, {}).setdefault(query.scheme, set()).add(official)
    return CrosswalkSnapshot(
        items={
            qid: {scheme: frozenset(ids) for scheme, ids in schemes.items()}
            for qid, schemes in collected.items()
        },
        rows=rows,
        dropped=dropped,
    )


def _identities(scheme: str, external_ids: Iterable[str]) -> dict[str, tuple[UUID, str]]:
    """External id -> (entity, entity kind) for the ids already mapped in ``scheme``."""
    found: dict[str, tuple[UUID, str]] = {}
    for chunk in batched(sorted(set(external_ids)), BATCH, strict=False):
        for external_id, entity_id, kind in SourceIdentity.objects.filter(
            source=scheme, external_id__in=chunk
        ).values_list("external_id", "entity_id", "entity__kind"):
            found[external_id] = (entity_id, kind)
    return found


def _through(
    ids: dict[str, frozenset[str]],
    anchors: dict[str, dict[str, tuple[UUID, str]]],
    scheme: str,
    entity_id: UUID,
) -> list[str]:
    """The item's ``scheme`` ids that officially identify ``entity_id``."""
    return sorted(
        external_id
        for external_id in ids.get(scheme, ())
        if anchors[scheme].get(external_id, (None, ""))[0] == entity_id
    )


def plan_snapshot(snapshot: CrosswalkSnapshot) -> CrosswalkPlan:
    """Read-only: which QIDs join exactly one entity, and which suggestions follow."""
    anchors = {
        scheme: _identities(
            scheme, (i for ids in snapshot.items.values() for i in ids.get(scheme, ()))
        )
        for scheme in ANCHORS
    }
    targets: dict[str, set[UUID]] = {}
    for qid, ids in snapshot.items.items():
        found: set[UUID] = set()
        for scheme in ANCHORS:
            for external_id in ids.get(scheme, ()):
                match = anchors[scheme].get(external_id)
                # AR DepCadIds identify deputies; organisation registers never persons.
                if match is not None and (match[1] == Entity.Kind.PERSON) == (scheme == PARLIAMENT):
                    found.add(match[0])
        if found:
            targets[qid] = found
    stored = {
        qid: entity_id
        for chunk in batched(sorted(targets), BATCH, strict=False)
        for qid, entity_id in SourceIdentity.objects.filter(
            source=WIKIDATA, external_id__in=chunk
        ).values_list("external_id", "entity_id")
    }
    single = {qid: next(iter(found)) for qid, found in targets.items() if len(found) == 1}
    held: dict[UUID, set[str]] = {}
    for chunk in batched(sorted(set(single.values())), BATCH, strict=False):
        for qid, entity_id in SourceIdentity.objects.filter(
            source=WIKIDATA, entity_id__in=chunk
        ).values_list("external_id", "entity_id"):
            held.setdefault(entity_id, set()).add(qid)
    claimants: dict[UUID, int] = {}
    for qid, entity_id in single.items():
        if qid not in stored:
            claimants[entity_id] = claimants.get(entity_id, 0) + 1
    hints: dict[str, UUID] = {}
    anchored: dict[str, UUID] = {}
    unchanged = 0
    for qid, entity_id in single.items():
        if qid in stored:
            if stored[qid] == entity_id:
                unchanged += 1
                anchored[qid] = entity_id
        elif not held.get(entity_id) and claimants[entity_id] == 1:
            hints[qid] = entity_id
            anchored[qid] = entity_id
    conflicts = len(targets) - len(anchored)
    proposals = _proposals(snapshot, anchors, anchored)
    return CrosswalkPlan(
        hints=hints,
        unchanged=unchanged,
        conflicts=conflicts,
        unmatched=len(snapshot.items) - len(targets),
        proposals=proposals,
    )


def _proposals(
    snapshot: CrosswalkSnapshot,
    anchors: dict[str, dict[str, tuple[UUID, str]]],
    anchored: dict[str, UUID],
) -> tuple[Proposal, ...]:
    """EP ids of anchored deputies and TR ids of NIPC-anchored organisations."""
    wanted: dict[tuple[str, str, UUID], tuple[str, str]] = {}
    for qid, entity_id in sorted(anchored.items()):
        ids = snapshot.items[qid]
        deputies = _through(ids, anchors, PARLIAMENT, entity_id)
        if deputies:
            for ep_id in sorted(ids.get(EP, ())):
                wanted[(EP, ep_id, entity_id)] = (
                    qid,
                    f"{DISCLAIMER}: o item {qid} regista o identificador de deputado da "
                    f"Assembleia da República {', '.join(deputies)} (P6199) e o identificador "
                    f"de deputado ao Parlamento Europeu {ep_id} (P1186); o identificador da "
                    "Assembleia já corresponde a esta pessoa.",
                )
        nipcs = _through(ids, anchors, NIPC, entity_id)
        if nipcs:
            for tr_id in sorted(ids.get(EU_TR, ())):
                wanted[(EU_TR, tr_id, entity_id)] = (
                    qid,
                    f"{DISCLAIMER}: o item {qid} regista o NIPC {', '.join(nipcs)} (P3608) e o "
                    f"identificador do Registo de Transparência da UE {tr_id} (P2657); o NIPC "
                    "já corresponde a esta organização.",
                )
    mapped: dict[str, dict[str, tuple[UUID, str]]] = {
        EP: _identities(EP, (key[1] for key in wanted if key[0] == EP)),
        EU_TR: anchors[EU_TR],
    }
    decided: dict[tuple[str, str, UUID], str] = {}
    for scheme in (EP, EU_TR):
        external_ids = sorted({key[1] for key in wanted if key[0] == scheme})
        for chunk in batched(external_ids, BATCH, strict=False):
            for external_id, candidate_id, status in IdentitySuggestion.objects.filter(
                scheme=scheme, external_id__in=chunk
            ).values_list("external_id", "candidate_id", "status"):
                decided[(scheme, external_id, candidate_id)] = status
    return tuple(
        Proposal(
            scheme=scheme,
            external_id=external_id,
            candidate_id=candidate_id,
            qid=qid,
            basis=basis,
            new=(scheme, external_id, candidate_id) not in decided,
        )
        for (scheme, external_id, candidate_id), (qid, basis) in wanted.items()
        if (external_id not in mapped[scheme] or mapped[scheme][external_id][0] != candidate_id)
        and decided.get((scheme, external_id, candidate_id), IdentitySuggestion.Status.PENDING)
        == IdentitySuggestion.Status.PENDING
    )


def summary(snapshot: CrosswalkSnapshot, plan: CrosswalkPlan) -> dict[str, int]:
    new = sum(proposal.new for proposal in plan.proposals)
    return {
        "items": len(snapshot.items),
        "rows": snapshot.rows,
        "dropped": snapshot.dropped,
        "hints": len(plan.hints),
        "unchanged": plan.unchanged,
        "conflicts": plan.conflicts,
        "unmatched": plan.unmatched,
        "suggestions_new": new,
        "suggestions_existing": len(plan.proposals) - new,
    }


def apply_snapshot(snapshot: CrosswalkSnapshot) -> dict[str, int]:
    """Atomic: unreviewed QID hints and pending suggestions; no entity, claim or review."""
    with import_transaction():
        plan = plan_snapshot(snapshot)
        identities = [
            SourceIdentity(source=WIKIDATA, external_id=qid, entity_id=entity_id)
            for qid, entity_id in plan.hints.items()
        ]
        for identity in identities:
            identity.full_clean(
                exclude=["entity"], validate_unique=False, validate_constraints=False
            )
        SourceIdentity.objects.bulk_create(identities, batch_size=1000)
        candidates = Entity.objects.in_bulk({proposal.candidate_id for proposal in plan.proposals})
        for proposal in plan.proposals:
            # Also retain a hint when that identifier already points at a separate
            # profile: late-arriving crosswalks must be available to reconciliation.
            IdentitySuggestion.objects.update_or_create(
                scheme=proposal.scheme,
                external_id=proposal.external_id,
                candidate=candidates[proposal.candidate_id],
                defaults={
                    "name_as_published": f"Wikidata {proposal.qid}",
                    "basis": proposal.basis,
                },
            )
        return summary(snapshot, plan)


def corroborating_qid(scheme: str, external_id: str, candidate: Entity) -> str:
    """Read an imported AR/EP crosswalk hint carrying both exact official identifiers.

    The crosswalk stores the candidate's QID as a SourceIdentity and the missing
    EP identifier as a suggestion. A QID by itself, or a name-based suggestion,
    is not a two-identifier signal. Rejected suggestions are not reused.
    """
    if scheme != EP:
        return ""
    qids = set(
        SourceIdentity.objects.filter(source=WIKIDATA, entity=candidate).values_list(
            "external_id", flat=True
        )
    )
    deputies = set(
        SourceIdentity.objects.filter(source=PARLIAMENT, entity=candidate).values_list(
            "external_id", flat=True
        )
    )
    if not qids or not deputies:
        return ""
    suggestions = (
        IdentitySuggestion.objects.filter(
            scheme=scheme,
            external_id=external_id,
            candidate=candidate,
        )
        .exclude(status=IdentitySuggestion.Status.REJECTED)
        .order_by("pk")
    )
    for suggestion in suggestions:
        item = re.fullmatch(r"Wikidata (Q[1-9][0-9]*)", suggestion.name_as_published)
        if item is None or item.group(1) not in qids:
            continue
        qid = item.group(1)
        signal = re.fullmatch(
            rf"{re.escape(DISCLAIMER)}: o item {qid} regista o identificador de deputado da "
            r"Assembleia da República ([0-9]+(?:, [0-9]+)*) \(P6199\) e o identificador "
            rf"de deputado ao Parlamento Europeu {re.escape(external_id)} \(P1186\); o identificador da "
            r"Assembleia já corresponde a esta pessoa\.",
            suggestion.basis,
        )
        if signal is not None and deputies.intersection(signal.group(1).split(", ")):
            return qid
    return ""
