"""Atomic draft-only Parliament imports. Source revisions never overwrite editorial prose."""

import uuid
from dataclasses import dataclass
from datetime import date

from django.utils import timezone

from .models import (
    Entity,
    Evidence,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    Relationship,
    Source,
    editorial_transaction,
    invalidate_relationships,
)
from .parliament_fetch import CATALOGUES, ParliamentImportError, discover_download, validate_url
from .parliament_parse import ParliamentSnapshot, canonical_json, parse_snapshot


@dataclass(frozen=True)
class ImportResult:
    serving: int
    created_members: int
    created_records: int
    ceased_members: int


def fetch_snapshot(
    *, legislature: str, as_of: date, expected_count: int = 230
) -> ParliamentSnapshot:
    return parse_snapshot(
        discover_download("roster", legislature),
        discover_download("biography", legislature),
        legislature=legislature,
        as_of=as_of,
        expected_count=expected_count,
    )


def _withdraw(record: ParliamentRecord) -> None:
    invalidate_relationships(Relationship.objects.filter(pk=record.relationship_id))
    evidence = record.evidence
    if evidence.is_public:
        evidence.is_public = False
        evidence.save()


@editorial_transaction()
def apply_snapshot(snapshot: ParliamentSnapshot) -> ImportResult:
    """Apply one previously validated complete snapshot under the editorial write lock."""
    if (
        len(snapshot.members) != snapshot.expected_count
        or not 1 <= snapshot.expected_count <= 1000
        or len({member.cadastro_id for member in snapshot.members}) != len(snapshot.members)
    ):
        raise ParliamentImportError("Cannot apply an incomplete or duplicate snapshot.")
    validate_url(snapshot.roster_url, "roster", snapshot.legislature)
    validate_url(snapshot.biography_url, "biography", snapshot.legislature)
    state = ParliamentImportState.objects.select_for_update().filter(key="assembly").first()
    if state is not None and snapshot.as_of < state.as_of:
        raise ParliamentImportError("Cannot replace a newer import with an older as-of snapshot.")
    retrieved_at = timezone.now()
    # Keep state sources historical; allocate a batch source only for new revisions.
    roster_source = None
    if state is None:
        institution = Entity.objects.create(
            name="Assembleia da República",
            slug=f"assembleia-da-republica-{uuid.uuid4().hex}",
            kind=Entity.Kind.ORGANISATION,
        )
        roster_source = Source.objects.create(
            title="Assembleia da República — Informação de Base",
            publisher="Assembleia da República",
            url=CATALOGUES["roster"],
            retrieved_at=retrieved_at,
        )
        biography_source = Source.objects.create(
            title="Assembleia da República — Registo Biográfico",
            publisher="Assembleia da República",
            url=CATALOGUES["biography"],
            retrieved_at=retrieved_at,
        )
        state = ParliamentImportState.objects.create(
            institution=institution,
            roster_source=roster_source,
            biography_source=biography_source,
            as_of=snapshot.as_of,
        )
    created_members = created_records = ceased_members = 0
    current_ids: set[str] = set()
    for observed in snapshot.members:
        current_ids.add(observed.cadastro_id)
        member = (
            ParliamentMember.objects.select_related("current_record__evidence")
            .filter(cadastro_id=observed.cadastro_id)
            .first()
        )
        if member is None:
            entity = Entity.objects.create(
                name=observed.name,
                slug=f"ar-deputado-{observed.cadastro_id}-{uuid.uuid4().hex}",
                kind=Entity.Kind.PERSON,
            )
            member = ParliamentMember.objects.create(
                cadastro_id=observed.cadastro_id, entity=entity, as_of=snapshot.as_of
            )
            created_members += 1
        previous = member.current_record
        fingerprint = observed.fingerprint
        if previous is None or previous.fingerprint != fingerprint:
            if previous is not None:
                _withdraw(previous)
            record = ParliamentRecord.objects.filter(member=member, fingerprint=fingerprint).first()
            if record is None:
                if roster_source is None:
                    roster_source = Source.objects.create(
                        title="Assembleia da República — Informação de Base",
                        publisher="Assembleia da República",
                        url=CATALOGUES["roster"],
                        retrieved_at=retrieved_at,
                    )
                relationship = Relationship.objects.create(
                    subject=member.entity,
                    object=state.institution,
                    kind=Relationship.Kind.PUBLIC_OFFICE,
                    description=f"Deputado/a à Assembleia da República — {snapshot.legislature} Legislatura.",
                    start_date=observed.start_date,
                    end_date=observed.end_date,
                )
                evidence = Evidence.objects.create(
                    relationship=relationship,
                    source=roster_source,
                    excerpt=canonical_json(observed.data["roster"]),
                    page_reference=f"Deputados / DepCadId={observed.cadastro_id}; {snapshot.legislature}",
                )
                record = ParliamentRecord.objects.create(
                    member=member,
                    fingerprint=fingerprint,
                    legislature=snapshot.legislature,
                    as_of=snapshot.as_of,
                    retrieved_at=retrieved_at,
                    data=observed.data,
                    roster_url=snapshot.roster_url,
                    biography_url=snapshot.biography_url,
                    relationship=relationship,
                    evidence=evidence,
                )
                created_records += 1
            else:
                # A return to an earlier observation is not permission to resurrect review.
                _withdraw(record)
            member.current_record = record
        elif not member.is_current:
            _withdraw(previous)
        if (
            not member.is_current
            or member.as_of != snapshot.as_of
            or member.current_record != previous
        ):
            member.is_current = True
            member.as_of = snapshot.as_of
            member.save()
    for member in (
        ParliamentMember.objects.filter(is_current=True)
        .exclude(cadastro_id__in=current_ids)
        .select_related("current_record__evidence")
    ):
        if member.current_record is not None:
            _withdraw(member.current_record)
        member.is_current = False
        member.as_of = snapshot.as_of
        member.save()
        ceased_members += 1
    if state.as_of != snapshot.as_of:
        state.as_of = snapshot.as_of
        state.save(update_fields=["as_of"])
    return ImportResult(len(snapshot.members), created_members, created_records, ceased_members)
