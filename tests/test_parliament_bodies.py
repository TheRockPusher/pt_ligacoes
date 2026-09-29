import json
from datetime import date
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pytest

from ligacoes.core.models import (
    Entity,
    ParliamentMember,
    Relationship,
    SourceIdentity,
    SourceObservation,
    Term,
)
from ligacoes.core.parliament_bodies import (
    BodiesSnapshot,
    apply_snapshot,
    build_snapshot,
    fetch_snapshot,
)
from ligacoes.core.parliament_fetch import (
    Download,
    ParliamentImportError,
    discover_download,
    fetch_url,
    validate_legislature,
    validate_url,
)
from ligacoes.core.parliament_import import apply_snapshot as apply_roster
from ligacoes.core.parliament_parse import JSONObject, JSONValue, parse_snapshot
from ligacoes.public.selectors import public_relationships

AFTER_XVI = date(2025, 7, 1)
PERIODS = {
    "XV": ("2022-03-29", "2024-03-25"),
    "XVI": ("2024-03-26", "2025-06-02"),
    "XVII": ("2025-06-03", None),
}


def file_url(prefix: str, code: str) -> str:
    query = urlencode({"path": "ficticio", "fich": f"{prefix}{code}_json.txt", "Inline": "true"})
    return f"https://app.parlamento.pt/webutils/docs/doc.txt?{query}"


def payload(prefix: str, code: str, value: JSONValue) -> Download:
    return Download(json.dumps(value).encode(), file_url(prefix, code))


def deputy(cadastro: int, code: str = "XVI", *, status: str = "Efetivo") -> JSONObject:
    start, end = PERIODS[code]
    return {
        "DepId": float(cadastro + 10000),
        "DepCadId": float(cadastro),
        "DepNomeParlamentar": f"Fictícia {cadastro}",
        "DepNomeCompleto": f"Pessoa Fictícia {cadastro}",
        "DepCPDes": "Círculo Fictício",
        "LegDes": code,
        "DepGP": [{"gpId": 7251.0, "gpSigla": "FIC", "gpDtInicio": start, "gpDtFim": end}],
        "DepSituacao": [{"sioDes": status, "sioDtInicio": start, "sioDtFim": end}],
        "DepCargo": None,
    }


def roster(code: str, deputies: list[JSONObject]) -> Download:
    start, end = PERIODS[code]
    return payload(
        "InformacaoBase",
        code,
        {
            "DetalheLegislatura": {
                "sigla": code,
                "siglaAntiga": code,
                "dtini": start,
                "dtfim": end,
            },
            "Deputados": list[JSONValue](deputies),
        },
    )


def member(
    cadastro: int,
    *,
    kind: str = "Efetivo",
    start: str = "2024-04-18",
    end: str | None = "2025-06-01",
    cargo: str | None = None,
) -> JSONObject:
    return {
        "depId": float(cadastro + 10000),
        "depCadId": float(cadastro),
        "depNomeParlamentar": f"Fictícia {cadastro}",
        "legDes": "XVI",
        # Party-like fields are never read: only the parliamentary group ids are.
        "depGP": [{"gpId": 7251.0, "gpSigla": "FIC", "gpDtInicio": start, "gpDtFim": None}],
        "depCargo": (
            [{"carId": 3.0, "carDes": cargo, "carDtInicio": start, "carDtFim": end}]
            if cargo
            else None
        ),
        "depSituacao": [
            {"sioDes": "Ativo", "sioTipMem": kind, "sioDtInicio": start, "sioDtFim": end}
        ],
    }


def organ(organ_id: int, name: str, sigla: str, number: int, rows: list[JSONObject]) -> JSONObject:
    return {
        "DetalheOrgao": {
            "idOrgao": float(organ_id),
            "nomeSigla": f"{name} ",
            "numeroOrgao": float(number),
            "siglaLegislatura": "XVI",
            "siglaOrgao": sigla,
        },
        "HistoricoComposicao": list[JSONValue](rows),
        "Reunioes": [],
    }


def bodies(
    code: str = "XVI",
    *,
    committees: list[JSONObject] | None = None,
    subcommittees: list[JSONObject] | None = None,
    plenary: list[JSONObject] | None = None,
    mesa: list[JSONObject] | None = None,
) -> Download:
    staff: JSONObject = {
        "depId": 90001.0,
        "depCadId": 0.0,
        "depNomeParlamentar": "Funcionária Fictícia",
        "legDes": code,
        "depGP": None,
        "depCargo": [
            {
                "carId": 7.0,
                "carDes": "Secretária-geral",
                "carDtInicio": "2024-09-18",
                "carDtFim": None,
            }
        ],
        "depSituacao": [
            {"sioDes": "Activo", "sioTipMem": None, "sioDtInicio": None, "sioDtFim": None}
        ],
    }
    return payload(
        "OrgaoComposicao",
        code,
        {
            "Plenario": {
                "DetalheOrgao": {"idOrgao": 8386.0, "nomeSigla": "Plenário", "siglaOrgao": "PL"},
                "Composicao": list[JSONValue](plenary or []),
                "Reunioes": [],
            },
            "MesaAR": {
                "DetalheOrgao": {"idOrgao": 0.0, "nomeSigla": None, "siglaOrgao": "Mesa"},
                "HistoricoComposicao": None,
                "HistoricoComposicaoMesa": list[JSONValue](mesa or []),
                "Reunioes": [],
            },
            "ComissaoPermanente": None,
            "ConferenciaLideres": {"DetalheOrgao": None, "HistoricoComposicao": None},
            "ConferenciaPresidentesComissoes": None,
            "ConselhoAdministracao": {
                "DetalheOrgao": {
                    "idOrgao": 8388.0,
                    "nomeSigla": "Conselho de Administração",
                    "numeroOrgao": 0.0,
                    "siglaOrgao": "CA",
                },
                "HistoricoComposicao": [staff],
                "Reunioes": [],
            },
            "Comissoes": list[JSONValue](committees or []),
            "SubComissoes": list[JSONValue](subcommittees or []),
            "GruposTrabalho": [],
        },
    )


def activity(code: str = "XVI", entries: list[tuple[int, JSONObject]] | None = None) -> Download:
    return payload(
        "AtividadeDeputado",
        code,
        [
            {
                "Deputado": {
                    "DepId": float(cadastro + 20000),
                    "DepCadId": float(cadastro),
                    "DepNomeParlamentar": f"Fictícia {cadastro}",
                    "DepNomeCompleto": f"Pessoa Fictícia {cadastro}",
                    "LegDes": "XVII",
                },
                "AtividadeDeputadoList": [section],
            }
            for cadastro, section in entries or []
        ],
    )


def delegations(code: str, members: list[JSONObject]) -> Download:
    delegation: JSONObject = {
        "Id": "115",
        "Nome": "Assembleia Parlamentar Fictícia",
        "Legislatura": code,
        "Sessao": "1",
        "DataEleicao": "31/07/2024 00:00:00",
        "Composicao": list[JSONValue](members),
        "Comissoes": [],
        "Reunioes": [],
    }
    # The official file repeats every delegation object.
    return payload("DelegacaoPermanente", code, [delegation, delegation, delegation])


def xvi_snapshot(*, as_of: date = AFTER_XVI, with_substitute: bool = True) -> BodiesSnapshot:
    committee_rows = [member(101, cargo="Presidente")]
    if with_substitute:
        committee_rows.append(member(102, kind="Suplente", end=None))
    return build_snapshot(
        legislature="XVI",
        as_of=as_of,
        roster=roster("XVI", [deputy(101), deputy(102, status="Suplente")]),
        bodies=bodies(
            committees=[organ(8401, "Comissão de Saúde Fictícia", "CO", 9, committee_rows)],
            subcommittees=[organ(8417, "Subcomissão Fictícia", "SC", 1, [member(101)])],
            plenary=[deputy(101), deputy(102, status="Suplente")],
            mesa=[
                {
                    "depId": 10101.0,
                    "depCadId": 101.0,
                    "depNomeParlamentar": "Fictícia 101",
                    "legDes": "XVI",
                    "depCargo": [
                        {
                            "carId": 6.0,
                            "carDes": "Vice-Presidente",
                            "carDtInicio": "2024-03-27",
                            "carDtFim": "2025-06-02",
                        }
                    ],
                }
            ],
        ),
        activity=activity(
            entries=[
                (
                    101,
                    {
                        "Gpa": [
                            {
                                "GplId": "517",
                                "GplNo": "Portugal - País Fictício",
                                "GplSelLg": "XVI",
                                "CgaCrg": "Membro",
                                "CgaDtini": "2025-02-06 00:00:00.0",
                                "CgaDtfim": None,
                            }
                        ],
                        "Scgt": [
                            {
                                "ScmCd": "1",
                                "CcmDscom": "Subcomissão Fictícia ",
                                "ScmComCd": "9",
                                "ScmComLg": "XVI",
                                "CmsCargo": None,
                            }
                        ],
                    },
                )
            ]
        ),
    )


def claim(subject: Entity, object_name: str, role: str) -> Relationship:
    return Relationship.objects.select_related("term", "object").get(
        subject=subject, object__name__startswith=object_name, role=role
    )


def person(cadastro: int) -> Entity:
    return SourceIdentity.objects.get(source="parliament", external_id=str(cadastro)).entity


@pytest.mark.django_db
def test_import_auto_publishes_structured_memberships_per_legislature():
    apply_snapshot(xvi_snapshot())
    deputy_101 = person(101)
    committee = claim(deputy_101, "Comissão de Saúde Fictícia (XVI Legislatura)", "Efetivo")
    assert committee.status == "published"
    assert (committee.kind, committee.role_class, committee.temporal_status) == (
        "membership",
        "member",
        "ended",
    )
    assert (committee.start_date, committee.end_date) == (date(2024, 4, 18), date(2025, 6, 1))
    assert committee.term is not None
    assert (committee.term.code, committee.term.end_date) == ("XVI", date(2025, 6, 2))
    evidence = committee.evidence.get()
    assert evidence.page_reference == "OrgaoComposicaoXVI / idOrgao=8401 / depCadId=101"
    assert evidence.source.url == file_url("OrgaoComposicao", "XVI")
    assert evidence.source.dataset == "ar_composicao_orgaos"
    assert evidence.is_public
    chair = claim(deputy_101, "Comissão de Saúde Fictícia", "Presidente")
    assert (chair.kind, chair.role_class, chair.status) == ("membership", "leadership", "published")
    substitute = claim(person(102), "Comissão de Saúde Fictícia", "Suplente")
    assert (substitute.role_class, substitute.end_date) == ("substitute", None)
    # The legislature is over, so an open source interval is ended, not current.
    assert substitute.temporal_status == "ended"

    mesa = claim(deputy_101, "Mesa da Assembleia da República (XVI", "Vice-Presidente")
    assert (mesa.kind, mesa.role_class, mesa.status) == (
        "public_office",
        "deputy_leadership",
        "published",
    )
    group = claim(deputy_101, "Grupo Parlamentar do FIC (XVI Legislatura)", "Deputado/a")
    assert (group.kind, group.status, group.object.classification) == (
        "membership",
        "published",
        "parliamentary_group",
    )
    # A substitute who never sat is not a member of the parliamentary group.
    assert not Relationship.objects.filter(subject=person(102), object=group.object).exists()
    friendship = claim(deputy_101, "Grupo Parlamentar de Amizade Portugal - País", "Membro")
    assert friendship.start_date == date(2025, 2, 6)
    assert "só inclui deputados da composição mais recente" in friendship.evidence.get().excerpt

    ar = Entity.objects.get(classification="parliament")
    subcommittee = Entity.objects.get(name="Subcomissão Fictícia (XVI Legislatura)")
    parent = Relationship.objects.get(subject=subcommittee, kind="part_of")
    assert parent.object == committee.object
    assert parent.status == "published"
    assert Relationship.objects.get(subject=committee.object, kind="part_of").object == ar

    staff = SourceObservation.objects.get(subject_name="Funcionária Fictícia", role="")
    assert staff.identity is None and staff.relationship is None
    assert not Entity.objects.filter(name__contains="Funcionária Fictícia").exists()


@pytest.mark.django_db
def test_same_official_ids_in_two_legislatures_are_distinct_entities():
    apply_snapshot(xvi_snapshot())
    apply_snapshot(
        build_snapshot(
            legislature="XV",
            as_of=AFTER_XVI,
            roster=roster("XV", [deputy(101, "XV")]),
            bodies=bodies(
                "XV",
                committees=[
                    organ(
                        8401,
                        "Comissão de Saúde Fictícia",
                        "CO",
                        9,
                        [member(101, start="2022-04-20", end="2024-03-25")],
                    )
                ],
                plenary=[deputy(101, "XV")],
            ),
            activity=activity("XV"),
        )
    )
    committees = Entity.objects.filter(name__startswith="Comissão de Saúde Fictícia")
    assert sorted(committees.values_list("name", flat=True)) == [
        "Comissão de Saúde Fictícia (XV Legislatura)",
        "Comissão de Saúde Fictícia (XVI Legislatura)",
    ]
    assert sorted(
        SourceIdentity.objects.filter(entity__in=committees).values_list("external_id", flat=True)
    ) == ["orgao:XV:8401", "orgao:XVI:8401"]
    assert Entity.objects.filter(classification="parliamentary_group").count() == 2
    assert Entity.objects.filter(kind="person", name="Pessoa Fictícia 101").count() == 1
    assert set(Term.objects.values_list("code", flat=True)) == {"XV", "XVI"}


@pytest.mark.django_db
def test_delegation_member_depids_resolve_through_other_legislature_rosters():
    chair: JSONObject = {"Id": "10301", "Nome": "Fictícia 301", "Gp": "FIC", "Cargo": "Presidente"}
    chair |= {"DataInicio": "", "DataFim": "11/03/2025 00:00:00"}
    unknown: JSONObject = {
        "Id": "99999",
        "Nome": "Nome Sem Registo",
        "Gp": "FIC",
        "Cargo": "Membro",
    }
    unknown |= {"DataInicio": "", "DataFim": ""}
    files = {
        ("roster", "XVI"): roster("XVI", [deputy(101)]),
        # The chair's DepId was issued in the XV Legislatura, as in the official files.
        ("roster", "XV"): roster("XV", [deputy(301, "XV")]),
        ("bodies", "XVI"): bodies(plenary=[deputy(101)]),
        ("activity", "XVI"): activity(entries=[(101, {})]),
        ("delegations", "XVI"): delegations("XVI", [chair]),
    }
    requested: list[tuple[str, str]] = []

    def serve(dataset: str, code: str) -> Download:
        requested.append((dataset, code))
        return files[(dataset, code)]

    with patch("ligacoes.core.parliament_bodies.discover_download", side_effect=serve):
        fetch_snapshot(legislature="XVI", as_of=AFTER_XVI)
    # Nearest rosters first, and only while a member DepId is unresolved.
    assert requested == [
        ("roster", "XVI"),
        ("bodies", "XVI"),
        ("activity", "XVI"),
        ("delegations", "XVI"),
        ("roster", "XV"),
    ]
    apply_snapshot(
        build_snapshot(
            legislature="XVI",
            as_of=AFTER_XVI,
            roster=files[("roster", "XVI")],
            bodies=files[("bodies", "XVI")],
            activity=files[("activity", "XVI")],
            delegations=delegations("XVI", [chair, unknown]),
            other_rosters=(("XV", files[("roster", "XV")]),),
        )
    )
    delegation = Entity.objects.get(classification="parliamentary_delegation")
    assert delegation.name == (
        "Delegação da Assembleia da República — Assembleia Parlamentar Fictícia (XVI Legislatura)"
    )
    relationship = Relationship.objects.get(object=delegation)
    assert (relationship.subject, relationship.role, relationship.role_class) == (
        person(301),
        "Presidente",
        "leadership",
    )
    assert (relationship.start_date, relationship.end_date, relationship.status) == (
        None,
        date(2025, 3, 11),
        "published",
    )
    assert relationship.evidence.get().page_reference == (
        "DelegacaoPermanenteXVI / Id=115 / DepId=10301 / DepCadId=301"
    )
    # An unresolved member stays a private name-only candidate.
    unresolved = SourceObservation.objects.get(subject_name="Nome Sem Registo")
    assert unresolved.identity is None and unresolved.relationship is None


@pytest.mark.django_db
def test_deputy_listed_under_two_mandate_depids_is_one_delegation_claim():
    # As in the official XII file: the same deputy appears once per mandate DepId
    # (one issued in an earlier legislature) with identical role and dates.
    earlier = deputy(101, "XV") | {"DepId": 30101.0}
    rows: list[JSONObject] = []
    for dep in ("10101", "30101"):
        row: JSONObject = {"Id": dep, "Nome": "Fictícia 101", "Gp": "FIC", "Cargo": "Membro"}
        row |= {"DataInicio": "29/07/2024 00:00:00", "DataFim": ""}
        rows.append(row)
    later: JSONObject = {"Id": "10101", "Nome": "Fictícia 101", "Gp": "FIC", "Cargo": "Presidente"}
    later |= {"DataInicio": "01/02/2025 00:00:00", "DataFim": ""}
    apply_snapshot(
        build_snapshot(
            legislature="XVI",
            as_of=AFTER_XVI,
            roster=roster("XVI", [deputy(101)]),
            bodies=bodies(plenary=[deputy(101)]),
            activity=activity(),
            delegations=delegations("XVI", [*rows, later]),
            other_rosters=(("XV", roster("XV", [earlier])),),
        )
    )
    delegation = Entity.objects.get(classification="parliamentary_delegation")
    claims = Relationship.objects.filter(subject=person(101), object=delegation)
    # Distinct roles or periods stay distinct claims.
    assert sorted(claims.values_list("role", "start_date")) == [
        ("Membro", date(2024, 7, 29)),
        ("Presidente", date(2025, 2, 1)),
    ]
    assert claims.get(role="Membro").evidence.get().page_reference == (
        "DelegacaoPermanenteXVI / Id=115 / DepId=10101,30101 / DepCadId=101"
    )


@pytest.mark.django_db
def test_membership_absent_from_a_later_file_is_withdrawn_from_public():
    apply_snapshot(xvi_snapshot())
    substitute = claim(person(102), "Comissão de Saúde Fictícia", "Suplente")
    assert public_relationships().filter(pk=substitute.pk).exists()
    result = apply_snapshot(xvi_snapshot(as_of=date(2025, 7, 2), with_substitute=False))
    substitute.refresh_from_db()
    assert result["ceased"] == 1
    assert substitute.status == "draft"
    assert not substitute.evidence.get().is_public
    assert not public_relationships().filter(pk=substitute.pk).exists()
    kept = claim(person(101), "Comissão de Saúde Fictícia", "Efetivo")
    assert kept.status == "published"


@pytest.mark.django_db
def test_historic_mandates_only_for_legislatures_the_roster_import_does_not_manage():
    apply_snapshot(xvi_snapshot())
    ar = Entity.objects.get(classification="parliament")
    mandate = Relationship.objects.get(subject=person(101), object=ar)
    assert (mandate.kind, mandate.role, mandate.role_class, mandate.status) == (
        "public_office",
        "Deputado/a",
        "member",
        "published",
    )
    assert mandate.term is not None
    assert (mandate.term.code, mandate.start_date, mandate.end_date) == (
        "XVI",
        date(2024, 3, 26),
        date(2025, 6, 2),
    )
    # A substitute who never sat holds no mandate.
    assert not Relationship.objects.filter(subject=person(102), object=ar).exists()

    xvii = date(2025, 7, 1)
    apply_roster(
        parse_snapshot(
            roster("XVII", [deputy(101, "XVII")]),
            payload(
                "RegistoBiografico",
                "XVII",
                [{"CadId": 101.0, "CadHabilitacoes": None, "CadCargosFuncoes": None}],
            ),
            legislature="XVII",
            as_of=xvii,
            expected_count=1,
        )
    )
    roster_mandate = Relationship.objects.get(subject=person(101), object=ar, term__code="XVII")
    assert (roster_mandate.role, roster_mandate.role_class, roster_mandate.temporal_status) == (
        "Deputado/a",
        "member",
        "current",
    )
    result = apply_snapshot(
        build_snapshot(
            legislature="XVII",
            as_of=xvii,
            roster=roster("XVII", [deputy(101, "XVII")]),
            bodies=bodies("XVII", plenary=[deputy(101, "XVII")]),
            activity=activity("XVII"),
        )
    )
    assert result["mandates_skipped"] == 1
    mandates = Relationship.objects.filter(subject=person(101), object=ar, term__code="XVII")
    assert mandates.count() == 1
    # One Assembleia, one deputy entity and one Term per legislature across both importers.
    assert Entity.objects.filter(classification="parliament").count() == 1
    assert ParliamentMember.objects.get(cadastro_id="101").entity == person(101)
    assert Entity.objects.filter(kind="person").count() == 2
    assert Term.objects.filter(code="XVII").count() == 1
    group = Relationship.objects.get(
        subject=person(101), object__classification="parliamentary_group", term__code="XVII"
    )
    assert (group.temporal_status, group.end_date) == ("current", None)


@pytest.mark.parametrize(("code", "label"), [("Cons", "Constituinte"), ("IA", "I Legislatura")])
def test_open_data_file_codes_besides_roman_numerals(code: str, label: str):
    download = file_url("OrgaoComposicao", code)
    catalogue = "https://www.parlamento.pt/Cidadania/Paginas/DAComposicaoOrgaos.aspx"
    pages = {
        catalogue: (
            '<a href="DAComposicaoOrgaos.aspx?t=6869&amp;Path=ficticio">'
            f"{label}</a><a href='DAComposicaoOrgaos.aspx?t=0&amp;Path=outro'>II Legislatura</a>"
        ),
        f"{catalogue}?t=6869&Path=ficticio": (
            f'<a href="{download.replace("&", "&amp;")}">JSON</a>'
        ),
        download: "{}",
    }

    def serve(url: str, dataset: str, legislature: str) -> Download:
        validate_url(url, dataset, legislature)
        return Download(pages[url].encode(), url)

    with patch("ligacoes.core.parliament_fetch.fetch_url", side_effect=serve):
        assert discover_download("bodies", code).url == download
    # The serving-roster import itself still takes Roman numerals only.
    with pytest.raises(ParliamentImportError):
        validate_legislature(code)


def test_oversize_official_file_is_rejected_by_its_dataset_limit():
    def connection() -> Mock:
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: default
        response.read1.side_effect = [b"0123456789", b""]
        return Mock(sock=None, getresponse=Mock(return_value=response))

    with (
        patch.dict("ligacoes.core.parliament_fetch.LARGE_DOWNLOADS", {"bodies": (8, 90)}),
        patch(
            "ligacoes.core.parliament_fetch._PinnedHTTPSConnection",
            side_effect=lambda *args, **kwargs: connection(),
        ),
    ):
        with pytest.raises(ParliamentImportError, match="size limit"):
            fetch_url(file_url("OrgaoComposicao", "XVI"), "bodies", "XVI")
        # Datasets without a larger bound keep the default limit.
        assert (
            fetch_url(file_url("DelegacaoPermanente", "XVI"), "delegations", "XVI").content
            == b"0123456789"
        )
