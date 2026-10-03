"""Complete, independently scoped AR mandate histories with revocable publication."""

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from django.utils import timezone

from .enrichment import sync_biography_roles, sync_observations
from .identity import OfficeContext, ar_institution, ar_person
from .models import (
    Evidence,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    ParliamentStatusInterval,
    Relationship,
    Source,
    SourceObservation,
    TemporalStatus,
    import_transaction,
    invalidate_relationships,
)
from .parliament_bodies import legislature_term
from .parliament_fetch import (
    CATALOGUES,
    ParliamentImportError,
    canonical_legislature,
    discover_download,
    validate_url,
)
from .parliament_parse import ParliamentSnapshot, canonical_json, parse_snapshot
from .services import publish_imported


@dataclass(frozen=True)
class ImportResult:
    serving: int  # Complete effective periods, not the roster on one reference day.
    created_members: int
    created_records: int
    ceased_members: int


def fetch_snapshot(*, legislature: str, as_of: date) -> ParliamentSnapshot:
    legislature = canonical_legislature(legislature)
    roster = discover_download("roster", legislature)
    # Biography exports describe the current cadastro, even in historic folders. Their
    # absence or access failure cannot invalidate a complete official mandate history.
    try:
        biography = discover_download("biography", legislature)
    except ParliamentImportError:
        biography = None
    return parse_snapshot(roster, biography, legislature=legislature, as_of=as_of)


def roster_passage(roster: dict, *, start: date, end: date | None) -> str:
    lines = [
        f"Nome parlamentar: {roster.get('DepNomeParlamentar') or roster['DepNomeCompleto']}.",
        f"Nome completo: {roster['DepNomeCompleto']}.",
        f"Identificador AR (DepCadId): {roster['DepCadId']}; DepId: {', '.join(roster['DepIds'])}.",
        f"Período efetivo contínuo: {start.isoformat()} — {end.isoformat() if end else 'sem fim publicado'}.",
    ]
    for status in roster["DepSituacao"]:
        lines.append(
            f"DepSituacao / DepId={status['DepId']}: {status['sioDes']}; "
            f"início={status['sioDtInicio'] or 'não publicado'}; "
            f"fim={status['sioDtFim'] or 'não publicado'}."
        )
    return "\n".join(lines)


def _withdraw(record: ParliamentRecord) -> None:
    invalidate_relationships(Relationship.objects.filter(pk=record.relationship_id))
    if record.evidence.is_public:
        record.evidence.is_public = False
        record.evidence.save()
    record.relationship.refresh_from_db()


def _publish(record: ParliamentRecord) -> None:
    evidence = record.evidence
    if not evidence.source.is_public:
        evidence.source.is_public = True
        evidence.source.save()
    if not evidence.is_public:
        evidence.is_public = True
        evidence.save()
    publish_imported(record.relationship)


@import_transaction()
def apply_snapshot(snapshot: ParliamentSnapshot) -> ImportResult:
    """Absence/revisions affect only this complete legislature snapshot."""
    if snapshot.legislature_start is None or len({m.cadastro_id for m in snapshot.members}) != len(
        snapshot.members
    ):
        raise ParliamentImportError("Cannot apply an incomplete or duplicate snapshot.")
    validate_url(snapshot.roster_url, "roster", snapshot.legislature)
    if snapshot.biography_url:
        validate_url(snapshot.biography_url, "biography", snapshot.legislature)
    state = (
        ParliamentImportState.objects.select_for_update().filter(key=snapshot.legislature).first()
    )
    if state is not None and snapshot.as_of < state.as_of:
        raise ParliamentImportError("Cannot replace a newer import with an older as-of snapshot.")
    institution = ar_institution()
    retrieved_at = timezone.now()
    roster_source = None
    if state is None:
        roster_source = Source.objects.create(
            title="Assembleia da República — Informação de Base",
            publisher="Assembleia da República",
            url=snapshot.roster_url,
            retrieved_at=retrieved_at,
            is_public=True,
            dataset="ar_informacao_base",
        )
        biography_source = Source.objects.create(
            title="Assembleia da República — Registo Biográfico",
            publisher="Assembleia da República",
            url=snapshot.biography_url or CATALOGUES["biography"],
            retrieved_at=retrieved_at,
            is_public=True,
            dataset="ar_registo_biografico",
        )
        state = ParliamentImportState.objects.create(
            key=snapshot.legislature,
            institution=institution,
            roster_source=roster_source,
            biography_source=biography_source,
            as_of=snapshot.as_of,
        )
    term = legislature_term(
        snapshot.legislature, snapshot.legislature_start, snapshot.legislature_end
    )
    created_members = created_records = ceased_members = 0
    observed_record_ids: set[int] = set()
    ParliamentStatusInterval.objects.filter(legislature=snapshot.legislature).delete()
    for observed in snapshot.members:
        roster = cast(dict[str, Any], observed.data["roster"])
        statuses = roster["DepSituacao"]
        suspensions = [
            date.fromisoformat(s["sioDtInicio"])
            for s in statuses
            if s["sioDes"].startswith("Suspenso") and s["sioDtInicio"]
        ]
        entity = ar_person(
            observed.cadastro_id,
            observed.name,
            aliases=[roster["DepNomeParlamentar"]],
            offices=[
                OfficeContext(institution, first, last) for _, first, last in observed.periods
            ],
            suspensions=suspensions,
        )
        member, created = ParliamentMember.objects.get_or_create(
            cadastro_id=observed.cadastro_id,
            defaults={"entity": entity, "as_of": snapshot.as_of},
        )
        created_members += int(created)
        intervals = {}
        for status in statuses:
            first = date.fromisoformat(status["sioDtInicio"]) if status["sioDtInicio"] else None
            last = date.fromisoformat(status["sioDtFim"]) if status["sioDtFim"] else None
            key = (status["sioDes"], first, last)
            intervals[key] = ParliamentStatusInterval(
                cadastro_id=observed.cadastro_id,
                entity=entity,
                legislature=snapshot.legislature,
                status=status["sioDes"],
                start=first,
                end=last,
            )
        ParliamentStatusInterval.objects.bulk_create(intervals.values())
        biography_record = None
        for _, first, last in observed.periods:
            status = (
                TemporalStatus.ENDED
                if last is not None or snapshot.legislature_end is not None
                else TemporalStatus.CURRENT
            )
            passage = roster_passage(roster, start=first, end=last)
            fingerprint = hashlib.sha256(
                canonical_json(
                    {
                        "roster": roster,
                        "start": first.isoformat(),
                        "end": last.isoformat() if last else None,
                        "temporal_status": status,
                        "url": snapshot.roster_url,
                    }
                ).encode()
            ).hexdigest()
            record = (
                ParliamentRecord.objects.select_related("relationship", "evidence__source")
                .filter(
                    member=member,
                    legislature=snapshot.legislature,
                    period_start=first,
                )
                .first()
            )
            existing = record is not None
            changed = record is None or record.fingerprint != fingerprint or not record.is_current
            if record is None:
                if roster_source is None:
                    roster_source = Source.objects.create(
                        title="Assembleia da República — Informação de Base",
                        publisher="Assembleia da República",
                        url=snapshot.roster_url,
                        retrieved_at=retrieved_at,
                        is_public=True,
                        dataset="ar_informacao_base",
                    )
                # Adopt a previously bodies-owned plenary claim, then detach its old owner.
                old = (
                    SourceObservation.objects.filter(
                        source="parliament",
                        scope=f"bodies:{snapshot.legislature}",
                        relationship__subject=entity,
                        relationship__object=institution,
                        relationship__kind=Relationship.Kind.PUBLIC_OFFICE,
                        relationship__start_date=first,
                    )
                    .select_related("relationship")
                    .first()
                )
                relationship = (
                    old.relationship
                    if old is not None
                    else Relationship.objects.create(
                        subject=entity,
                        object=institution,
                        kind=Relationship.Kind.PUBLIC_OFFICE,
                        description=f"Deputado/a à Assembleia da República — {term.label}.",
                        start_date=first,
                        end_date=last,
                        role="Deputado/a",
                        role_class=Relationship.RoleClass.MEMBER,
                        term=term,
                        temporal_status=status,
                    )
                )
                if old is not None:
                    old.relationship = None
                    old.is_current = False
                    old.save(update_fields=["relationship", "is_current"])
                evidence = Evidence.objects.create(
                    relationship=relationship,
                    source=roster_source,
                    excerpt=passage,
                    page_reference=f"Deputados / DepCadId={observed.cadastro_id} / DepSituacao; {snapshot.legislature}",
                )
                if old is not None and relationship is not None:
                    # Adding evidence invalidates publication in the database; do not
                    # save the adopted relationship with its cached published status.
                    relationship.refresh_from_db()
                record = ParliamentRecord.objects.create(
                    member=member,
                    fingerprint=fingerprint,
                    legislature=snapshot.legislature,
                    period_start=first,
                    as_of=snapshot.as_of,
                    retrieved_at=retrieved_at,
                    data=observed.data,
                    roster_url=snapshot.roster_url,
                    biography_url=snapshot.biography_url,
                    relationship=relationship,
                    evidence=evidence,
                )
                created_records += 1
            if changed:
                if existing:
                    _withdraw(record)
                if roster_source is None:
                    roster_source = Source.objects.create(
                        title="Assembleia da República — Informação de Base",
                        publisher="Assembleia da República",
                        url=snapshot.roster_url,
                        retrieved_at=retrieved_at,
                        is_public=True,
                        dataset="ar_informacao_base",
                    )
                relationship = record.relationship
                relationship.start_date, relationship.end_date = first, last
                relationship.role, relationship.role_class = (
                    "Deputado/a",
                    Relationship.RoleClass.MEMBER,
                )
                relationship.term, relationship.temporal_status = term, status
                relationship.save()
                record.evidence.excerpt = passage
                record.evidence.source = roster_source
                record.evidence.save()
                record.fingerprint = fingerprint
                record.is_current = True
                record.as_of, record.retrieved_at = snapshot.as_of, retrieved_at
                record.roster_url, record.biography_url = (
                    snapshot.roster_url,
                    snapshot.biography_url,
                )
                record.data = observed.data
                record.save()
                _publish(record)
            elif record.data != observed.data or record.biography_url != snapshot.biography_url:
                record.data, record.biography_url = observed.data, snapshot.biography_url
                record.save(update_fields=["data", "biography_url"])
            observed_record_ids.add(record.pk)
            biography_record = record
        if biography_record is not None and snapshot.biography_url:
            sync_biography_roles(biography_record, as_of=snapshot.as_of)
        member.as_of = max(member.as_of, snapshot.as_of)
        member.is_current = member.records.filter(is_current=True).exists()
        member.save(update_fields=["as_of", "is_current"])
    for record in (
        ParliamentRecord.objects.filter(
            legislature=snapshot.legislature,
            is_current=True,
        )
        .exclude(pk__in=observed_record_ids)
        .select_related("evidence", "member")
    ):
        _withdraw(record)
        record.is_current = False
        record.save(update_fields=["is_current"])
        ceased_members += 1
        member = record.member
        member.is_current = member.records.filter(is_current=True).exists()
        member.save(update_fields=["is_current"])
        if snapshot.biography_url and member.cadastro_id not in {
            m.cadastro_id for m in snapshot.members
        }:
            sync_observations(
                source="parliament",
                scope=f"member:{member.cadastro_id}:{snapshot.legislature}",
                observations=(),
                as_of=snapshot.as_of,
            )
    state.as_of = snapshot.as_of
    state.save(update_fields=["as_of"])
    return ImportResult(snapshot.mandate_count, created_members, created_records, ceased_members)
