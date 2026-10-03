"""AR ``Atividades<Leg>``: committee hearings and audiences, and external-body elections.

Hearings and audiences become ``hearing`` events hosted by the committee (the legislature's
AR organ entity). Their listed entities are free text and stay unresolved name-only
attendees, so those events remain private for editorial resolution. People elected to
external bodies (``OEX``) publish automatically when verifiable; the parliamentary-group suffix
published after deputies' names is party-like information and is never stored.
"""

import contextlib
import hashlib
import http.client
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_scoped_snapshot
from .events import EventInput, PartyInput, sync_events
from .government import revised
from .identity import normalise_name
from .models import (
    EnrichmentSource,
    Entity,
    Event,
    EventParty,
    IdentityScheme,
    Relationship,
    SourceIdentity,
    SourceObservation,
    import_transaction,
)
from .parliament_bodies import _clean, _load, legislature_label, role_class
from .parliament_fetch import (
    Download,
    ParliamentImportError,
    discover_download,
    validate_file_legislature,
    validate_url,
)
from .parliament_parse import JSONObject, JSONValue, _date, _identifier, _object, _rows, _text
from .validators import validate_source_url

DATASET = "ar_atividades"
FETCH_DATASET = "activities"
TITLE_LIMIT = 500
NAME_LIMIT = 300
# "<seq>-<COMMITTEE>-<Leg>"; one XVI number carries a stray space before the last dash.
NUMBER = re.compile(r"^(\d+)-\s*([^-\s][^-]*?)\s*-\s*([IVXLCDM]+|IA|IB|Cons)$")
# Collective placeholders, not identifiable attendees.
PLACEHOLDERS = frozenset(
    {"peticionarios", "peticionarias", "peticionario", "peticionaria", "peticionantes"}
)
# Audiences that did not take place are not hearings.
NOT_HELD = frozenset({"naorealizada"})
# Resignation notices share the OEX type but record departures, not elections.
RESIGNATION = re.compile(r"\bren[uú]ncia\b", re.IGNORECASE)
# "Full Name (GP)": a short trailing acronym is the parliamentary group/party of a deputy.
GROUP_SUFFIX = re.compile(r"\s*\([^()\s]{1,15}\)\s*$")
STOPWORDS = frozenset(
    {"a", "o", "as", "os", "à", "às", "ao", "aos", "e", "de", "da", "do", "das", "dos"}
    | {"em", "no", "na", "nos", "nas", "para", "com", "por", "pela", "pelo"}
)
# Years before the first constitutional legislature are export errors (e.g. "0200-05-18").
FIRST_YEAR = 1975
# Transient network failures (the server generates files on request) are retried.
ATTEMPTS = 3
BACKOFF = 10.0
# The I Legislatura's two file periods (IA, IB) carry the code "I" inside the files.
PUBLISHED_CODES = {"IA": ("IA", "I"), "IB": ("IB", "I")}


def published_codes(legislature: str) -> tuple[str, ...]:
    """Legislature codes the files of ``legislature`` may carry in their records."""
    return PUBLISHED_CODES.get(legislature, (legislature,))


@dataclass(frozen=True)
class Hearing:
    record_id: str
    kind: str  # "Audição" or "Audiência"
    number: str
    title: str
    date: date | None
    granted: str
    committee: str  # the acronym as published, "" when the number is unparseable
    attendees: tuple[str, ...]


@dataclass(frozen=True)
class Election:
    activity_id: str
    row: int
    name: str
    role: str
    body: str
    start: date | None
    source_url: str
    reference: str
    passage: str


@dataclass(frozen=True)
class ActivitiesSnapshot:
    legislature: str
    as_of: date
    url: str
    # Committee acronym (casefolded) → (AR organ key, official committee name).
    committees: dict[str, tuple[str, str]]
    hearings: tuple[Hearing, ...]
    elections: tuple[Election, ...]
    unmapped_committees: tuple[str, ...]
    skipped_placeholders: int
    skipped_not_held: int
    skipped_resignations: int
    skipped_without_parties: int
    invalid_dates: int


def _fold(value: str) -> str:
    return normalise_name(value).replace(" ", "")


def _initials(name: str) -> str:
    """Acronym-style initials; embedded acronyms (``PRR``, ``PT2030``) count whole."""
    parts = []
    for token in re.findall(r"\w+", name):
        if token.casefold() in STOPWORDS:
            continue
        parts.append(token if len(token) > 1 and token.upper() == token else token[0])
    return _fold("".join(parts))


def _subsequence(needle: str, haystack: str) -> bool:
    letters = iter(haystack)
    return all(char in letters for char in needle)


def _committees(bodies: JSONObject, legislature: str) -> dict[str, str]:
    """Committee organ id → official name, from the legislature's OrgaoComposicao file."""
    result: dict[str, str] = {}
    for organ in _rows(bodies.get("Comissoes"), "Comissoes", optional=True):
        raw = organ.get("DetalheOrgao")
        if raw is None:
            continue
        detail = _object(raw, "DetalheOrgao")
        if detail.get("siglaLegislatura") not in (None, *published_codes(legislature)):
            raise ParliamentImportError("Composition file does not match the legislature.")
        if detail.get("idOrgao") in (None, 0, 0.0):
            continue
        name = _clean(detail.get("nomeSigla"), "nomeSigla", optional=True, limit=500)
        if name:
            result[_identifier(detail.get("idOrgao"), "idOrgao")] = name
    return result


def _report_pairs(value: JSONValue, found: dict[str, set[str]]) -> None:
    """Committee acronym/id pairs published inside the committee reports."""
    if isinstance(value, dict):
        acronym, organ_id = value.get("Sigla"), value.get("Id")
        if isinstance(acronym, str) and acronym.strip() and organ_id not in (None, "", 0):
            with contextlib.suppress(ParliamentImportError):
                found.setdefault(_fold(acronym), set()).add(_identifier(organ_id, "Id"))
        for item in value.values():
            _report_pairs(item, found)
    elif isinstance(value, list):
        for item in value:
            _report_pairs(item, found)


def _initials_match(acronym: str, initials: dict[str, str]) -> set[str]:
    """Candidates of the first tier that matches anything: exact, prefix, ordered letters."""
    tiers: list[Callable[[str], bool]] = [
        lambda value: value == acronym,
        lambda value: value.startswith(acronym),
    ]
    if len(acronym) >= 5:
        tiers.append(lambda value: value[:1] == acronym[:1] and _subsequence(acronym, value))
    for matches in tiers:
        ids = {organ_id for organ_id, value in initials.items() if matches(value)}
        if ids:
            return ids
    return set()


def committee_map(
    acronyms: set[str], bodies: JSONObject, activities: JSONObject, legislature: str
) -> dict[str, tuple[str, str]]:
    """Map hearing acronyms to this legislature's committees; ambiguity stays unmapped.

    First the acronym/id pairs published in the committee reports (ids of other
    legislatures are ignored); then, among committees not already claimed that way, an
    exact, prefix or (long acronyms only) ordered match against the initials of the
    official committee names — always requiring exactly one candidate.
    """
    committees = _committees(bodies, legislature)
    reported: dict[str, set[str]] = {}
    _report_pairs(
        _object(activities.get("AtividadesGerais"), "AtividadesGerais").get("Relatorios"),
        reported,
    )
    found: dict[str, str] = {}
    for acronym in acronyms:
        ids = reported.get(acronym, set()) & committees.keys()
        if len(ids) == 1:
            found[acronym] = ids.pop()
    claimed = set(found.values())
    initials = {
        organ_id: _initials(name)
        for organ_id, name in committees.items()
        if organ_id not in claimed
    }
    for acronym in sorted(acronyms - found.keys()):
        ids = _initials_match(acronym, initials)
        if len(ids) == 1:
            found[acronym] = ids.pop()
    return {
        acronym: (f"orgao:{legislature}:{organ_id}", committees[organ_id])
        for acronym, organ_id in found.items()
    }


def _date_or_none(value: JSONValue, field: str) -> tuple[date | None, bool]:
    """The source date, or ``None`` flagged invalid for impossible export years."""
    parsed = _date(value, field)
    if parsed is not None and parsed.year < FIRST_YEAR:
        return None, True
    return parsed, False


def _attendees(value: JSONValue) -> tuple[tuple[str, ...], int]:
    text = _text(value, "Entidades", optional=True, limit=20000)
    names: list[str] = []
    skipped = 0
    for segment in text.split(";"):
        name = " ".join(segment.split())[:NAME_LIMIT].strip()
        if not name:
            continue
        if _fold(name) in PLACEHOLDERS:
            skipped += 1
            continue
        if name not in names:
            names.append(name)
    return tuple(names), skipped


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hearings(root: JSONObject, legislature: str) -> tuple[list[Hearing], dict[str, int]]:
    hearings: list[Hearing] = []
    counts = dict.fromkeys(("placeholders", "not_held", "invalid_dates", "without_parties"), 0)
    sections = (
        ("Audicoes", "IDAudicao", "NumeroAudicao", "Audição", "audicao"),
        ("Audiencias", "IDAudiencia", "NumeroAudiencia", "Audiência", "audiencia"),
    )
    for section, id_key, number_key, label, prefix in sections:
        for row in _rows(root.get(section), section, optional=True):
            granted = ""
            if section == "Audiencias":
                granted = _clean(row.get("Concedida"), "Concedida", optional=True)
                if _fold(granted) in NOT_HELD:
                    counts["not_held"] += 1
                    continue
            number = _clean(row.get(number_key), number_key, optional=True)
            match = NUMBER.fullmatch(number)
            committee = (
                match.group(2) if match and match.group(3) in published_codes(legislature) else ""
            )
            when, bad = _date_or_none(row.get("Data"), "Data")
            counts["invalid_dates"] += bad
            attendees, skipped = _attendees(row.get("Entidades"))
            counts["placeholders"] += skipped
            if not committee and not attendees:
                counts["without_parties"] += 1
                continue
            subject = " ".join(_text(row.get("Assunto"), "Assunto", optional=True).split())
            hearings.append(
                Hearing(
                    record_id=f"{prefix}:{_identifier(row.get(id_key), id_key)}",
                    kind=label,
                    number=number,
                    title=_cap(subject or f"{label} {number}".strip(), TITLE_LIMIT),
                    date=when,
                    granted=granted,
                    committee=committee,
                    attendees=attendees,
                )
            )
    if len({hearing.record_id for hearing in hearings}) != len(hearings):
        raise ParliamentImportError("Repeated hearing or audience identifier.")
    return hearings, counts


def _publication_url(publications: list[JSONObject]) -> str:
    for publication in publications:
        url = publication.get("URLDiario")
        if isinstance(url, str) and 0 < len(url) <= 2048:
            try:
                validate_source_url(url)
            except ValidationError:
                continue
            return url
    return ""


def _publication_text(publications: list[JSONObject]) -> str:
    for publication in publications:
        kind = _clean(publication.get("pubTipo"), "pubTipo", optional=True)
        number = _clean(publication.get("pubNr"), "pubNr", optional=True)
        when = _date(publication.get("pubdt"), "pubdt")
        if kind:
            text = kind + (f" n.º {number}" if number else "")
            return text + (f", de {when.strftime('%d/%m/%Y')}" if when else "")
    return ""


def published_name(value: str) -> str:
    """The elected person's name without a trailing parliamentary group/party suffix."""
    return GROUP_SUFFIX.sub("", " ".join(value.split())).strip()


def _elections(root: JSONObject, legislature: str, url: str) -> tuple[list[Election], int]:
    general = _object(root.get("AtividadesGerais"), "AtividadesGerais")
    elections: list[Election] = []
    resignations = 0
    seen: dict[str, int] = {}
    for activity in _rows(general.get("Atividades"), "Atividades", optional=True):
        if activity.get("Tipo") != "OEX":
            continue
        subject = _clean(activity.get("Assunto"), "Assunto", optional=True, limit=2000)
        body = _clean(activity.get("OrgaoExterior"), "OrgaoExterior", optional=True, limit=500)
        rows = _rows(activity.get("Eleitos"), "Eleitos", optional=True)
        if not rows:
            continue
        if RESIGNATION.search(subject):
            resignations += 1
            continue
        if not body:
            raise ParliamentImportError("External-body election without the body's name.")
        entry = _text(activity.get("DataEntrada"), "DataEntrada", optional=True, limit=20)
        digest = hashlib.sha256(f"{body}\n{entry}\n{subject}".encode()).hexdigest()[:16]
        seen[digest] = seen.get(digest, 0) + 1
        activity_id = digest if seen[digest] == 1 else f"{digest}-{seen[digest]}"
        start, _ = _date_or_none(activity.get("DataAgendamentoDebate"), "DataAgendamentoDebate")
        publications = _rows(activity.get("Publicacao"), "Publicacao", optional=True)
        source_url = _publication_url(publications) or url
        publication = _publication_text(publications)
        for index, elected in enumerate(rows, start=1):
            name = published_name(_text(elected.get("nome"), "nome", optional=True, limit=500))
            if not name:
                continue
            role = _clean(elected.get("cargo"), "cargo", optional=True)
            passage = (
                f"{name} — {role or 'cargo não indicado'}, {body}"
                f" (eleição pela Assembleia da República, {legislature_label(legislature)})."
            )
            if subject:
                passage += f" Assunto: {subject}."
            passage += (
                f" Data do debate/eleição: {start.strftime('%d/%m/%Y')}."
                if start
                else " Data da eleição não indicada na fonte."
            )
            if publication:
                passage += f" Publicação: {publication}."
            elections.append(
                Election(
                    activity_id=activity_id,
                    row=index,
                    name=name[:NAME_LIMIT],
                    role=role,
                    body=body,
                    start=start,
                    source_url=source_url,
                    reference=f"Atividades{legislature} / OEX {activity_id} / Eleitos[{index}]",
                    passage=passage,
                )
            )
    return elections, resignations


def build_snapshot(
    activities: Download, bodies: Download, *, legislature: str, as_of: date
) -> ActivitiesSnapshot:
    """Validate both files and build the complete snapshot without database access."""
    validate_file_legislature(legislature)
    validate_url(activities.url, FETCH_DATASET, legislature)
    validate_url(bodies.url, "bodies", legislature)
    root = _object(_load(activities, FETCH_DATASET), "Atividades")
    composition = _object(_load(bodies, "bodies"), "OrgaoComposicao")
    hearings, counts = _hearings(root, legislature)
    acronyms = {_fold(hearing.committee) for hearing in hearings if hearing.committee}
    committees = committee_map(acronyms, composition, root, legislature)
    elections, resignations = _elections(root, legislature, activities.url)
    return ActivitiesSnapshot(
        legislature=legislature,
        as_of=as_of,
        url=activities.url,
        committees=committees,
        hearings=tuple(hearings),
        elections=tuple(elections),
        unmapped_committees=tuple(sorted(acronyms - committees.keys())),
        skipped_placeholders=counts["placeholders"],
        skipped_not_held=counts["not_held"],
        skipped_resignations=resignations,
        skipped_without_parties=counts["without_parties"],
        invalid_dates=counts["invalid_dates"],
    )


def _transient(exc: ParliamentImportError) -> bool:
    """Network failures (timeouts, dropped connections); never validation failures."""
    return isinstance(exc.__cause__, (OSError, http.client.HTTPException))


def download_with_retries(
    dataset: str, legislature: str, *, sleep: Callable[[float], None] = time.sleep
) -> Download:
    """The official file, retrying only transient network failures a bounded number of times."""
    for attempt in range(ATTEMPTS):
        try:
            return discover_download(dataset, legislature)
        except ParliamentImportError as exc:
            if attempt == ATTEMPTS - 1 or not _transient(exc):
                raise
        sleep(BACKOFF * 2**attempt)
    raise ParliamentImportError("Official download was not discovered.")


def fetch_snapshot(*, legislature: str, as_of: date) -> ActivitiesSnapshot:
    validate_file_legislature(legislature)
    activities = download_with_retries(FETCH_DATASET, legislature)
    bodies = download_with_retries("bodies", legislature)
    return build_snapshot(activities, bodies, legislature=legislature, as_of=as_of)


def _event(hearing: Hearing, snapshot: ActivitiesSnapshot, organs: dict[str, Entity]) -> EventInput:
    parties: list[PartyInput] = []
    if hearing.committee:
        mapped = snapshot.committees.get(_fold(hearing.committee))
        if mapped is None:
            parties.append(PartyInput(EventParty.Role.HOST, hearing.committee))
        else:
            key, name = mapped
            parties.append(PartyInput(EventParty.Role.HOST, name[:NAME_LIMIT], organs.get(key)))
    parties.extend(PartyInput(EventParty.Role.ATTENDEE, name) for name in hearing.attendees)
    details: dict[str, str] = {"number": hearing.number, "type": hearing.kind}
    if hearing.granted:
        details["granted"] = hearing.granted
    return EventInput(
        record_id=hearing.record_id,
        kind=Event.Kind.HEARING,
        title=hearing.title,
        date=hearing.date,
        details=details,
        parties=tuple(parties),
    )


def _observation(election: Election, snapshot: ActivitiesSnapshot) -> ObservationInput:
    dataset = DATASETS[DATASET]
    reference = f"oex:{snapshot.legislature}:{election.activity_id}:{election.row}"
    return revised(
        ObservationInput(
            external_id=reference,
            revision="",
            identity=None,
            subject_name=election.name,
            subject_reference=reference,
            category=SourceObservation.Category.OFFICE_HOLDING,
            passage=election.passage,
            source_url=election.source_url,
            publisher=dataset.publisher,
            reference=election.reference,
            title=f"{dataset.title} — {legislature_label(snapshot.legislature)}",
            effective_start=election.start,
            object=None,
            object_name=election.body,
            kind=Relationship.Kind.PUBLIC_OFFICE,
            dataset=DATASET,
            role=election.role,
            role_class=role_class(election.role) if election.role else "",
        )
    )


def apply_snapshot(snapshot: ActivitiesSnapshot) -> dict[str, int]:
    """Atomic: the legislature's complete hearing events and external-body elections."""
    with import_transaction():
        keys = [key for key, _ in snapshot.committees.values()]
        organs = {
            identity.external_id: identity.entity
            for identity in SourceIdentity.objects.select_related("entity").filter(
                source=IdentityScheme.PARLIAMENT, external_id__in=keys
            )
        }
        hosts_missing = sum(
            1
            for hearing in snapshot.hearings
            if hearing.committee
            and snapshot.committees.get(_fold(hearing.committee), ("", ""))[0] not in organs
        )
        events = sync_events(
            dataset=DATASET,
            scope=f"hearings:{snapshot.legislature}",
            events=(_event(hearing, snapshot, organs) for hearing in snapshot.hearings),
            as_of=snapshot.as_of,
        )
        scopes: dict[str, list[ObservationInput]] = {}
        for election in snapshot.elections:
            scope = f"oex:{snapshot.legislature}:{election.activity_id}"
            scopes.setdefault(scope, []).append(_observation(election, snapshot))
        candidates = sync_scoped_snapshot(
            source=EnrichmentSource.PARLIAMENT,
            prefix=f"oex:{snapshot.legislature}:",
            snapshots={scope: tuple(items) for scope, items in scopes.items()},
            as_of=snapshot.as_of,
        )
    result = {f"events_{key}": value for key, value in events.items()}
    result.update({f"candidates_{key}": value for key, value in candidates.items()})
    result["hosts_unresolved"] = hosts_missing
    return result
