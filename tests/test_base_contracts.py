import io
import json
import zipfile
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ligacoes.core import base_contracts
from ligacoes.core.base_contracts import (
    BaseContractsError,
    Party,
    Resource,
    classify,
    parse_amount,
    parse_day,
    parse_party,
    parse_resources,
)
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Event, EventParty, SourceIdentity

COMMAND = "ligacoes.core.management.commands.import_base_contracts"
RESOURCE_URL = (
    "https://dados.gov.pt/s/resources/contratos-publicos-portal-base-impic-contratos-de-2012-"
    "a-2026/20260927-090503-6deefa7c/contratos2024.zip"
)
# Fictional legal-person NIPCs with valid check digits.
MUNICIPALITY = "600000117"
INSTITUTE = "600000125"
BUILDER = "510000010"
ENGINEERING = "510000029"
BIDDER = "510000037"
PERSON_NAME = "Tomé Fictício Arruda"
OTHER_PERSON = "Olga Fictícia Pires"


def record(identifier: str, **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "idcontrato": identifier,
        "tipoContrato": ["Empreitadas de obras públicas"],
        "tipoprocedimento": "Concurso público",
        "objectoContrato": f"Empreitada fictícia {identifier}",
        "descContrato": "Descrição fictícia",
        "adjudicante": [f"{MUNICIPALITY} - Município de Vila Fictícia"],
        "adjudicatarios": [f"{BUILDER} - Construtora Fictícia - Obras, Lda."],
        "dataPublicacao": "10/03/2024",
        "dataCelebracaoContrato": "01/03/2024",
        "precoContratual": 1000.0,
        "cpv": ["45000000-7 - Obras de construção"],
        "fundamentacao": "Artigo 20.º, n.º 1, alínea b) do Código dos Contratos Públicos",
        "dataDecisaoAdjudicacao": "15/02/2024",
        "dataFechoContrato": "",
        "PrecoTotalEfetivo": 0.0,
        "concorrentes": None,
        "Observacoes": "Observação fictícia",
        "Ano": 2024,
    }
    base.update(fields)
    return base


def archive(records: list[dict[str, object]], year: int = 2024) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"Contratos{year}.json", json.dumps(records, ensure_ascii=False, indent=1))
    return content.getvalue()


def run(records: list[dict[str, object]], *, apply: bool = True, as_of: str = "2026-09-01") -> str:
    payload = archive(records)
    output = StringIO()
    with (
        patch(
            f"{COMMAND}.fetch_resources",
            return_value={2024: Resource(year=2024, url=RESOURCE_URL, size=len(payload))},
        ),
        patch(f"{COMMAND}.fetch_archive", return_value=payload),
        # Tiny reads: records must be reassembled across chunk boundaries.
        patch("ligacoes.core.base_contracts.CHUNK_CHARS", 61),
    ):
        call_command(
            "import_base_contracts",
            "--year",
            "2024",
            "--as-of",
            as_of,
            *(["--apply"] if apply else []),
            stdout=output,
        )
    return output.getvalue()


def parties(record_id: str) -> set[tuple[str, str, str]]:
    return set(
        EventParty.objects.filter(event__record_id=record_id).values_list(
            "role", "identifier", "entity__sourceidentity__external_id"
        )
    )


@pytest.mark.parametrize(
    ("text", "role", "expected"),
    [
        (
            f"{BUILDER} - Construtora Fictícia - Obras, Lda.",
            "supplier",
            Party("supplier", BUILDER, "Construtora Fictícia - Obras, Lda."),
        ),
        (
            f"{ENGINEERING}-Engenharia Fictícia &amp; Filhos, S.A.",
            "bidder",
            Party("bidder", ENGINEERING, "Engenharia Fictícia & Filhos, S.A."),
        ),
        (
            "510 000 037 - Espaçada Fictícia, Lda.",
            "supplier",
            Party("supplier", BIDDER, "Espaçada Fictícia, Lda."),
        ),
        (
            f"{MUNICIPALITY}\t - Município de Vila Fictícia",
            "buyer",
            Party("buyer", MUNICIPALITY, "Município de Vila Fictícia"),
        ),
        (f"- - {PERSON_NAME}", "supplier", None),
        (f"--{PERSON_NAME.upper()}", "bidder", None),
        (f"123456789 - {PERSON_NAME}", "supplier", None),
        (f"453456789-{PERSON_NAME}", "bidder", None),
        ("ESB12345678 - Empresa Estrangeira Fictícia SL", "supplier", None),
        ("510000011 - Dígito de Controlo Errado, Lda.", "supplier", None),
        ("51000001 - Número Curto, Lda.", "supplier", None),
        (None, "supplier", None),
    ],
)
def test_party_strings_keep_only_legal_person_nipcs(text, role, expected):
    assert parse_party(text, role) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("8500.0"), Decimal("8500.00")),
        (Decimal("12571.555"), Decimal("12571.56")),
        (1500, Decimal("1500.00")),
        ("1.234,56 €", Decimal("1234.56")),
        ("99.5", Decimal("99.50")),
        (Decimal("0.0"), None),
        (Decimal("-10.00"), None),
        (Decimal("1E+16"), None),
        ("não indicado", None),
        (None, None),
        (True, None),
    ],
)
def test_amounts_are_cents_and_unreported_or_invalid_values_are_unknown(value, expected):
    assert parse_amount(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20/08/2020", date(2020, 8, 20)),
        ("2020-08-20", date(2020, 8, 20)),
        ("", None),
        ("31/02/2020", None),
        ("20-08-2020", None),
        (None, None),
    ],
)
def test_dates_come_only_from_valid_source_days(value, expected):
    assert parse_day(value) == expected


@pytest.mark.parametrize(
    ("name", "nipc", "buyer", "expected"),
    [
        ("Município de Vila Fictícia", MUNICIPALITY, True, ("organisation", "municipality")),
        ("JUNTA DE FREGUESIA DE ALDEIA FICTÍCIA", "600000125", True, ("organisation", "parish")),
        ("União das Freguesias de Alfa e Beta", "600000125", True, ("organisation", "parish")),
        ("Universidade Fictícia", "600000125", True, ("university", "higher_education")),
        ("Hospital Fictício, E.P.E.", "509000010", True, ("company", "state_company")),
        ("Direção-Geral Fictícia", "600000125", True, ("organisation", "public_body")),
        ("Associação Fictícia de Bombeiros", BUILDER, False, ("organisation", "association")),
        ("Fundação Fictícia", BUILDER, False, ("organisation", "foundation")),
        ("Adega Fictícia, C.R.L.", BUILDER, False, ("organisation", "cooperative")),
        ("Instituto Público Fictício", INSTITUTE, False, ("organisation", "public_body")),
        ("Construtora Fictícia, S.A.", BUILDER, False, ("company", "company")),
    ],
)
def test_new_organisations_are_classified_by_role_name_form_and_nipc(name, nipc, buyer, expected):
    assert classify(name, nipc=nipc, buyer=buyer) == expected


def test_year_resources_are_selected_by_title_on_the_official_host_only():
    other = RESOURCE_URL.replace("contratos2024", "contratos2023")
    payload = {
        "resources": [
            {"title": "contratos2024.xlsx", "url": RESOURCE_URL[:-3] + "xlsx", "filesize": 10},
            {"title": "contratos2024.zip", "url": RESOURCE_URL, "filesize": 10},
            {"title": "contratos2023.zip", "url": other, "filesize": 20},
        ]
    }
    assert parse_resources(payload) == {
        2024: Resource(2024, RESOURCE_URL, 10),
        2023: Resource(2023, other, 20),
    }
    foreign = RESOURCE_URL.replace("dados.gov.pt", "dados.example.org")
    with pytest.raises(BaseContractsError):
        parse_resources(
            {"resources": [{"title": "contratos2024.zip", "url": foreign, "filesize": 10}]}
        )
    # The title's year must match the file the URL serves.
    with pytest.raises(BaseContractsError):
        parse_resources(
            {"resources": [{"title": "contratos2023.zip", "url": RESOURCE_URL, "filesize": 10}]}
        )


@pytest.mark.django_db
def test_year_import_links_organisations_by_nipc_and_drops_natural_persons():
    consortium = record(
        "9000001",
        adjudicatarios=[
            f"{BUILDER} - Construtora Fictícia - Obras, Lda.",
            f"{ENGINEERING} - Engenharia Fictícia &amp; Filhos, S.A.",
        ],
        concorrentes=[
            f"{BUILDER}-Construtora Fictícia - Obras, Lda.",
            f"{BIDDER}-Concorrente Fictícia, Lda.",
            f"--{PERSON_NAME.upper()}",
        ],
        precoContratual=125000.5,
        dataFechoContrato="30/06/2024",
        PrecoTotalEfetivo=126000.0,
    )
    person_supplier = record("9000002", adjudicatarios=[f"- - {PERSON_NAME}"], precoContratual=0.0)
    only_persons = record(
        "9000003",
        adjudicante=[f"- - {OTHER_PERSON}"],
        adjudicatarios=[f"123456789 - {PERSON_NAME}"],
    )
    output = run([consortium, person_supplier, consortium, only_persons])

    assert set(Event.objects.values_list("record_id", "status", "scope")) == {
        ("9000001", "published", "2024"),
        ("9000002", "published", "2024"),
    }
    event = Event.objects.get(record_id="9000001")
    assert event.kind == "contract"
    assert event.title == "Empreitada fictícia 9000001"
    assert (event.date, event.start_date, event.end_date) == (
        date(2024, 3, 1),
        None,
        date(2024, 6, 30),
    )
    assert (event.amount, event.amount_label) == (Decimal("125000.50"), "Preço contratual")
    assert event.record_url == (
        "https://www.base.gov.pt/Base4/pt/detalhe/?type=contratos&id=9000001"
    )
    assert event.details == {
        "tipoContrato": ["Empreitadas de obras públicas"],
        "tipoProcedimento": "Concurso público",
        "cpv": ["45000000-7"],
        "dataPublicacao": "2024-03-10",
        "dataDecisaoAdjudicacao": "2024-02-15",
        "precoTotalEfetivo": "126000.00",
        "fundamentacao": "Artigo 20.º, n.º 1, alínea b) do Código dos Contratos Públicos",
    }
    assert parties("9000001") == {
        ("buyer", MUNICIPALITY, MUNICIPALITY),
        ("supplier", BUILDER, BUILDER),
        ("supplier", ENGINEERING, ENGINEERING),
        ("bidder", BUILDER, BUILDER),
        ("bidder", BIDDER, BIDDER),
    }
    assert EventParty.objects.get(event=event, role="supplier", identifier=ENGINEERING).name == (
        "Engenharia Fictícia & Filhos, S.A."
    )
    # The buyer remains a legal-person party once the natural-person supplier is dropped.
    assert parties("9000002") == {("buyer", MUNICIPALITY, MUNICIPALITY)}
    assert Event.objects.get(record_id="9000002").amount is None

    classified = {
        identity.external_id: (identity.entity.kind, identity.entity.classification)
        for identity in SourceIdentity.objects.select_related("entity").filter(source="nipc")
    }
    assert classified == {
        MUNICIPALITY: ("organisation", "municipality"),
        BUILDER: ("company", "company"),
        ENGINEERING: ("company", "company"),
        BIDDER: ("company", "company"),
    }
    stored = [
        *EventParty.objects.values_list("name", flat=True),
        *Entity.objects.values_list("name", flat=True),
    ]
    assert not any(
        PERSON_NAME.casefold() in name.casefold() or OTHER_PERSON in name for name in stored
    )
    assert "123456789" not in json.dumps(list(Event.objects.values_list("details", flat=True)))
    assert PERSON_NAME not in output and "123456789" not in output
    assert "1 repetições idênticas" in output
    assert "1 contratos sem pessoa coletiva identificada" in output


@pytest.mark.django_db
def test_existing_organisations_keep_their_identity_and_unpublished_ones_hold_events_in_draft():
    existing = official_entity(
        "nipc",
        MUNICIPALITY,
        name="Câmara Municipal de Vila Fictícia (SIOE)",
        kind="organisation",
        classification="public_body",
    )
    hidden = official_entity(
        "nipc",
        INSTITUTE,
        name="Instituto Fictício",
        kind="organisation",
        classification="public_body",
        public=False,
    )
    run(
        [
            record("9000011"),
            record("9000012", adjudicante=[f"{INSTITUTE} - Instituto Fictício, I.P."]),
        ]
    )
    statuses = dict(Event.objects.values_list("record_id", "status"))
    assert statuses == {"9000011": "published", "9000012": "draft"}
    existing.refresh_from_db()
    assert (existing.name, existing.kind, existing.classification) == (
        "Câmara Municipal de Vila Fictícia (SIOE)",
        "organisation",
        "public_body",
    )
    assert EventParty.objects.get(event__record_id="9000012", role="buyer").entity == hidden
    assert Entity.objects.filter(sourceidentity__external_id=MUNICIPALITY).count() == 1


@pytest.mark.django_db
def test_contract_missing_from_a_later_year_file_ceases():
    run([record("9000021"), record("9000022")])
    output = run([record("9000021")], as_of="2026-09-08")
    assert dict(Event.objects.values_list("record_id", "status")) == {
        "9000021": "published",
        "9000022": "ceased",
    }
    assert Event.objects.get(record_id="9000022").published_at is None
    assert "cessados=1" in output and "inalterados=1" in output


@pytest.mark.django_db
def test_dry_run_is_the_default_and_writes_nothing():
    output = run([record("9000031")], apply=False)
    assert "Sem escritas" in output
    assert not Event.objects.exists()
    assert not SourceIdentity.objects.filter(source="nipc").exists()


def test_malformed_or_foreign_year_archive_is_refused():
    with pytest.raises(BaseContractsError):
        list(base_contracts.contracts(archive([record("1")], year=2023), 2024))
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as bundle:
        bundle.writestr("Contratos2024.json", '[{"idcontrato": "1"}, {"idcontrato": ')
    with pytest.raises(BaseContractsError):
        list(base_contracts.contracts(broken.getvalue(), 2024))
