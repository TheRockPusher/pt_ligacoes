import json
from datetime import date
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError

from ligacoes.core.models import (
    EntityAlias,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    ParliamentStatusInterval,
    Relationship,
    ReviewEvent,
)
from ligacoes.core.parliament_fetch import (
    LEGISLATURES,
    Download,
    ParliamentImportError,
    _PinnedHTTPSConnection,
    discover_download,
    fetch_url,
    file_suffix,
    validate_url,
)
from ligacoes.core.parliament_import import apply_snapshot
from ligacoes.core.parliament_parse import _object, _rows, parse_snapshot
from ligacoes.core.services import withdraw_relationship

DAY = date(2025, 7, 1)


def download_url(dataset, code="XVII", path="fictional"):
    prefix = "InformacaoBase" if dataset == "roster" else "RegistoBiografico"
    return (
        "https://app.parlamento.pt/webutils/docs/doc.txt"
        f"?path={path}&fich={prefix}{file_suffix(code)}_json.txt&Inline=true"
    )


def status_row(status="Efetivo", start="2025-06-03", end=None):
    return {"sioDes": status, "sioDtInicio": start, "sioDtFim": end}


def roster_row(cadastro=101, statuses=None, dep_id=None):
    return {
        "DepCadId": float(cadastro),
        "DepId": float(dep_id or cadastro + 1000),
        "DepNomeCompleto": f"Pessoa Fictícia {cadastro}",
        "DepNomeParlamentar": f"Fictícia {cadastro}",
        "DepCPDes": "Círculo Fictício",
        "DepGP": [{"gpId": 23, "gpSigla": "FIC", "gpDtInicio": "2025-06-03", "gpDtFim": None}],
        "DepSituacao": statuses if statuses is not None else [status_row()],
        "Videos": "PRIVATE_CANARY",
        "DepDtNascimento": "PRIVATE_CANARY",
    }


def biography_row(cadastro=101):
    return {
        "CadId": cadastro,
        "CadProfissao": "Profissão fictícia",
        "CadCargosFuncoes": [],
        "CadHabilitacoes": [],
        "CadDtNascimento": "PRIVATE_CANARY",
    }


def snapshot(
    rows=None,
    *,
    code="XVII",
    start="2025-06-03",
    end=None,
    as_of=DAY,
    biographies=None,
    path="fictional",
):
    detail = {"sigla": "I" if code in {"IA", "IB"} else code, "dtini": start, "dtfim": end}
    if code in {"IA", "IB"}:
        detail["siglaAntiga"] = code
    root = {"DetalheLegislatura": detail, "Deputados": rows if rows is not None else [roster_row()]}
    biography = (
        Download(json.dumps(biographies).encode(), download_url("biography", code))
        if biographies is not None
        else None
    )
    return parse_snapshot(
        Download(json.dumps(root).encode(), download_url("roster", code, path)),
        biography,
        legislature=code,
        as_of=as_of,
    )


def test_complete_history_merges_effective_periods_and_preserves_suspension_gaps():
    result = snapshot(
        [
            roster_row(
                statuses=[
                    status_row("Efetivo Temporário", "2025-06-03", "2025-06-09"),
                    status_row("Efetivo Definitivo", "2025-06-10", "2025-06-20"),
                    status_row("Suspenso(Eleito)", "2025-06-21", "2025-06-25"),
                    status_row("Efetivo", "2025-06-26", None),
                ]
            )
        ],
        as_of=date(2026, 1, 1),
        biographies=[],
    )
    assert result.mandate_count == 2
    assert result.members[0].periods == (
        ("Efetivo Temporário, Efetivo Definitivo", date(2025, 6, 3), date(2025, 6, 20)),
        ("Efetivo", date(2025, 6, 26), None),
    )

    retained_roster = _object(result.members[0].data["roster"], "roster")
    assert len(_rows(retained_roster["DepSituacao"], "DepSituacao")) == 4
    assert _rows(retained_roster["DepGP"], "DepGP")[0]["gpId"] == "23"
    assert "PRIVATE_CANARY" not in json.dumps(result.members[0].data)


def test_duplicate_cadastro_rows_union_periods_without_duplicating_people():
    rows = [
        roster_row(dep_id=111, statuses=[status_row(end="2025-06-20")]),
        roster_row(dep_id=112, statuses=[status_row(start="2025-06-15")]),
    ]
    result = snapshot(rows)
    assert len(result.members) == result.mandate_count == 1
    assert result.members[0].periods[0][1:] == (date(2025, 6, 3), None)
    rows[1]["DepNomeCompleto"] = "Conflicting fictional name"
    with pytest.raises(ParliamentImportError, match="Conflicting names"):
        snapshot(rows)


def test_missing_and_reversed_status_starts_remain_private_locating_context():
    result = snapshot(
        [
            roster_row(
                statuses=[
                    status_row("Efetivo", None, "2025-06-04"),
                    status_row("Efetivo", "2025-06-10", "2025-06-09"),
                    status_row("Suspenso(Eleito)", "2025-06-03"),
                ]
            )
        ]
    )
    assert result.mandate_count == 0

    retained_roster = _object(result.members[0].data["roster"], "roster")
    assert len(_rows(retained_roster["DepSituacao"], "DepSituacao")) == 3


@pytest.mark.parametrize(
    "code,bounds",
    [
        ("IA", (date(1976, 6, 3), date(1980, 1, 2))),
        ("IB", (date(1980, 1, 3), date(1980, 11, 12))),
    ],
)
def test_first_legislature_subperiods_are_clipped(code, bounds):
    result = snapshot(
        [roster_row(statuses=[status_row(start="1976-06-03", end="1980-11-12")])],
        code=code,
        start="1976-06-03",
        end="1980-11-12",
    )
    assert result.members[0].periods[0][1:] == bounds
    assert (result.legislature_start, result.legislature_end) == bounds


def test_published_historical_date_anomalies_are_retained_not_repaired():
    result = snapshot(
        [roster_row(statuses=[status_row(start="1980-11-03", end="1983-05-30")])],
        code="II",
        start="1980-11-13",
        end="1983-05-30",
    )
    assert result.members[0].periods[0][1] == date(1980, 11, 3)
    assert result.members[0].data["date_conflicts"] == ["1980-11-03"]


@pytest.mark.django_db
def test_snapshot_publishes_each_period_writes_all_statuses_and_records_aliases():
    observed = snapshot(
        [
            roster_row(
                statuses=[
                    status_row(end="2025-06-10"),
                    status_row("Suspenso(Eleito)", "2025-06-11", "2025-06-20"),
                    status_row(start="2025-06-21"),
                ]
            ),
            roster_row(102, [status_row("Suspenso(Eleito)")]),
        ]
    )
    result = apply_snapshot(observed)
    assert result.serving == result.created_records == 2
    assert result.created_members == 2
    assert ParliamentStatusInterval.objects.count() == 4
    assert EntityAlias.objects.filter(
        name="Fictícia 101", scheme="parliament", external_id="101"
    ).exists()
    assert EntityAlias.objects.filter(name="Fictícia 102", external_id="102").exists()
    mandates = Relationship.objects.filter(kind="public_office", status="published").order_by(
        "start_date"
    )
    assert list(mandates.values_list("start_date", "end_date", "temporal_status")) == [
        (date(2025, 6, 3), date(2025, 6, 10), "ended"),
        (date(2025, 6, 21), None, "current"),
    ]
    assert not Relationship.objects.filter(
        subject=ParliamentMember.objects.get(pk="102").entity
    ).exists()
    record_count, reviews = ParliamentRecord.objects.count(), ReviewEvent.objects.count()
    rerun = apply_snapshot(observed)
    assert rerun.created_members == rerun.created_records == rerun.ceased_members == 0
    assert ParliamentRecord.objects.count() == record_count
    assert ReviewEvent.objects.count() == reviews
    assert ParliamentStatusInterval.objects.count() == 4


@pytest.mark.django_db
def test_older_legislature_never_withdraws_newer_and_historic_open_period_is_ended():
    apply_snapshot(snapshot())
    current = Relationship.objects.get(status="published")
    apply_snapshot(
        snapshot(
            [roster_row(statuses=[status_row(start="2002-04-05", end=None)])],
            code="IX",
            start="2002-04-05",
            end="2005-03-09",
            as_of=date(2025, 6, 1),
        )
    )
    current.refresh_from_db()
    assert current.status == "published"
    assert Relationship.objects.filter(
        term__code="IX", status="published", temporal_status="ended"
    ).exists()
    assert ParliamentImportState.objects.count() == 2


@pytest.mark.django_db
def test_corrected_end_revises_existing_claim_without_duplicate_and_withdrawal_sticks(reviewer):
    apply_snapshot(snapshot())
    record = ParliamentRecord.objects.get()
    apply_snapshot(snapshot([roster_row(statuses=[status_row(end="2025-06-30")])]))
    record.refresh_from_db()
    assert ParliamentRecord.objects.count() == Relationship.objects.count() == 1
    assert record.relationship.end_date == date(2025, 6, 30)
    assert record.relationship.temporal_status == "ended"
    assert record.relationship.status == "published"
    withdraw_relationship(record.relationship, reviewer)
    apply_snapshot(snapshot([roster_row(statuses=[status_row(end="2025-06-29")])]))
    record.relationship.refresh_from_db()
    assert record.relationship.status == "rejected"


@pytest.mark.django_db
def test_snapshot_absence_is_scoped_and_status_intervals_are_replaced():
    apply_snapshot(snapshot())
    apply_snapshot(snapshot([roster_row(102)]))
    old = ParliamentRecord.objects.get(member_id="101")
    assert not old.is_current
    assert old.relationship.status == "draft"
    assert old.relationship.end_date is None
    assert not ParliamentStatusInterval.objects.filter(cadastro_id="101").exists()
    with pytest.raises(ParliamentImportError, match="newer"):
        apply_snapshot(snapshot(as_of=date(2025, 6, 30)))


@pytest.mark.django_db
def test_apply_is_atomic_on_late_failure():
    apply_snapshot(snapshot())
    with (
        patch(
            "ligacoes.core.parliament_import.ParliamentStatusInterval.objects.bulk_create",
            side_effect=DatabaseError,
        ),
        pytest.raises(DatabaseError),
    ):
        apply_snapshot(snapshot([roster_row(102)]))
    assert ParliamentRecord.objects.get().is_current
    assert not ParliamentMember.objects.filter(pk="102").exists()
    assert ParliamentStatusInterval.objects.get().cadastro_id == "101"


def test_command_defaults_to_dry_run_without_database_access(capsys):
    with patch(
        "ligacoes.core.management.commands.import_parliament.fetch_snapshot",
        return_value=snapshot(),
    ):
        call_command("import_parliament")
    assert "mandates=1" in capsys.readouterr().out


@pytest.mark.parametrize("code,suffix", [("Cons", "Constituinte"), ("IA", "IA"), ("IB", "IB")])
def test_official_filename_discovery(code, suffix):
    url = download_url("roster", code)
    html = f'<a href="{url}">JSON</a>'.encode()
    with patch(
        "ligacoes.core.parliament_fetch.fetch_url",
        side_effect=[
            Download(html, "https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx"),
            Download(b"{}", url),
        ],
    ):
        assert discover_download("roster", code).url == url
    validate_url(url, "roster", code)
    assert f"InformacaoBase{suffix}_json.txt" in url
    assert code in LEGISLATURES


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
def test_fetcher_rejects_unwanted_urls_before_network_access(url):
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError),
        pytest.raises(ParliamentImportError),
    ):
        fetch_url(url, "roster", "XVII")


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]
)
def test_fetcher_rejects_private_resolution_before_connecting(address):
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]),
        patch("socket.socket", side_effect=AssertionError),
        pytest.raises(ParliamentImportError, match="non-public"),
    ):
        _PinnedHTTPSConnection("app.parlamento.pt").connect()


def test_discovery_rejects_external_link_with_valid_filename():
    catalogue = b'<a href="https://evil.example/file?fich=InformacaoBaseXVII_json.txt">JSON</a>'

    def catalogue_only(url, dataset, legislature):
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
def test_all_raw_status_intervals_survive_conflicting_ends_and_missing_starts():
    apply_snapshot(
        snapshot(
            [
                roster_row(
                    statuses=[
                        status_row("Efetivo", "2025-06-03", "2025-06-09"),
                        status_row("Efetivo", "2025-06-03", "2025-06-10"),
                        status_row("Suspenso(Eleito)", None, "2025-06-02"),
                    ]
                )
            ]
        )
    )
    assert ParliamentStatusInterval.objects.count() == 3
    assert ParliamentStatusInterval.objects.filter(start=None).exists()
    mandate = Relationship.objects.get(kind="public_office")
    assert mandate.end_date == date(2025, 6, 10)


def test_constituent_catalogue_display_name_and_actual_query_code_are_supported():
    url = download_url("roster", "Cons").replace("Constituinte_json.txt", "Cons_json.txt")
    validate_url(url, "roster", "Cons")
    html = f'<a href="{url}">InformacaoBaseConstituinte_json.txt</a>'.encode()
    with patch(
        "ligacoes.core.parliament_fetch.fetch_url",
        side_effect=[
            Download(html, "https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx"),
            Download(b"{}", url),
        ],
    ):
        assert discover_download("roster", "Cons").url == url


@pytest.mark.parametrize(
    "error", [ValidationError("PRIVATE_CANARY"), DatabaseError("PRIVATE_CANARY")]
)
def test_command_failure_reports_exception_class_without_exception_content(error):
    with (
        patch(
            "ligacoes.core.management.commands.import_parliament.fetch_snapshot",
            return_value=snapshot(),
        ),
        patch(
            "ligacoes.core.management.commands.import_parliament.apply_snapshot", side_effect=error
        ),
        pytest.raises(CommandError) as caught,
    ):
        call_command("import_parliament", "--apply")
    assert type(error).__name__ in str(caught.value)
    assert "PRIVATE_CANARY" not in str(caught.value)
    assert "rolled back" in str(caught.value)


@pytest.mark.django_db
def test_distinct_cadastro_namesakes_with_overlapping_service_remain_distinct_people():
    apply_snapshot(snapshot())
    namesake = roster_row(102)
    namesake["DepNomeCompleto"] = "Pessoa Fictícia 101"
    namesake["DepNomeParlamentar"] = "Fictícia 101"
    apply_snapshot(snapshot([roster_row(), namesake]))
    first = ParliamentMember.objects.get(cadastro_id="101")
    second = ParliamentMember.objects.get(cadastro_id="102")
    assert first.entity_id != second.entity_id
    assert ParliamentRecord.objects.filter(is_current=True).count() == 2
    assert Relationship.objects.filter(status="published", kind="public_office").count() == 2
