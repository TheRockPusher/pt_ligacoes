import io
import json
import zipfile
from collections import Counter
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from xml.sax.saxutils import escape

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ligacoes.core import eu_funds
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Event, EventParty, SourceIdentity
from ligacoes.public.selectors import public_events

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 1)
LATER = date(2026, 9, 15)
# Fictional legal-person NIPCs with valid check digits.
NIPC_A = "510000010"
NIPC_B = "510000029"
NIPC_C = "510000037"
NIPC_D = "510000045"
NIPC_E = "510000053"
NIPC_F = "600000117"
# Fictional natural persons as the sources publish them (pseudonyms, names, a personal NIF).
PERSONS = (
    "Rui Fictício Pardal",
    "Maria Fictícia Lopes",
    "Joana Fictícia Reis",
    "Carla Fictícia Mota",
    "AA123456",
    "AA654321",
    "A12345678",
    "12345678",
    "87654321",
    "234567899",
    "RGPD",
)
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"

type Cell = str | int | Decimal | None


def _column(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def xlsx(rows: Sequence[Sequence[Cell]], *, inline: bool = False) -> bytes:
    """A minimal workbook: shared strings (PT2020/PRR files) or inline strings (PT2030)."""
    shared: dict[str, int] = {}
    lines: list[str] = []
    for number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for index, value in enumerate(row):
            if value is None or value == "":
                continue
            reference = f"{_column(index)}{number}"
            if not isinstance(value, str):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            elif inline:
                cells.append(
                    f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                )
            else:
                position = shared.setdefault(value, len(shared))
                cells.append(f'<c r="{reference}" t="s"><v>{position}</v></c>')
        lines.append(f'<row r="{number}">{"".join(cells)}</row>')
    sheet = f'<worksheet xmlns="{MAIN}"><sheetData>{"".join(lines)}</sheetData></worksheet>'
    workbook = (
        f'<workbook xmlns="{MAIN}" xmlns:r="{RELATIONSHIPS}"><sheets>'
        '<sheet name="sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    links = [
        f'<Relationship Id="rId1" Type="{RELATIONSHIPS}/worksheet" '
        f'Target="{"/xl/worksheets/sheet1.xml" if inline else "worksheets/sheet1.xml"}"/>'
    ]
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as bundle:
        if not inline:
            links.append(
                f'<Relationship Id="rId2" Type="{RELATIONSHIPS}/sharedStrings" '
                'Target="sharedStrings.xml"/>'
            )
            items = "".join(f"<si><t>{escape(text)}</t></si>" for text in shared)
            bundle.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{items}</sst>')
        bundle.writestr("xl/workbook.xml", workbook)
        bundle.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{PACKAGE}">{"".join(links)}</Relationships>',
        )
        bundle.writestr("xl/worksheets/sheet1.xml", sheet)
    return content.getvalue()


class FakeDados:
    """Fictional dados.gov.pt API and resources: ``{slug: [(title, file name, content)]}``."""

    def __init__(self, datasets: dict[str, list[tuple[str, str, bytes]]]) -> None:
        self.api: dict[str, bytes] = {}
        self.files: dict[str, bytes] = {}
        for slug, resources in datasets.items():
            listed = []
            for title, filename, content in resources:
                url = f"https://dados.gov.pt/s/resources/{slug}/20260915-134301-da7d0274/{filename}"
                self.files[url] = content
                listed.append({"title": title, "format": "xlsx", "url": url})
            self.api[f"https://dados.gov.pt/api/1/datasets/{slug}/"] = json.dumps(
                {"slug": slug, "resources": listed}
            ).encode()

    def download(self, url: str, **request: object) -> bytes:
        return self.api[url] if url in self.api else self.files[url]


def run(fake: FakeDados, programme: str, *, as_of: date = DAY, apply: bool = True) -> str:
    output = StringIO()
    with patch("ligacoes.core.eu_funds.download", side_effect=fake.download):
        call_command(
            "import_eu_funds",
            "--programme",
            programme,
            *(["--apply"] if apply else []),
            as_of=as_of,
            stdout=output,
        )
    return output.getvalue()


def parties(record_id: str) -> set[tuple[str, str]]:
    return set(
        EventParty.objects.filter(event__record_id=record_id).values_list("role", "identifier")
    )


def classified(nipc: str) -> tuple[str, str]:
    entity = SourceIdentity.objects.get(source="nipc", external_id=nipc).entity
    return entity.kind, entity.classification


def assert_no_person_stored(output: str) -> None:
    stored = [
        output,
        *Entity.objects.values_list("name", flat=True),
        *Entity.objects.values_list("slug", flat=True),
        *SourceIdentity.objects.values_list("external_id", flat=True),
        *EventParty.objects.values_list("name", flat=True),
        *EventParty.objects.values_list("identifier", flat=True),
        *Event.objects.values_list("title", flat=True),
        *(
            json.dumps(details, ensure_ascii=False)
            for details in Event.objects.values_list("details", flat=True)
        ),
    ]
    for person in PERSONS:
        assert not any(person in text for text in stored), person


# Portugal 2020 --------------------------------------------------------------------

PT2020_OPERATIONS = [
    "Data",
    "Código da Operação",
    "Designação da Operação",
    "Resumo da Operação",
    "Designação do Programa Operacional da Operação",
    "Sigla do Fundo da Operação",
    "Designação Domínio Temático da Operação",
    "Estado da Operação",
    "Natureza do Investimento da Operação",
    "NIF Beneficiário Principal da Operação",
    "Beneficiário Principal da Operação",
    "Apoio Total Aprovado - Em Vigor",
    "Apoio Executado",
    "Data Prevista Conclusão",
    "Data Efetiva Conclusão da Operação",
    "Enquadramento",
]
PT2020_BENEFICIARIES = [
    "Data",
    "Código da Operação",
    "NIF das Entidades Beneficiárias",
    "Designação da Entidade",
    "Percentagem Beneficiário da operação",
    "Principal",
    "Enquadramento",
]
OP1 = "ALGARVE-01-0145-FEDER-000001"
OP2 = "ALGARVE-02-REACT-000002"
OP3 = "ALGARVE-03-FSE-000003"
OP4 = "ALGARVE-04-FEDER-000004"


def pt2020_operation(
    code: str,
    *,
    title: str,
    nif: str,
    name: str,
    approved: Cell,
    planned_end: Cell = 46068,
    actual_end: Cell = None,
) -> list[Cell]:
    return [
        46068,
        code,
        title,
        "Resumo fictício da operação.",
        "Programa Operacional Fictício do Algarve",
        "FEDER",
        "Competitividade e Internacionalização",
        "Encerrada / Concluída",
        "Investimento produtivo",
        # The operations sheet right-pads tax numbers with spaces.
        nif.ljust(50),
        name,
        approved,
        Decimal("20000"),
        planned_end,
        actual_end,
        "PT2020",
    ]


def pt2020_beneficiary(code: str, nif: str, name: str, principal: str) -> list[Cell]:
    return [46068, code, nif, name, 50, principal, "PT2020"]


def pt2020(operations: list[list[Cell]], beneficiaries: list[list[Cell]]) -> FakeDados:
    slug = "datasets-do-portugal-2020"
    return FakeDados(
        {
            slug: [
                (
                    "Lista de Projetos Aprovados MAR 2020",
                    "06-lista-operacoes-mar2020-relat-finais-15022026.xlsx",
                    xlsx([PT2020_OPERATIONS]),
                ),
                (
                    "Lista de Projetos Aprovados do Portugal 2020",
                    "01-lista-de-operacoes-pt2020-relat-finais-15022026.xlsx",
                    xlsx([PT2020_OPERATIONS, *operations, []]),
                ),
                (
                    "Lista de Beneficiários do Portugal 2020",
                    "14-lista-de-beneficiarios-pt2020-relat-finais-15022026.xlsx",
                    xlsx([PT2020_BENEFICIARIES, *beneficiaries]),
                ),
            ]
        }
    )


def test_pt2020_joins_beneficiaries_drops_pseudonymised_persons_and_publishes():
    fake = pt2020(
        [
            pt2020_operation(
                OP1,
                title="Projeto fictício de inovação",
                nif=NIPC_A,
                name="Companhia Fictícia Alfa, Lda.",
                approved=Decimal("21824.799999999999"),
                actual_end=45000,
            ),
            pt2020_operation(
                OP2,
                title="Apoio à liquidez de Rui Fictício Pardal",
                nif="AA123456",
                name="Rui Fictício Pardal",
                approved=5000,
            ),
            pt2020_operation(
                OP3,
                title="Formação fictícia",
                nif="AA654321",
                name="Maria Fictícia Lopes",
                approved=15000,
            ),
            # No beneficiary rows: the principal of the operations sheet is used.
            pt2020_operation(
                OP4,
                title="Requalificação fictícia",
                nif=NIPC_F,
                name="Agrupamento de Escolas Fictício",
                approved=7000,
                planned_end=44500,
            ),
        ],
        [
            pt2020_beneficiary(OP1, NIPC_A, "Companhia Fictícia Alfa, Lda.", "SIM"),
            pt2020_beneficiary(OP1, NIPC_C, "Universidade Fictícia do Sul", "NÃO"),
            pt2020_beneficiary(OP1, "234567899", "Joana Fictícia Reis", "NÃO"),
            pt2020_beneficiary(OP2, "AA123456", "Rui Fictício Pardal", "SIM"),
            pt2020_beneficiary(OP3, "AA654321", "Maria Fictícia Lopes", "SIM"),
            pt2020_beneficiary(OP3, NIPC_B, "Associação Fictícia de Desenvolvimento", "NÃO"),
        ],
    )
    dry = run(fake, "pt2020", apply=False)
    assert "Sem escritas" in dry
    assert "3 eventos com entidade coletiva" in dry
    assert not Event.objects.exists() and not Entity.objects.exists()

    output = run(fake, "pt2020")
    assert set(Event.objects.values_list("record_id", "status")) == {
        (OP1, "published"),
        (OP3, "published"),
        (OP4, "published"),
    }
    assert public_events().count() == 3
    first = Event.objects.get(record_id=OP1)
    assert (first.kind, first.dataset, first.scope) == (
        "eu_funding",
        "pt2020_beneficiarios",
        "pt2020",
    )
    assert first.source.dataset == "pt2020_beneficiarios"
    assert (first.amount, first.amount_label) == (Decimal("21824.80"), "Apoio aprovado")
    # Actual completion (Excel serial 45000) wins over the planned one; no approval date.
    assert (first.date, first.start_date, first.end_date) == (None, None, date(2023, 3, 15))
    assert first.details["programme"] == "Programa Operacional Fictício do Algarve"
    assert first.details["lead_beneficiary"] == f"nipc:{NIPC_A}"
    assert (first.details["executed"], first.details["end_basis"]) == ("20000.00", "efetiva")
    assert parties(OP1) == {("beneficiary", f"nipc:{NIPC_A}"), ("beneficiary", f"nipc:{NIPC_C}")}
    # A pseudonymised principal leaves its legal-person partner as the only party.
    assert parties(OP3) == {("beneficiary", f"nipc:{NIPC_B}")}
    assert "lead_beneficiary" not in Event.objects.get(record_id=OP3).details
    fallback = Event.objects.get(record_id=OP4)
    assert (fallback.end_date, fallback.details["end_basis"]) == (date(2021, 10, 31), "prevista")
    assert parties(OP4) == {("beneficiary", f"nipc:{NIPC_F}")}
    # Operations that also named a natural person lose their free-text title.
    assert dict(Event.objects.values_list("record_id", "title")) == {
        OP1: f"Portugal 2020: operação {OP1}",
        OP3: f"Portugal 2020: operação {OP3}",
        OP4: "Requalificação fictícia",
    }
    assert classified(NIPC_A) == ("company", "company")
    assert classified(NIPC_B) == ("organisation", "association")
    assert classified(NIPC_C) == ("university", "higher_education")
    assert classified(NIPC_F) == ("organisation", "public_body")
    assert Entity.objects.count() == 4
    assert_no_person_stored(dry + output)


# PRR ------------------------------------------------------------------------------

PRR_PROJECTS = [
    "cd_projeto",
    "dt_referencia",
    "ds_projeto",
    "sumario",
    "valor_aprovado",
    "valor_pago",
    "subvencoes",
    "emprestimos",
    "nota_final_candidatura",
    "cd_investimento",
    "dt_inicio",
    "dt_prevista_conclusao",
    "dt_efetiva_conclusao",
]
PRR_ENTITIES = [
    "cd_entidade",
    "dt_referencia",
    "nif_entidade",
    "ds_entidade",
    "papel_entidade",
    "atividade_economica",
    "localizacao_sede",
    "valor_contratado",
    "valor_pago",
    "cd_projeto",
]


def prr_entity(project: str, nif: str, name: str, role: str) -> list[Cell]:
    return [nif, "2026-09-28", nif, name, role, 85310, "Vila Fictícia", 1000, 500, project]


def prr(projects: list[list[Cell]], entities: list[list[Cell]]) -> FakeDados:
    return FakeDados(
        {
            "dataset-estrutura-de-missao-prr-projetos-2": [
                (
                    "listagem-de-projetos-prr-20260928.xlsx",
                    "listagem-de-projetos-prr-20260928.xlsx",
                    xlsx([PRR_PROJECTS, *projects]),
                )
            ],
            "dataset-estrutura-de-missao-prr-entidades-1": [
                (
                    "listagem-de-entidades-prr-20260928.xlsx",
                    "listagem-de-entidades-prr-20260928.xlsx",
                    xlsx([PRR_ENTITIES, *entities]),
                )
            ],
        }
    )


def prr_project(code: str, title: str, approved: Cell, dates: tuple[str, str, str]) -> list[Cell]:
    start, planned, actual = dates
    return [
        code,
        "2026-09-28",
        title,
        "Sumário fictício.",
        approved,
        720,
        approved,
        0,
        "12.00",
        "C03-i06.01",
        start,
        planned,
        actual,
    ]


P1 = "01/C03-i06.01/2021.P1"
P2 = "05/C05-i01/2022.P2"
P3 = "01/C03-i06.01/2021.P3"
P4 = "02/C11-i01/2023.P4"


def test_prr_keeps_intermediaries_and_suppliers_and_drops_pseudonymised_final_beneficiaries():
    fake = prr(
        [
            prr_project(
                P1,
                "Apoio fictício à digitalização escolar",
                1200,
                ("2024-09-25", "2025-12-31", ""),
            ),
            prr_project(
                P2,
                "Equipamento fictício",
                Decimal("262.26"),
                ("2024-12-23", "2026-12-31", "2025-02-08"),
            ),
            prr_project(P3, "Apoio fictício individual", 300, ("2024-01-10", "2024-12-31", "")),
            # A title redacted by the source itself.
            prr_project(P4, "RGPD", 50, ("2024-02-01", "2024-06-30", "")),
        ],
        [
            prr_entity(P1, "12345678", "RGPD", "Beneficiário Final"),
            prr_entity(P1, NIPC_F, "Agrupamento de Escolas Fictício", "Beneficiário Intermediário"),
            prr_entity(P2, NIPC_A, "Companhia Fictícia Alfa, Lda.", "Beneficiário Direto"),
            prr_entity(P2, NIPC_D, "Fornecedora Fictícia, S.A.", "Fornecedor"),
            prr_entity(P2, NIPC_E, "Cooperativa Agrícola Fictícia, CRL", "Beneficiário Final"),
            prr_entity(P3, "87654321", "RGPD", "Beneficiário Final"),
            prr_entity(P4, NIPC_D, "Fornecedora Fictícia, S.A.", "Beneficiário Direto"),
        ],
    )
    output = run(fake, "prr")
    assert set(public_events().values_list("record_id", "title")) == {
        (P1, f"PRR: projeto {P1}"),
        (P2, "Equipamento fictício"),
        (P4, f"PRR: projeto {P4}"),
    }
    assert Event.objects.count() == 3
    assert parties(P1) == {("intermediary", f"nipc:{NIPC_F}")}
    assert parties(P2) == {
        ("beneficiary", f"nipc:{NIPC_A}"),
        ("supplier", f"nipc:{NIPC_D}"),
        ("beneficiary", f"nipc:{NIPC_E}"),
    }
    project = Event.objects.get(record_id=P2)
    assert (project.dataset, project.scope) == ("prr_entidades", "prr")
    assert (project.amount, project.amount_label) == (Decimal("262.26"), "Valor aprovado")
    assert (project.start_date, project.end_date) == (date(2024, 12, 23), date(2025, 2, 8))
    assert project.details["investment"] == "C03-i06.01"
    assert project.details["end_basis"] == "efetiva" and "start_basis" not in project.details
    school = Event.objects.get(record_id=P1)
    assert (school.end_date, school.details["end_basis"]) == (date(2025, 12, 31), "prevista")
    assert classified(NIPC_E) == ("organisation", "cooperative")
    assert classified(NIPC_F) == ("organisation", "public_body")
    assert_no_person_stored(output)


@pytest.mark.parametrize(
    ("entities_header", "role"),
    [
        (PRR_ENTITIES, "Parceiro Fictício"),
        ([*PRR_ENTITIES[:4], "papel", *PRR_ENTITIES[5:]], "Fornecedor"),
    ],
)
def test_unknown_role_or_missing_column_fails_without_writing(entities_header, role):
    fake = prr([prr_project(P2, "Equipamento fictício", 100, ("2024-12-23", "", ""))], [])
    url = next(url for url in fake.files if "entidades" in url)
    fake.files[url] = xlsx(
        [entities_header, prr_entity(P2, NIPC_A, "Companhia Fictícia Alfa, Lda.", role)]
    )
    with pytest.raises(CommandError, match="PRR"):
        run(fake, "prr")
    assert not Event.objects.exists() and not Entity.objects.exists()


# Portugal 2030 --------------------------------------------------------------------

PT2030_OPERATIONS = [
    "Data",
    "Código da Operação",
    "Nome da Operação",
    "Finalidade da Operação",
    "Designação do Programa",
    "Designação do Objetivo Específico",
    "Nif Beneficiário",
    "Nome do Beneficiário",
    "Sigla do Fundo",
    "Custo Total Elegível Aprovado",
    "Fundo Aprovado",
    "Fundo Executado",
    "Fundo Pago",
    "Designação do estado da Operação",
    "Data de Inicio Prevista",
    "Data de Inicio Efetiva",
    "Data de Conclusão Prevista",
    "Data de Conclusão Efetiva",
]
PT2030_ENTITIES = [
    "Data",
    "Código da Operação",
    "Nif entidade",
    "Designação da entidade",
    "Papel da entidade",
    "Percentagem Beneficiário na Operação",
    "Valor contratualizado",
    "Fundo Aprovado",
    "Fundo Executado",
    "Fundo Pago",
    "Enquadramento",
]


def pt2030_operation(code: str, nif: str, name: str, *, actual_start: Cell = None) -> list[Cell]:
    return [
        46265,
        code,
        f"Operação fictícia {code}",
        "Finalidade fictícia.",
        "Programa Fictício Inovação",
        "Reforçar a competitividade fictícia.",
        nif,
        name,
        "FEDER",
        Decimal("9890152.07"),
        Decimal("3956060.83"),
        Decimal("2180394.29"),
        Decimal("3697893.93"),
        "Em Execução",
        45075,
        actual_start,
        46068,
        None,
    ]


def pt2030(operations: list[list[Cell]], entities: list[list[Cell]]) -> FakeDados:
    return FakeDados(
        {
            "datasets-pt2030-03-lista-de-operacoes-pt2030": [
                (
                    "03-datasets-operacoes-pt2030-31082026.xlsx",
                    "03-datasets-operacoes-pt2030-31082026.xlsx",
                    xlsx([PT2030_OPERATIONS, *operations], inline=True),
                )
            ],
            "datasets-pt2030-05-lista-de-entidades-pt2030": [
                (
                    "05-datasets-entidades-pt2030-31082026.xlsx",
                    "05-datasets-entidades-pt2030-31082026.xlsx",
                    xlsx([PT2030_ENTITIES, *entities], inline=True),
                )
            ],
        }
    )


def pt2030_entity(code: str, nif: str, name: str, role: str) -> list[Cell]:
    return [46265, code, nif, name, role, 100, 1000, 400, 200, 100, "PT2030"]


O1 = "COMPETE2030-FEDER-00000100"
O2 = "NORTE2030-FSE+-00000200"


def test_pt2030_removed_operation_ceases_and_existing_organisation_is_kept():
    existing = official_entity(
        "nipc",
        NIPC_A,
        name="Nome Editorial Fictício",
        kind="organisation",
        classification="foundation",
    )
    first = pt2030_operation(O1, NIPC_A, "Companhia Fictícia Alfa, Lda.", actual_start=46265)
    second = pt2030_operation(O2, NIPC_B, "Serviços Fictícios Beta, Lda.")
    entities = [
        pt2030_entity(O1, NIPC_A, "Companhia Fictícia Alfa, Lda.", "Beneficiário Principal"),
        pt2030_entity(O1, "A12345678", "Carla Fictícia Mota", "Outros Beneficiários"),
        pt2030_entity(O2, NIPC_B, "Serviços Fictícios Beta, Lda.", "Beneficiário Principal"),
    ]
    run(pt2030([first, second], entities), "pt2030")
    assert set(Event.objects.values_list("record_id", "status")) == {
        (O1, "published"),
        (O2, "published"),
    }
    event = Event.objects.get(record_id=O1)
    assert (event.dataset, event.scope) == ("pt2030_operacoes", "pt2030")
    assert (event.amount, event.amount_label) == (Decimal("3956060.83"), "Fundo aprovado")
    assert event.details["paid"] == "3697893.93"
    assert event.title == f"Portugal 2030: operação {O1}"
    assert Event.objects.get(record_id=O2).title == f"Operação fictícia {O2}"
    # The actual start (2026-08-31) follows the stale planned end (2026-02-15): end omitted.
    assert (event.start_date, event.end_date) == (date(2026, 8, 31), None)
    assert event.details["start_basis"] == "efetiva" and "end_basis" not in event.details
    assert event.parties.get().entity == existing
    existing.refresh_from_db()
    assert (existing.name, existing.kind, existing.classification) == (
        "Nome Editorial Fictício",
        "organisation",
        "foundation",
    )

    output = run(pt2030([first], entities[:2]), "pt2030", as_of=LATER)
    assert "cessados=1" in output
    assert set(Event.objects.values_list("record_id", "status")) == {
        (O1, "published"),
        (O2, "ceased"),
    }
    assert list(public_events().values_list("record_id", flat=True)) == [O1]
    assert_no_person_stored(output)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("46068", date(2026, 2, 15)),
        ("46068.0", date(2026, 2, 15)),
        ("1", date(1899, 12, 31)),
        ("0", None),
        ("2025-02-08", date(2025, 2, 8)),
        ("08/02/2025", date(2025, 2, 8)),
        ("", None),
        ("31/02/2025", None),
    ],
)
def test_excel_serial_and_text_dates(value, expected):
    assert eu_funds.day(value, Counter()) == expected
