"""Atomic Parliament imports that auto-publish mandates. Revisions never overwrite prose."""

from dataclasses import dataclass
from datetime import date

from django.utils import timezone

from .enrichment import sync_biography_roles, sync_observations
from .identity import ar_institution, ar_person
from .models import (
    Evidence,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    Relationship,
    Source,
    Term,
    import_transaction,
    invalidate_relationships,
)
from .parliament_bodies import legislature_term, temporal_status
from .parliament_fetch import CATALOGUES, ParliamentImportError, discover_download, validate_url
from .parliament_parse import ParliamentSnapshot, parse_snapshot
from .services import publish_imported


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


def _date(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d/%m/%Y") if value else ""


def roster_passage(roster: object) -> str:
    """Readable Portuguese passage built only from the retained roster allowlist."""
    if not isinstance(roster, dict):
        raise ParliamentImportError("Retained roster projection is not an object.")
    lines = [
        f"Nome parlamentar: {roster.get('DepNomeParlamentar') or roster.get('DepNomeCompleto')}.",
        f"Nome completo: {roster.get('DepNomeCompleto')}.",
        f"Círculo eleitoral: {roster.get('DepCPDes')}.",
    ]
    status = roster.get("DepSituacao") or {}
    mandate = f"Situação do mandato: {status.get('sioDes')}"
    if status.get("sioDtInicio"):
        mandate += f", desde {_date(status['sioDtInicio'])}"
    if status.get("sioDtFim"):
        mandate += f" até {_date(status['sioDtFim'])}"
    lines.append(mandate + ".")
    for group in roster.get("DepGP") or []:
        period = " a ".join(
            _date(group.get(key)) for key in ("gpDtInicio", "gpDtFim") if group.get(key)
        )
        lines.append(
            f"Grupo parlamentar: {group.get('gpSigla')}" + (f" ({period})." if period else ".")
        )
    lines.append(f"Identificador AR (DepCadId): {roster.get('DepCadId')}.")
    return "\n".join(lines)


def _withdraw(record: ParliamentRecord) -> None:
    invalidate_relationships(Relationship.objects.filter(pk=record.relationship_id))
    evidence = record.evidence
    if evidence.is_public:
        evidence.is_public = False
        evidence.save()


def _publish(record: ParliamentRecord) -> None:
    """Official mandates publish automatically; editors withdraw them afterwards."""
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
        institution = ar_institution()
        roster_source = Source.objects.create(
            title="Assembleia da República — Informação de Base",
            publisher="Assembleia da República",
            url=CATALOGUES["roster"],
            retrieved_at=retrieved_at,
            is_public=True,
            dataset="ar_informacao_base",
        )
        biography_source = Source.objects.create(
            title="Assembleia da República — Registo Biográfico",
            publisher="Assembleia da República",
            url=CATALOGUES["biography"],
            retrieved_at=retrieved_at,
            is_public=True,
            dataset="ar_registo_biografico",
        )
        state = ParliamentImportState.objects.create(
            institution=institution,
            roster_source=roster_source,
            biography_source=biography_source,
            as_of=snapshot.as_of,
        )
    # Only new mandate relationships get the structured fields; published ones stay as they are.
    term = (
        legislature_term(snapshot.legislature, snapshot.legislature_start, snapshot.legislature_end)
        if snapshot.legislature_start is not None
        else Term.objects.filter(kind=Term.Kind.LEGISLATURE, code=snapshot.legislature).first()
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
            entity = ar_person(observed.cadastro_id, observed.name)
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
                        is_public=True,
                        dataset="ar_informacao_base",
                    )
                relationship = Relationship.objects.create(
                    subject=member.entity,
                    object=state.institution,
                    kind=Relationship.Kind.PUBLIC_OFFICE,
                    description=f"Deputado/a à Assembleia da República — {snapshot.legislature} Legislatura.",
                    start_date=observed.start_date,
                    end_date=observed.end_date,
                    role="Deputado/a",
                    role_class=Relationship.RoleClass.MEMBER,
                    term=term,
                    temporal_status=temporal_status(
                        observed.start_date,
                        observed.end_date,
                        snapshot.legislature_end,
                        snapshot.as_of,
                    ),
                )
                evidence = Evidence.objects.create(
                    relationship=relationship,
                    source=roster_source,
                    excerpt=roster_passage(observed.data["roster"]),
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
                _publish(record)
            else:
                # A return drops old approval, then republishes unless an editor withdrew it.
                _withdraw(record)
                _publish(record)
            member.current_record = record
        else:
            record = previous
            if not member.is_current:
                _withdraw(record)
                _publish(record)
        if (
            not member.is_current
            or member.as_of != snapshot.as_of
            or member.current_record != previous
        ):
            member.is_current = True
            member.as_of = snapshot.as_of
            member.save()
        sync_biography_roles(record, as_of=snapshot.as_of)
    for member in (
        ParliamentMember.objects.filter(is_current=True)
        .exclude(cadastro_id__in=current_ids)
        .select_related("current_record__evidence")
    ):
        if member.current_record is not None:
            _withdraw(member.current_record)
        sync_observations(
            source="parliament",
            scope=f"member:{member.cadastro_id}",
            observations=(),
            as_of=snapshot.as_of,
        )
        member.is_current = False
        member.as_of = snapshot.as_of
        member.save()
        ceased_members += 1
    if state.as_of != snapshot.as_of:
        state.as_of = snapshot.as_of
        state.save(update_fields=["as_of"])
    return ImportResult(len(snapshot.members), created_members, created_records, ceased_members)
