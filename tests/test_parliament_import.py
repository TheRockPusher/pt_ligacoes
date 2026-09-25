import json
from datetime import UTC, date, datetime
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.db import DatabaseError

from ligacoes.core.models import (
    Entity,
    Evidence,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    Relationship,
    ReviewEvent,
    Source,
)
from ligacoes.core.parliament_fetch import (
    Download,
    ParliamentImportError,
    _PinnedHTTPSConnection,
    discover_download,
    fetch_url,
    validate_url,
)
from ligacoes.core.parliament_import import apply_snapshot
from ligacoes.core.parliament_parse import JSONObject, JSONValue, ParliamentSnapshot, parse_snapshot
from ligacoes.core.services import publish_relationship
from ligacoes.public.selectors import public_evidence

DAY = date(2025, 7, 1)


def download_url(dataset: str, opaque_path: str = "fictional") -> str:
    prefix = "InformacaoBase" if dataset == "roster" else "RegistoBiografico"
    return (
        "https://app.parlamento.pt/webutils/docs/doc.txt"
        f"?path={opaque_path}&fich={prefix}XVII_json.txt&Inline=true"
    )


def status_row(
    status: str = "Efetivo", start: str | None = "2025-06-03", end: str | None = None
) -> JSONObject:
    return {"sioDes": status, "sioDtInicio": start, "sioDtFim": end}


def roster_row(cadastro: int = 101, *, status: JSONObject | None = None) -> JSONObject:
    return {
        "DepCadId": float(cadastro),
        "DepId": float(cadastro + 1000),
        "DepNomeCompleto": f"Pessoa Fictícia {cadastro}",
        "DepNomeParlamentar": f"Fictícia {cadastro}",
        "DepCPDes": "Círculo Fictício",
        "LegDes": "XVII",
        "DepGP": [{"gpSigla": "FIC", "gpDtInicio": "2025-06-03", "gpDtFim": None}],
        "DepSituacao": [status or status_row()],
        "Videos": "IGNORED_UNNECESSARY_FIELD",
    }


def biography_row(cadastro: int = 101, profession: str = "Profissão fictícia") -> JSONObject:
    return {
        "CadId": float(cadastro),
        "CadNomeCompleto": "Different fictional display name, never an identity key",
        "CadProfissao": profession,
        "CadHabilitacoes": [{"HabDes": "Curso fictício", "HabEstado": "C", "HabId": 8}],
        "CadCargosFuncoes": [
            {
                "FunId": 31.0,
                "FunAntiga": "S",
                "FunDes": "  Cargo fictício — 1999?\nsem datas certas.  ",
            }
        ],
        "CadDtNascimento": "PRIVATE_DATE_CANARY",
        "CadSexo": "PRIVATE_SEX_CANARY",
    }


def snapshot(
    rows: list[JSONObject] | None = None,
    biographies: list[JSONObject] | None = None,
    *,
    expected_count: int = 1,
    as_of: date = DAY,
    opaque_path: str = "fictional",
) -> ParliamentSnapshot:
    root: JSONObject = {
        "DetalheLegislatura": {"sigla": "XVII", "dtini": "2025-06-03", "dtfim": None},
        "Deputados": list[JSONValue](rows if rows is not None else [roster_row()]),
    }
    bios = biographies if biographies is not None else [biography_row()]
    return parse_snapshot(
        Download(json.dumps(root).encode(), download_url("roster", opaque_path)),
        Download(json.dumps(bios).encode(), download_url("biography", opaque_path)),
        legislature="XVII",
        as_of=as_of,
        expected_count=expected_count,
    )


def publish_record(record: ParliamentRecord, reviewer) -> Relationship:
    relationship = record.relationship
    for entity in (relationship.subject, relationship.object):
        entity.is_public = True
        entity.save()
    evidence = record.evidence
    evidence.source.is_public = True
    evidence.source.save()
    evidence.is_public = True
    evidence.save()
    return publish_relationship(relationship, reviewer)


def test_selects_actual_serving_intervals_and_joins_by_cadastro():
    result = snapshot(
        [
            roster_row(101, status=status_row("Suplente")),
            roster_row(102, status=status_row("Efetivo Definitivo")),
            roster_row(103, status=status_row("Efetivo Temporário", "2025-07-01")),
            roster_row(104, status=status_row(end="2025-06-30")),
            roster_row(105, status=status_row(start="2025-07-02")),
        ],
        [biography_row(103, "Biografia fictícia B"), biography_row(102, "Biografia fictícia A")],
        expected_count=2,
    )
    assert [(m.cadastro_id, m.start_date) for m in result.members] == [
        ("102", date(2025, 6, 3)),
        ("103", DAY),
    ]
    assert result.members[0].data["biography"] == {
        "CadProfissao": "Biografia fictícia A",
        "CadHabilitacoes": [{"HabDes": "Curso fictício", "HabEstado": "C"}],
        "CadCargosFuncoes": [
            {
                "FunId": "31",
                "FunAntiga": "S",
                "FunDes": "  Cargo fictício — 1999?\nsem datas certas.  ",
            }
        ],
    }
    assert "PRIVATE_" not in json.dumps([m.data for m in result.members])


def test_inclusive_supplied_end_date_and_historical_anomaly():
    row = roster_row()
    row["DepSituacao"] = [
        status_row(start="2025-06-20", end="2025-06-10"),
        status_row(start=None, end="2025-06-01"),
        status_row(end="2025-07-01"),
    ]
    result = snapshot([row])
    assert result.members[0].end_date == DAY
    with pytest.raises(ParliamentImportError, match="Serving count"):
        snapshot([row], as_of=date(2025, 7, 2))


@pytest.mark.parametrize(
    "statuses",
    [
        [status_row(), status_row("Suspenso(Eleito)")],
        [status_row(start=None)],
        [status_row(start="2025-08-01", end="2025-07-02")],
    ],
)
def test_relevant_interval_ambiguity_fails(statuses: list[JSONObject]):
    row = roster_row()
    row["DepSituacao"] = list[JSONValue](statuses)
    with pytest.raises(ParliamentImportError):
        snapshot([row])


@pytest.mark.django_db
@pytest.mark.parametrize("problem", ["missing", "ambiguous", "duplicate_roster", "incomplete"])
def test_invalid_complete_snapshot_never_writes(problem: str):
    rows = [roster_row()]
    biographies = [biography_row()]
    expected = 1
    if problem == "missing":
        biographies = [biography_row(999)]
    elif problem == "ambiguous":
        biographies.append(biography_row())
    elif problem == "duplicate_roster":
        rows.append(roster_row())
    else:
        expected = 2
    with pytest.raises(ParliamentImportError):
        apply_snapshot(snapshot(rows, biographies, expected_count=expected))
    assert not Entity.objects.exists()
    assert not Source.objects.exists()
    assert not ParliamentImportState.objects.exists()


@pytest.mark.django_db
def test_draft_only_import_does_not_merge_a_namesake():
    namesake = Entity.objects.create(
        name="Pessoa Fictícia 101", slug="fictional-namesake", kind="person"
    )
    result = apply_snapshot(snapshot())
    member = ParliamentMember.objects.get(cadastro_id="101")
    assert member.entity_id != namesake.pk
    assert (result.created_members, result.created_records) == (1, 1)
    assert Entity.objects.count() == 3
    assert Source.objects.count() == 2
    assert not Entity.objects.filter(is_public=True).exists()
    assert not Source.objects.filter(is_public=True).exists()
    assert not Evidence.objects.filter(is_public=True).exists()
    relationship = Relationship.objects.get()
    assert (relationship.kind, relationship.status, relationship.start_date) == (
        "public_office",
        "draft",
        date(2025, 6, 3),
    )
    assert relationship.reviewed_at is None
    assert not ReviewEvent.objects.exists()


@pytest.mark.django_db
def test_identical_rerun_preserves_review_and_editorial_text(reviewer):
    apply_snapshot(snapshot())
    record = ParliamentRecord.objects.get()
    entity = record.member.entity
    entity.name = "Nome editorial fictício corrigido"
    entity.save()
    relation = record.relationship
    relation.description = "Texto editorial fictício revisto."
    relation.save()
    relation = publish_record(record, reviewer)
    review_time = relation.reviewed_at
    retrieved = Source.objects.get(pk=record.evidence.source_id).retrieved_at
    result = apply_snapshot(snapshot(as_of=date(2025, 7, 2), opaque_path="rotated-fictional-path"))
    relation.refresh_from_db()
    entity.refresh_from_db()
    assert (result.created_members, result.created_records, result.ceased_members) == (0, 0, 0)
    assert relation.status == "published"
    assert relation.reviewed_at == review_time
    assert relation.description == "Texto editorial fictício revisto."
    assert entity.name == "Nome editorial fictício corrigido"
    assert Source.objects.get(pk=record.evidence.source_id).retrieved_at == retrieved
    assert ParliamentRecord.objects.count() == 1
    assert ReviewEvent.objects.filter(action="invalidate").count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("observation", ["changed", "new"])
def test_later_observation_preserves_public_consultation_provenance(reviewer, observation):
    time_a = datetime(2025, 7, 1, 9, tzinfo=UTC)
    time_b = datetime(2025, 7, 2, 10, tzinfo=UTC)
    time_c = datetime(2025, 7, 3, 11, tzinfo=UTC)
    rows = [roster_row(101), roster_row(102)]
    biographies = [biography_row(101), biography_row(102)]
    with patch("ligacoes.core.parliament_import.timezone.now", return_value=time_a):
        apply_snapshot(snapshot(rows, biographies, expected_count=2))
    previous = ParliamentRecord.objects.get(member__cadastro_id="101")
    unchanged = ParliamentRecord.objects.get(member__cadastro_id="102")
    publish_record(previous, reviewer)
    unchanged_relationship = publish_record(unchanged, reviewer)
    unchanged_review = unchanged_relationship.reviewed_at
    historical_sources = dict(Source.objects.values_list("pk", "retrieved_at"))
    old_source_id = public_evidence().get(pk=previous.evidence_id).source_id
    assert historical_sources[old_source_id] == time_a

    if observation == "changed":
        biographies[0] = biography_row(101, profession="Outra profissão fictícia")
        target_id = "101"
    else:
        rows.append(roster_row(103))
        biographies.append(biography_row(103))
        target_id = "103"
    later_snapshot = snapshot(rows, biographies, expected_count=len(rows), as_of=time_b.date())
    with patch("ligacoes.core.parliament_import.timezone.now", return_value=time_b):
        apply_snapshot(later_snapshot)
    current = ParliamentMember.objects.get(cadastro_id=target_id).current_record
    assert current is not None
    assert current.pk != previous.pk
    assert current.evidence.source_id != old_source_id
    assert current.retrieved_at == time_b
    assert current.evidence.source.retrieved_at == time_b
    assert not current.evidence.source.is_public
    assert not public_evidence().filter(pk=current.evidence_id).exists()
    previous.evidence.refresh_from_db()
    assert previous.evidence.source_id == old_source_id
    assert previous.evidence.source.retrieved_at == time_a
    assert (
        dict(Source.objects.filter(pk__in=historical_sources).values_list("pk", "retrieved_at"))
        == historical_sources
    )
    unchanged_relationship.refresh_from_db()
    assert unchanged_relationship.reviewed_at == unchanged_review
    assert public_evidence().get(pk=unchanged.evidence_id).source.retrieved_at == time_a

    relationship = publish_record(current, reviewer)
    reviewed_at = relationship.reviewed_at
    public_source = public_evidence().get(pk=current.evidence_id).source
    assert public_source.retrieved_at == time_b
    source_count = Source.objects.count()
    review_count = ReviewEvent.objects.count()
    with patch("ligacoes.core.parliament_import.timezone.now", return_value=time_c):
        result = apply_snapshot(
            snapshot(rows, biographies, expected_count=len(rows), as_of=time_c.date())
        )
    assert result.created_records == 0
    assert Source.objects.count() == source_count
    assert ReviewEvent.objects.count() == review_count
    relationship.refresh_from_db()
    assert relationship.reviewed_at == reviewed_at
    rerun_evidence = public_evidence().get(pk=current.evidence_id)
    assert rerun_evidence.source_id == public_source.pk
    assert rerun_evidence.source.retrieved_at == time_b
    assert public_evidence().get(pk=unchanged.evidence_id).source.retrieved_at == time_a


@pytest.mark.django_db
def test_changed_source_withdraws_previous_review_without_overwriting_editorial_claim(reviewer):
    apply_snapshot(snapshot())
    previous = ParliamentRecord.objects.get()
    relationship = previous.relationship
    relationship.description = "Anotação editorial fictícia a conservar."
    relationship.save()
    publish_record(previous, reviewer)
    result = apply_snapshot(
        snapshot(biographies=[biography_row(profession="Nova profissão fictícia")])
    )
    previous.refresh_from_db()
    relationship.refresh_from_db()
    previous.evidence.refresh_from_db()
    member = ParliamentMember.objects.get(cadastro_id="101")
    assert result.created_records == 1
    assert member.current_record_id != previous.pk
    assert relationship.status == "draft"
    assert relationship.description == "Anotação editorial fictícia a conservar."
    assert not previous.evidence.is_public
    assert ParliamentRecord.objects.count() == 2
    assert not Relationship.objects.filter(status="published").exists()
    assert ReviewEvent.objects.filter(action="invalidate").count() == 1
    assert previous.data["biography"]["CadProfissao"] == "Profissão fictícia"


@pytest.mark.django_db
def test_ceased_member_keeps_history_without_an_invented_end_date(reviewer):
    apply_snapshot(snapshot())
    old = ParliamentRecord.objects.get()
    publish_record(old, reviewer)
    result = apply_snapshot(
        snapshot(
            [roster_row(101, status=status_row("Suspenso(Eleito)")), roster_row(102)],
            [biography_row(102)],
            as_of=date(2025, 7, 2),
        )
    )
    member = ParliamentMember.objects.get(cadastro_id="101")
    old.relationship.refresh_from_db()
    old.evidence.refresh_from_db()
    assert result.ceased_members == 1
    assert not member.is_current
    assert member.as_of == date(2025, 7, 2)
    assert member.current_record_id == old.pk
    assert old.relationship.end_date is None
    assert old.relationship.status == "draft"
    assert not old.evidence.is_public
    assert not Relationship.objects.filter(status="published").exists()
    apply_snapshot(snapshot(as_of=date(2025, 7, 3)))
    member.refresh_from_db()
    assert member.is_current
    assert ParliamentRecord.objects.filter(member=member).count() == 1
    assert not Relationship.objects.filter(status="published").exists()


@pytest.mark.django_db
def test_snapshot_failure_rolls_back_new_records_and_withdrawals(reviewer):
    apply_snapshot(snapshot())
    previous = ParliamentRecord.objects.get()
    relation = publish_record(previous, reviewer)
    changed = snapshot(
        [roster_row(101), roster_row(102)],
        [biography_row(101, "Alteração fictícia"), biography_row(102)],
        expected_count=2,
    )
    with (
        patch.object(
            ParliamentRecord.objects, "create", side_effect=DatabaseError("fictional failure")
        ),
        pytest.raises(DatabaseError),
    ):
        apply_snapshot(changed)
    relation.refresh_from_db()
    previous.evidence.refresh_from_db()
    assert relation.status == "published"
    assert previous.evidence.is_public
    assert ParliamentMember.objects.count() == 1
    assert ParliamentRecord.objects.count() == 1
    assert Relationship.objects.count() == 1
    assert not ReviewEvent.objects.filter(action="invalidate").exists()


@pytest.mark.django_db
def test_old_snapshot_cannot_reactivate_a_ceased_member():
    apply_snapshot(snapshot(as_of=date(2025, 7, 2)))
    with pytest.raises(ParliamentImportError, match="older"):
        apply_snapshot(snapshot())
    assert ParliamentImportState.objects.get().as_of == date(2025, 7, 2)


def test_command_defaults_to_dry_run_without_database_access(capsys):
    with patch(
        "ligacoes.core.management.commands.import_parliament.fetch_snapshot",
        return_value=snapshot(),
    ):
        call_command("import_parliament")
    assert "No database writes" in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "http://app.parlamento.pt/webutils/docs/doc.txt?path=x&fich=InformacaoBaseXVII_json.txt&Inline=true",
        "https://app.parlamento.pt.evil.example/webutils/docs/doc.txt?path=x&fich=InformacaoBaseXVII_json.txt&Inline=true",
        "https://user@app.parlamento.pt/webutils/docs/doc.txt?path=x&fich=InformacaoBaseXVII_json.txt&Inline=true",
        "https://app.parlamento.pt/other?path=x&fich=InformacaoBaseXVII_json.txt&Inline=true",
        "https://app.parlamento.pt/webutils/docs/doc.txt?path=x&fich=InformacaoBaseXVII_json.txt&Inline=true&path=y",
        "file:///etc/passwd",
    ],
)
def test_fetcher_rejects_unwanted_urls_before_network_access(url: str):
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("Network must not be reached")),
        pytest.raises(ParliamentImportError),
    ):
        fetch_url(url, "roster", "XVII")


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]
)
def test_fetcher_rejects_private_resolution_before_connecting(address: str):
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]),
        patch("socket.socket", side_effect=AssertionError("Socket must not be opened")),
        pytest.raises(ParliamentImportError, match="non-public"),
    ):
        _PinnedHTTPSConnection("app.parlamento.pt").connect()


def test_discovery_rejects_external_link_with_valid_filename():
    catalogue = b'<a href="https://evil.example/file?fich=InformacaoBaseXVII_json.txt">JSON</a>'

    def catalogue_only(url: str, dataset: str, legislature: str) -> Download:
        validate_url(url, dataset, legislature)
        return Download(catalogue, url)

    with (
        patch("ligacoes.core.parliament_fetch.fetch_url", side_effect=catalogue_only),
        pytest.raises(ParliamentImportError),
    ):
        discover_download("roster", "XVII")


def test_fetcher_rejects_off_host_redirect_even_to_another_allowed_route():
    response = Mock(status=302)
    response.getheader.return_value = download_url("roster")
    connection = Mock()
    connection.getresponse.return_value = response
    with (
        patch("ligacoes.core.parliament_fetch._PinnedHTTPSConnection", return_value=connection),
        pytest.raises(ParliamentImportError, match="Cross-host"),
    ):
        fetch_url(
            "https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx", "roster", "XVII"
        )
    assert connection.request.call_count == 1


def test_fetcher_rejects_oversized_response_without_a_content_length():
    response = Mock(status=200)
    response.getheader.side_effect = lambda name, default=None: default
    response.read1.return_value = b"fictional"
    connection = Mock(sock=None)
    connection.getresponse.return_value = response
    with (
        patch("ligacoes.core.parliament_fetch.MAX_BYTES", 8),
        patch("ligacoes.core.parliament_fetch._PinnedHTTPSConnection", return_value=connection),
        pytest.raises(ParliamentImportError, match="size limit"),
    ):
        fetch_url(download_url("roster"), "roster", "XVII")


@pytest.mark.django_db
def test_updated_supplied_mandate_end_becomes_a_new_draft(reviewer):
    apply_snapshot(snapshot())
    previous = ParliamentRecord.objects.get()
    publish_record(previous, reviewer)
    apply_snapshot(snapshot([roster_row(status=status_row(end="2025-07-31"))]))
    member = ParliamentMember.objects.get(cadastro_id="101")
    assert member.current_record is not None
    current = member.current_record.relationship
    assert current.end_date == date(2025, 7, 31)
    assert current.start_date == date(2025, 6, 3)
    assert current.status == "draft"
    previous.relationship.refresh_from_db()
    assert previous.relationship.status == "draft"
    assert previous.relationship.end_date is None
