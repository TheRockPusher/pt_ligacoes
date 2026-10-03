import json
from datetime import date
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ligacoes.core.models import (
    Entity,
    Evidence,
    Relationship,
    Source,
    SourceIdentity,
    SourceObservation,
    Term,
)
from ligacoes.core.parliament_fetch import Download, ParliamentImportError
from ligacoes.core.parliament_interests import (
    InterestRow,
    InterestsSnapshot,
    apply_snapshot,
    parse_snapshot,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue

DAY = date(2026, 9, 28)
LATER = date(2026, 10, 5)
CAD_A = 9101
CAD_B = 9102
SPOUSE_NAME = "Cônjuge Fictícia Zeta Canário"
# Values that must never reach any stored row: spouse, personal and excluded fields.
FORBIDDEN = (
    SPOUSE_NAME,
    "Sociedade Fictícia Zeta",
    "Empresa Fictícia Kappa",
    "Sociedade Fictícia Xi",
    "PRIVATE_",
    "501234567",
    "912 345 678",
    "Partido Fictício",
    "Outra situação fictícia",
    "DGF-0042",
)
VERSION_KEYS = ("RegistoInteressesV1", "RegistoInteressesV2", "RegistoInteressesV3")


def url(legislature: str) -> str:
    return (
        "https://app.parlamento.pt/webutils/docs/doc.txt"
        f"?path=fictional&fich=RegistoInteresses{legislature}_json.txt&Inline=true"
    )


def element(version: str, record: JSONObject) -> JSONObject:
    result: JSONObject = dict.fromkeys((*VERSION_KEYS, "RegistoInteressesV5"))
    result[version] = record
    return result


def cargo(role: str, entity: str, start: str = "", end: str = "", **extra: str) -> JSONObject:
    row: JSONObject = {
        "Id": 0,
        "FormularioId": 0,
        "Formulario": None,
        "CargoFuncaoAtividade": role,
        "Entidade": entity,
        "LocalSede": "Localidade Fictícia",
        "Natureza": "Privada",
        "DataInicio": start,
        "DataTermo": end,
        "Remunerada": "0",
    }
    row.update(extra)
    return row


def default_cargos() -> list[JSONObject]:
    return [
        cargo("Consultora", "Empresa Fictícia Alfa, Lda.", "2019-10-25", "2022-03-28"),
        cargo("Vogal", "Associação Fictícia Beta", "2021"),
        cargo(
            "Membro da Comissão Nacional",
            "Partido Fictício",
            "2018-01-01",
            Natureza="Partido Político",
        ),
        cargo("Gerente (registo 501234567)", "Empresa Fictícia Delta", "2017"),
        cargo("Técnica", "Contacto 912 345 678", "2016"),
    ]


def v5(
    cadastro: int = CAD_A,
    *,
    cargos: list[JSONObject] | None = None,
    services: list[JSONObject] | None = None,
    legislature: str = "XV",
) -> JSONObject:
    return element(
        "RegistoInteressesV5",
        {
            "IdCadastroGODE": float(cadastro),
            "NomeIdentificacao": f"Deputada Fictícia {cadastro}",
            "Categoria": "Deputado(a)",
            "Servico": "GPFIC",
            "Legislatura": legislature,
            "versao": 5,
            "FactoDeclaracao": {
                "Id": 0,
                "CargoFuncao": "Deputado à Assembleia da República",
                "DataInicioFuncao": "2022-03-29T00:00:00",
                "DataAlteracaoFuncao": None,
                "DataCessacaoFuncao": None,
                "ChkDeclaracao": True,
                "Formulario": None,
            },
            "Exclusividade": {"Id": 0, "Exclusividade": False, "Formulario": None},
            "GenIncompatibilidade": {"Id": 0, "Incompatibilidade": True, "Formulario": None},
            "GenDadosPessoais": {
                "Id": 0,
                "NomeCompleto": "PRIVATE_FULL_NAME",
                "Sexo": "PRIVATE_SEX",
                "NomeConjuge": SPOUSE_NAME,
                "NIdentificacaoFiscal": "PRIVATE_NIF",
                "MoradaCompleta": "PRIVATE_ADDRESS",
                "Telemovel": "PRIVATE_PHONE",
            },
            "GenCargosMenosTresAnos": list[JSONValue](
                cargos if cargos is not None else default_cargos()
            ),
            "GenCargosMaisTresAnos": [
                cargo("Docente", "Universidade Fictícia Gama", "2010-09", "2015-06")
            ],
            "GenSociedade": [
                {
                    "Id": 0,
                    "FormularioId": 0,
                    "Sociedade": "Sociedade Fictícia Épsilon, Lda.",
                    "Natureza": "Sociedade por quotas",
                    "LocalSede": "Localidade Fictícia",
                    "NaturezaArea": "Serviços",
                    "Participacao": "PRIVATE_HOLDING_VALUE",
                },
                {
                    "Id": 0,
                    "FormularioId": 0,
                    "Sociedade": "Sociedade Fictícia Zeta, Lda.",
                    "Natureza": "Sociedade por Quotas (Cônjuge)",
                    "LocalSede": "Localidade Fictícia",
                    "NaturezaArea": "Comércio",
                    "Participacao": "25%",
                },
            ],
            "GenApoios": [
                {
                    "Id": 0,
                    "FormularioId": 0,
                    "Apoio": "Senhas de presença",
                    "Entidade": "Município Fictício",
                    "Data": "Desde 10/2023",
                    "NaturezaArea": "Autarquia",
                    "NaturezaBeneficio": "Senhas de presença",
                }
            ],
            "GenServicoPrestado": list[JSONValue](
                services if services is not None else [service("2018 a 2021")]
            ),
            "GenOutraSituacao": [
                {"Id": 0, "FormularioId": 0, "OutraSituacao": "Outra situação fictícia"}
            ],
        },
    )


def service(when: str) -> JSONObject:
    return {
        "Id": 0,
        "FormularioId": 0,
        "Servico": "Parecer jurídico",
        "Entidade": "Fundação Fictícia Eta",
        "Local": "Localidade Fictícia",
        "Data": when,
        "Natureza": "Consultoria",
    }


def v3(cadastro: int = CAD_A) -> JSONObject:
    first: JSONObject = {
        "RecordId": None,
        "PositionDesignation": "Deputado à Assembleia da República",
        "PositionBeginDate": "2019-10-25",
        "PositionChangedDate": None,
        "PositionEndDate": None,
        "Activities": [
            {
                "Activity": "Advogada",
                "Entity": "Sociedade Fictícia de Advogados Theta",
                "BeginDate": "23-10-2015",
                "EndDate": None,
                "Type": 0,
            },
            {
                "Activity": "Nada a declarar",
                "Entity": None,
                "BeginDate": None,
                "EndDate": None,
                "Type": 2,
            },
        ],
        "SocialPositions": [
            {
                "Position": "Presidente da Direção",
                "Entity": "Clube Fictício Iota",
                "ActivityArea": "Desporto",
                "HeadOfficeLocation": "Localidade Fictícia",
                "Type": 1,
            }
        ],
        "Societies": [
            {
                "Entity": "Empresa Fictícia Kappa",
                "ActivityArea": "Comércio",
                "HeadOfficeLocation": "Localidade Fictícia",
                "SocialParticipation": "24% (cônjuge)",
            }
        ],
        "Supports": [{"Support": "Nada a declarar"}],
        "ServicesProvided": None,
        "OtherSituations": [{"Situation": "Outra situação fictícia"}],
    }
    second = json.loads(json.dumps(first))
    second["PositionChangedDate"] = "2020-06-30"
    second["Activities"].append(
        {
            "Activity": "Formadora",
            "Entity": "Instituto Fictício Lambda",
            "BeginDate": "Maio 2016",
            "EndDate": "outubro 17",
            "Type": 1,
        }
    )
    return element(
        "RegistoInteressesV3",
        {
            "RecordId": cadastro,
            "FullName": f"Deputada Fictícia {cadastro}",
            "MaritalStatus": "PRIVATE_MARITAL",
            "SpouseName": SPOUSE_NAME,
            "MatrimonialRegime": "PRIVATE_REGIME",
            "DGFNumber": "DGF-0042",
            "Exclusivity": "N",
            "RecordInterests": [first, second],
        },
    )


def v2(cadastro: int = CAD_A) -> JSONObject:
    return element(
        "RegistoInteressesV2",
        {
            "cadId": float(cadastro),
            "cadNomeCompleto": f"Deputada Fictícia {cadastro}",
            "cadNomeConjuge": SPOUSE_NAME,
            "cadEstadoCivilCod": 2.0,
            "cadEstadoCivilDes": "PRIVATE_MARITAL",
            "cadFamId": 7.0,
            "cadActividadeProfissional": "PRIVATE_PROFESSION",
            "cadRgi": [
                {
                    "rgiId": 501.0,
                    "rgiLegDes": "XIII",
                    "rgiDataVersao": "2016-02-04",
                    "rgiCargoDes": "Deputada",
                    "rgiCargoData": "2015",
                    "rgiCadId": float(cadastro),
                    "rgiActividades": [
                        {
                            "rgaId": 11.0,
                            "rgaActividade": "Economista na Empresa Fictícia Mu",
                            "rgaDataInicio": "2013",
                            "rgaDataFim": "2015-06",
                            "rgaRemunerada": "S",
                        }
                    ],
                    "rgiCargosSociais": [
                        {
                            "rgcId": 21.0,
                            "rgcCargo": "Vogal do Conselho Fiscal",
                            "rgcEntidade": "Cooperativa Fictícia Nu",
                            "rgcAreaActividade": "Agricultura",
                            "rgcLocalSede": "Localidade Fictícia",
                            "rgcDataInicio": "2014-03-01",
                            "rgcDataFim": None,
                        }
                    ],
                    "rgiSociedades": [
                        {
                            "rgsId": 31.0,
                            "rgsEntidade": "Sociedade Fictícia Xi, Lda.",
                            "rgsAreaActividade": "Serviços",
                            "rgsLocalSede": "Localidade Fictícia",
                            "rgsPartiSocial": "Cônjuge: 12,5%",
                        },
                        {
                            "rgsId": 32.0,
                            "rgsEntidade": "Sociedade Fictícia Ómicron, Lda.",
                            "rgsAreaActividade": "Serviços",
                            "rgsLocalSede": "Localidade Fictícia",
                            "rgsPartiSocial": "PRIVATE_HOLDING_VALUE",
                        },
                    ],
                    "rgiApoiosBeneficios": "Nada a declarar.",
                    "rgiServicosPrestados": (
                        "- Parecer para a Fundação Fictícia Pi\n- Nada a declarar"
                    ),
                    "rgiOutrasSituacoes": "Outra situação fictícia",
                    "rgiRegimeBensId": 1.0,
                    "rgiRegimeBensDes": "PRIVATE_REGIME",
                }
            ],
        },
    )


def empty_shell(cadastro: int) -> JSONObject:
    return element(
        "RegistoInteressesV3",
        {
            "RecordId": cadastro,
            "FullName": "PRIVATE_SHELL_NAME",
            "MaritalStatus": None,
            "SpouseName": None,
            "MatrimonialRegime": None,
            "DGFNumber": None,
            "Exclusivity": "N",
            "RecordInterests": None,
        },
    )


def parsed(
    elements: list[JSONObject], legislature: str = "XV", as_of: date = DAY
) -> InterestsSnapshot:
    return parse_snapshot(
        Download(json.dumps(elements).encode(), url(legislature)),
        legislature=legislature,
        as_of=as_of,
    )


def rows_by_role(snapshot: InterestsSnapshot) -> dict[str, InterestRow]:
    return {row.role or row.object_name: row for d in snapshot.declarants for row in d.rows}


def test_v5_rows_become_minimised_rows_with_normalised_precision():
    snapshot = parsed([v5(), empty_shell(CAD_B)])
    assert [d.cadastro_id for d in snapshot.declarants] == [str(CAD_A)]
    rows = rows_by_role(snapshot)
    assert set(rows) == {
        "Consultora",
        "Vogal",
        "Docente",
        "Sociedade Fictícia Épsilon, Lda.",
        "Senhas de presença",
        "Parecer jurídico",
    }
    consultant = rows["Consultora"]
    assert (consultant.object_name, consultant.kind) == (
        "Empresa Fictícia Alfa, Lda.",
        "professional_activity",
    )
    assert (consultant.start, consultant.end) == (date(2019, 10, 25), date(2022, 3, 28))
    assert consultant.temporal_status == "ended"
    assert consultant.declared_on is None
    assert consultant.reference == (
        f"RegistoInteressesXV / CadId={CAD_A} / GenCargosMenosTresAnos / linha 1 / "
        "versão 2022-03-29"
    )
    member = rows["Vogal"]
    assert (member.start, member.start_precision, member.end) == (date(2021, 1, 1), "year", None)
    assert member.temporal_status == "unknown"
    teacher = rows["Docente"]
    assert (teacher.start, teacher.start_precision) == (date(2010, 9, 1), "month")
    assert (teacher.end, teacher.end_precision) == (date(2015, 6, 30), "month")
    company = rows["Sociedade Fictícia Épsilon, Lda."]
    assert (company.kind, company.role) == ("shareholding", "")
    support = rows["Senhas de presença"]
    assert (support.object_name, support.start, support.end) == (
        "Município Fictício",
        date(2023, 10, 1),
        None,
    )
    assert dict(snapshot.excluded) == {"identifier": 2, "party": 1, "spouse": 1}


@pytest.mark.parametrize(
    "when,start,end",
    [
        ("2018 a 2021", (date(2018, 1, 1), "year"), (date(2021, 12, 31), "year")),
        ("23.09.2023", (date(2023, 9, 23), "day"), (None, "day")),
        ("Outubro de 2021", (date(2021, 10, 1), "month"), (None, "day")),
        ("Out/17 a Set/21", (None, "day"), (None, "day")),
        ("sem data fixa", (None, "day"), (None, "day")),
        ("31/02/2020", (None, "day"), (None, "day")),
        ("2021 - 2019", (date(2021, 1, 1), "year"), (None, "day")),
    ],
)
def test_free_text_service_dates_are_parsed_only_for_known_shapes(when, start, end):
    snapshot = parsed([v5(cargos=[], services=[service(when)])])
    row = rows_by_role(snapshot)["Parecer jurídico"]
    assert ((row.start, row.start_precision), (row.end, row.end_precision)) == (start, end)
    assert when in row.passage


def test_v3_versions_are_deduplicated_and_spouse_or_empty_rows_skipped():
    snapshot = parsed([v3()], legislature="XIV")
    rows = rows_by_role(snapshot)
    assert set(rows) == {"Advogada", "Presidente da Direção", "Formadora"}
    lawyer = rows["Advogada"]
    assert (lawyer.start, lawyer.start_precision) == (date(2015, 10, 23), "day")
    assert lawyer.object_name == "Sociedade Fictícia de Advogados Theta"
    assert lawyer.reference.endswith("/ Activities / linha 1 / versão 1 (2019-10-25)")
    assert rows["Presidente da Direção"].kind == "directorship"
    trainer = rows["Formadora"]
    # "outubro 17" has a two-digit year: unknown, not guessed.
    assert (trainer.start, trainer.start_precision, trainer.end) == (
        date(2016, 5, 1),
        "month",
        None,
    )
    assert trainer.reference.endswith("versão 2 (2020-06-30)")


def test_v2_rows_keep_declaration_date_separate_from_activity_dates():
    snapshot = parsed([v2()], legislature="XIII")
    rows = rows_by_role(snapshot)
    assert set(rows) == {
        "Economista na Empresa Fictícia Mu",
        "Vogal do Conselho Fiscal",
        "Sociedade Fictícia Ómicron, Lda.",
        "Parecer para a Fundação Fictícia Pi",
    }
    economist = rows["Economista na Empresa Fictícia Mu"]
    assert economist.object_name == ""
    assert (economist.start, economist.start_precision) == (date(2013, 1, 1), "year")
    assert (economist.end, economist.end_precision) == (date(2015, 6, 30), "month")
    assert all(row.declared_on == date(2016, 2, 4) for row in rows.values())
    assert rows["Vogal do Conselho Fiscal"].object_name == "Cooperativa Fictícia Nu"


def test_ambiguous_schema_version_fails_before_any_write():
    broken = v5()
    broken["RegistoInteressesV3"] = {"RecordId": CAD_A}
    with pytest.raises(ParliamentImportError):
        parsed([broken])


def stored_text() -> str:
    return json.dumps(
        [
            *SourceObservation.objects.values(),
            *Entity.objects.values(),
            *SourceIdentity.objects.values(),
            *Source.objects.values(),
        ],
        default=str,
        ensure_ascii=False,
    )


@pytest.mark.django_db
def test_apply_publishes_verifiable_interests_without_spouse_or_personal_data():
    institution = Entity.objects.create(
        name="Assembleia Fictícia", slug="assembleia-ficticia", kind="organisation"
    )
    term = Term.objects.create(
        kind="legislature",
        code="XV",
        label="XV Legislatura fictícia",
        institution=institution,
        start_date=date(2022, 3, 29),
    )
    result = apply_snapshot(parsed([v5()]))
    apply_snapshot(parsed([v3(CAD_B)], legislature="XIV"))
    apply_snapshot(parsed([v2(CAD_B)], legislature="XIII"))
    assert result["created"] == 6
    assert result["published"] == 6
    observations = SourceObservation.objects.all()
    assert observations.count() == 13
    assert Relationship.objects.filter(status="published").count() == 11
    assert Evidence.objects.filter(is_public=True, source__is_public=True).count() == 11
    assert {o.category for o in observations} == {"declared_interest"}
    assert all(o.is_current for o in observations)
    # Two legacy free-text rows have no structured organisation: retain their passages
    # rather than manufacturing a counterpart. Every resolved counterpart publishes.
    resolved = observations.filter(object__isnull=False)
    assert resolved.count() == 11
    assert all(o.relationship_id is not None for o in resolved)
    assert all(o.relationship.status == "published" for o in resolved if o.relationship is not None)
    assert observations.filter(object__isnull=True, relationship__isnull=True).count() == 2
    assert {o.dataset for o in observations} == {"ar_registo_interesses"}
    xv = observations.filter(scope=f"interests:XV:{CAD_A}")
    assert {o.term_id for o in xv} == {term.pk}
    assert not observations.filter(scope__startswith="interests:XIV:", term__isnull=False).exists()
    identity = xv.get(role="Consultora").identity
    assert identity is not None
    assert (identity.source, identity.external_id) == ("parliament", str(CAD_A))
    assert identity.entity.kind == "person"
    assert Entity.objects.filter(kind="person").count() == 2
    text = stored_text()
    for forbidden in FORBIDDEN:
        assert forbidden not in text


@pytest.mark.django_db
def test_reimport_ceases_removed_rows_and_absent_deputies():
    apply_snapshot(parsed([v5(), v5(CAD_B)]))
    kept = SourceObservation.objects.get(scope=f"interests:XV:{CAD_A}", role="Consultora")
    cargos = [row for row in default_cargos() if row["CargoFuncaoAtividade"] != "Vogal"]
    result = apply_snapshot(parsed([v5(cargos=cargos)], as_of=LATER))
    assert result["ceased"] == 1 + 6
    assert result["created"] == 0
    kept.refresh_from_db()
    assert kept.is_current
    assert not SourceObservation.objects.get(role="Vogal", scope__endswith=str(CAD_A)).is_current
    assert not SourceObservation.objects.filter(
        scope=f"interests:XV:{CAD_B}", is_current=True
    ).exists()
    assert (
        SourceObservation.objects.filter(scope__startswith="interests:XV:", is_current=True).count()
        == 5
    )
    assert Relationship.objects.filter(status="published").count() == 5
    assert kept.relationship is not None and kept.relationship.status == "published"
    assert Relationship.objects.filter(status="draft").count() == 7


@pytest.mark.django_db
def test_command_is_dry_run_by_default_and_apply_publishes_interests(capsys):
    payload = Download(json.dumps([v5()]).encode(), url("XV"))
    with patch(
        "ligacoes.core.parliament_interests.discover_download", return_value=payload
    ) as fetch:
        call_command("import_parliament_interests", "--legislature", "XV", as_of=DAY)
        assert not SourceObservation.objects.exists()
        assert not Entity.objects.exists()
        call_command("import_parliament_interests", "--legislature", "XV", "--apply", as_of=DAY)
    fetch.assert_called_with("interests", "XV")
    assert SourceObservation.objects.filter(is_current=True).count() == 6
    assert Relationship.objects.filter(status="published").count() == 6
    output = capsys.readouterr().out
    assert "Deputada Fictícia" not in output
    assert "publicados automaticamente" in output
