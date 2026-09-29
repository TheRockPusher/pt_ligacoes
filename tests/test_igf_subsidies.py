import io
import zipfile
from collections import Counter
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from xml.sax.saxutils import escape

import pytest
from django.core.management import call_command

from ligacoes.core import igf_subsidies
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Event, EventParty, SourceIdentity
from ligacoes.core.parliament_parse import JSONValue

DAY = date(2026, 5, 1)
LATER = date(2026, 6, 1)
# Fictional legal-person NIPCs with valid check digits; one fictional personal NIF.
GRANTOR = "600000117"
COMPANY = "510000010"
ASSOCIATION = "510000029"
DONEE = "510000037"
PERSON_NIF = "123456789"
PERSON_NAME = "Joana Exemplo Fictícia"

Cell = tuple[str, str] | str | None

NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
)


def _cell(value: Cell) -> str:
    if value is None:
        return "<table:table-cell/>"
    if isinstance(value, tuple):
        kind, raw = value
        if kind == "date":
            return (
                f'<table:table-cell office:value-type="date" office:date-value="{raw}">'
                "<text:p>display</text:p></table:table-cell>"
            )
        return (
            f'<table:table-cell office:value-type="{kind}" office:value="{raw}">'
            "<text:p>display</text:p></table:table-cell>"
        )
    return f'<table:table-cell office:value-type="string"><text:p>{escape(value)}</text:p></table:table-cell>'


def _row(cells: list[Cell], repeated: int = 1) -> str:
    attribute = f' table:number-rows-repeated="{repeated}"' if repeated > 1 else ""
    trailing = '<table:table-cell table:number-columns-repeated="1014"/>'
    return f"<table:table-row{attribute}>{''.join(_cell(c) for c in cells)}{trailing}</table:table-row>"


def ods(rows: list[str]) -> bytes:
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {NS}><office:body>'
        f'<office:spreadsheet><table:table table:name="Subv">{"".join(rows)}'
        '<table:table-row table:number-rows-repeated="1048000"><table:table-cell '
        'table:number-columns-repeated="1024"/></table:table-row>'
        "</table:table></office:spreadsheet></office:body></office:document-content>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", xml)
    return buffer.getvalue()


def header(*, donations: bool = False) -> list[str]:
    return [
        _row(
            [
                "NIF (EP)" if donations else "NIF (EO)",
                "ENTIDADE PÚBLICA (EP)" if donations else "ENTIDADE OBRIGADA (EO)",
                "NIF (B)",
                "BENEFICIÁRIO (B)",
                "VALOR PATRIMONIAL ESTIMADO (EM EUROS)"
                if donations
                else "MONTANTE TRANSFERIDO OU\nBENEFÍCIO AUFERIDO (euros)",
                "DATA DO ATO" if donations else "DATA DA DECISÃO",
                "FINALIDADE",
                "FUNDAMENTO LEGAL",
            ]
        ),
        _row([None, None, None, None, None, None, None, "TIPO DE ATO", "N.º", "DATA"]),
    ]


def preamble() -> list[str]:
    return [
        _row(["LISTAGEM DAS SUBVENÇÕES E OUTROS BENEFÍCIOS PÚBLICOS (ANO 2024)"]),
        _row(["a) Nota fictícia sobre a origem dos dados."]),
        _row([None]),
    ]


def subsidy(
    beneficiary: str,
    name: str,
    *,
    value: str = "1500.5",
    decided: Cell = ("date", "2024-03-15T00:00:00"),
    purpose: str = "Apoio à modernização fictícia",
    repeated: int = 1,
) -> str:
    nif: Cell = ("float", beneficiary) if beneficiary.isdecimal() else beneficiary
    return _row(
        [
            ("float", GRANTOR),
            "Instituto Fictício de Apoio, I.P.",
            nif,
            name,
            ("currency", value),
            decided,
            purpose,
            "Decreto-Lei",
            ("float", "12.0"),
            ("date", "2020-01-10T00:00:00"),
        ],
        repeated,
    )


SUBSIDIES = [
    *preamble(),
    *header(),
    subsidy(COMPANY, "Empresa Fictícia, Lda."),
    subsidy(PERSON_NIF, PERSON_NAME, purpose=f"Bolsa atribuída a {PERSON_NAME}"),
    subsidy("E-FICT123", "Pessoa Estrangeira Fictícia"),
    subsidy(
        ASSOCIATION,
        "Associação Cultural Fictícia",
        value="2500",
        decided="05/06/2024",
        repeated=2,
    ),
]


def run(content: bytes) -> dict[str, int]:
    return igf_subsidies.apply_file(content, year=2024, category=igf_subsidies.SUBSIDIES, as_of=DAY)


def test_ods_rows_expand_repeats_skip_empty_and_read_values() -> None:
    rows = list(igf_subsidies.ods_rows(ods(SUBSIDIES)))

    assert [number for number, _ in rows] == [1, 2, 4, 5, 6, 7, 8, 9, 10]
    first = rows[4][1]
    assert first[0] == GRANTOR
    assert first[4] == "1500.5"
    assert first[5] == "2024-03-15T00:00:00"
    assert len(first) == igf_subsidies.COLUMNS
    assert rows[7][1] == rows[8][1]


def test_grants_drop_natural_persons_and_normalise_values() -> None:
    stats: Counter[str] = Counter()
    grants = list(igf_subsidies.grants(ods(SUBSIDIES), stats))

    assert stats["rows"] == 5
    assert stats["dropped_persons"] == 2
    assert [grant.beneficiary_nipc for grant in grants] == [COMPANY, ASSOCIATION, ASSOCIATION]
    company = grants[0]
    assert company.grantor_nipc == GRANTOR
    assert company.amount == Decimal("1500.50")
    assert company.decided_on == date(2024, 3, 15)
    assert company.basis_number == "12"
    assert company.basis_date == date(2020, 1, 10)
    assert grants[1].amount == Decimal("2500.00")
    assert grants[1].decided_on == date(2024, 6, 5)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("500051054", "500051054"),
        ("500051054.0", "500051054"),
        ("5.00051054E8", "500051054"),
        ("12345678", "012345678"),
        ("E-ESB123", "E-ESB123"),
        ("", ""),
    ],
)
def test_nif_cells_are_normalised(value: str, expected: str) -> None:
    assert igf_subsidies.nif(value) == expected


def test_amount_and_date_formats() -> None:
    assert igf_subsidies.amount("1.234,56") == Decimal("1234.56")
    assert igf_subsidies.amount("0.01") == Decimal("0.01")
    assert igf_subsidies.amount("-5") is None
    assert igf_subsidies.amount("n/d") is None
    assert igf_subsidies.day("31/12/2021") == date(2021, 12, 31)
    assert igf_subsidies.day("31/02/2021") is None
    assert igf_subsidies.day("") is None


def test_missing_header_is_refused() -> None:
    with pytest.raises(igf_subsidies.IgfError):
        list(igf_subsidies.grants(ods(preamble()), Counter()))


@pytest.mark.django_db
def test_apply_publishes_legal_person_subsidies_without_personal_data() -> None:
    result = run(ods(SUBSIDIES))

    assert result["created"] == 3
    assert result["published"] == 3
    events = Event.objects.filter(dataset="igf_subvencoes", scope="2024")
    assert set(events.values_list("status", flat=True)) == {Event.Status.PUBLISHED}
    company_event = events.get(parties__identifier=f"nipc:{COMPANY}")
    assert company_event.kind == Event.Kind.SUBSIDY
    assert company_event.amount == Decimal("1500.50")
    assert company_event.amount_label == "Montante"
    assert company_event.date == date(2024, 3, 15)
    assert company_event.title == "Apoio à modernização fictícia"
    assert company_event.details == {
        "category": "Subvenção ou benefício público",
        "legal_basis_type": "Decreto-Lei",
        "legal_basis_number": "12",
        "legal_basis_date": "2020-01-10",
    }
    roles = dict(company_event.parties.values_list("role", "identifier"))
    assert roles == {
        EventParty.Role.GRANTOR: f"nipc:{GRANTOR}",
        EventParty.Role.BENEFICIARY: f"nipc:{COMPANY}",
    }
    grantor = Entity.objects.get(sourceidentity__external_id=GRANTOR)
    assert (grantor.kind, grantor.classification) == ("organisation", "public_body")
    association = Entity.objects.get(sourceidentity__external_id=ASSOCIATION)
    assert association.classification == "association"
    company = Entity.objects.get(sourceidentity__external_id=COMPANY)
    assert (company.kind, company.classification) == ("company", "company")

    stored = [
        *Event.objects.values_list("title", "record_id", "details"),
        *EventParty.objects.values_list("name", "identifier"),
        *Entity.objects.values_list("name", "slug"),
        *SourceIdentity.objects.values_list("external_id", flat=True),
    ]
    assert PERSON_NIF not in str(stored)
    assert PERSON_NAME not in str(stored)
    assert "FICT123" not in str(stored)


@pytest.mark.django_db
def test_existing_entities_keep_their_classification() -> None:
    official_entity(
        "nipc",
        COMPANY,
        name="Nome Oficial Fictício",
        kind="organisation",
        classification="foundation",
    )
    run(ods(SUBSIDIES))

    entity = Entity.objects.get(sourceidentity__external_id=COMPANY)
    assert (entity.name, entity.classification) == ("Nome Oficial Fictício", "foundation")


@pytest.mark.django_db
def test_removed_row_ceases_on_the_next_complete_file() -> None:
    run(ods(SUBSIDIES))
    remaining = [row for row in SUBSIDIES if "Empresa Fictícia" not in row]

    result = igf_subsidies.apply_file(
        ods(remaining), year=2024, category=igf_subsidies.SUBSIDIES, as_of=LATER
    )

    assert result["ceased"] == 1
    assert result["unchanged"] == 2
    ceased = Event.objects.get(status=Event.Status.CEASED)
    assert ceased.parties.filter(identifier=f"nipc:{COMPANY}").exists()


@pytest.mark.django_db
def test_donations_use_their_own_scope_and_category() -> None:
    content = ods(
        [
            *header(donations=True),
            _row(
                [
                    GRANTOR,
                    "Município de Vila Fictícia",
                    ("float", DONEE),
                    "Fundação Fictícia",
                    ("float", "0.01"),
                    "15/09/2025",
                    "Doação de equipamento fictício",
                ]
            ),
            _row(
                [
                    ("float", GRANTOR),
                    "Município de Vila Fictícia",
                    ("float", PERSON_NIF),
                    PERSON_NAME,
                    ("float", "1"),
                    "16/09/2025",
                    "Doação",
                ]
            ),
        ]
    )
    run(ods(SUBSIDIES))

    result = igf_subsidies.apply_file(
        content, year=2025, category=igf_subsidies.DONATIONS, as_of=DAY
    )

    assert (result["created"], result["dropped_persons"]) == (1, 1)
    donation = Event.objects.get(scope="2025-doacoes")
    assert donation.status == Event.Status.PUBLISHED
    assert donation.details == {"category": "Doação"}
    assert donation.amount == Decimal("0.01")
    assert donation.amount_label == "Valor patrimonial estimado"
    assert donation.date == date(2025, 9, 15)
    assert set(donation.parties.values_list("role", flat=True)) == {"grantor", "beneficiary"}
    # The donations list is its own complete scope: subsidies stay current.
    assert Event.objects.filter(scope="2024", status=Event.Status.PUBLISHED).count() == 3
    assert Entity.objects.get(sourceidentity__external_id=DONEE).classification == "foundation"


def test_resources_pick_lists_by_file_name() -> None:
    base = "https://dados.gov.pt/s/resources/lista-fict/20250401-101500"
    payload: JSONValue = {
        "resources": [
            {"url": f"{base}/registos.doacoes.ano.2024.ods", "title": "Subvenções"},
            {"url": f"{base}-ab12/registos.subvencoes.ano.2024.ods", "title": "Doações"},
            {"url": "https://example.org/registos.subvencoes.ano.2024.ods"},
        ]
    }

    resources = igf_subsidies.parse_resources(payload)

    assert [(r.category, r.filename) for r in resources] == [
        ("subsidies", "registos.subvencoes.ano.2024.ods"),
        ("donations", "registos.doacoes.ano.2024.ods"),
    ]


@pytest.mark.django_db
def test_command_dry_run_prints_counts_only() -> None:
    resource = igf_subsidies.Resource(
        "subsidies", "https://dados.gov.pt/s/resources/x/20250401-101500/s.ods", "s.ods"
    )
    out = StringIO()
    with (
        patch.object(igf_subsidies.Client, "resources", return_value=[resource]),
        patch.object(igf_subsidies.Client, "content", return_value=ods(SUBSIDIES)),
    ):
        call_command("import_igf_subsidies", "--year", "2024", "--as-of", "2026-05-01", stdout=out)

    text = out.getvalue()
    assert "pessoas singulares excluídas=2" in text
    assert "Simulação" in text
    assert PERSON_NAME not in text and PERSON_NIF not in text
    assert not Event.objects.exists()
