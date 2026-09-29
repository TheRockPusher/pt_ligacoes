import csv
import io
import json
import zipfile
from collections.abc import Sequence
from datetime import date
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.core.management import call_command

from ligacoes.core import gleif
from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Evidence,
    Relationship,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue

DAY = date(2026, 9, 1)
LATER = date(2026, 9, 15)
RR_URL = (
    "https://goldencopy.gleif.org/storage/golden-copy-files/2026/09/01/1/"
    "20260901-0800-gleif-goldencopy-rr-golden-copy.csv.zip"
)
PERSON_NIF = "123456789"
# Fictional legal-person NIPCs with valid check digits.
NIPC_A = "510000010"
NIPC_B = "510000029"
NIPC_C = "510000037"
NIPC_D = "510000045"
NIPC_E = "510000053"
NIPC_F = "600000117"
RR_HEADER = [
    "Relationship.StartNode.NodeID",
    "Relationship.StartNode.NodeIDType",
    "Relationship.EndNode.NodeID",
    "Relationship.EndNode.NodeIDType",
    "Relationship.RelationshipType",
    "Relationship.RelationshipStatus",
    *(
        f"Relationship.Period.{number}.{field}"
        for number in range(1, 6)
        for field in ("startDate", "endDate", "periodType")
    ),
    "Registration.RegistrationStatus",
]


def lei(number: int) -> str:
    return f"FICTLEI{number:011d}00"


def record(
    code: str,
    *,
    name: str,
    nipc: str | None,
    legal_form: str = "DFE5",
    category: str = "GENERAL",
    country: str = "PT",
    registry: str = "RA000487",
    status: str = "ISSUED",
) -> JSONObject:
    return {
        "type": "lei-records",
        "id": code,
        "attributes": {
            "lei": code,
            "entity": {
                "legalName": {"name": name, "language": "pt"},
                "legalAddress": {"country": country, "city": "Vila Fictícia"},
                "registeredAt": {"id": registry, "other": None},
                "registeredAs": nipc,
                "category": category,
                "legalForm": {"id": legal_form, "other": None},
                "status": "ACTIVE",
            },
            "registration": {"status": status},
        },
    }


def relation(
    child: str,
    parent: str,
    *,
    kind: str = "IS_DIRECTLY_CONSOLIDATED_BY",
    start: str = "2019-03-01T00:00:00+01:00",
    end: str = "",
    status: str = "ACTIVE",
) -> dict[str, str]:
    return {
        "Relationship.StartNode.NodeID": child,
        "Relationship.StartNode.NodeIDType": "LEI",
        "Relationship.EndNode.NodeID": parent,
        "Relationship.EndNode.NodeIDType": "LEI",
        "Relationship.RelationshipType": kind,
        "Relationship.RelationshipStatus": status,
        # An accounting period first: only the relationship period dates the claim.
        "Relationship.Period.1.startDate": "2024-01-01T00:00:00Z",
        "Relationship.Period.1.endDate": "2024-12-31T00:00:00Z",
        "Relationship.Period.1.periodType": "ACCOUNTING_PERIOD",
        "Relationship.Period.2.startDate": start,
        "Relationship.Period.2.endDate": end,
        "Relationship.Period.2.periodType": "RELATIONSHIP_PERIOD",
        "Registration.RegistrationStatus": "PUBLISHED",
    }


def archive(relations: list[dict[str, str]]) -> bytes:
    text = StringIO()
    writer = csv.DictWriter(text, fieldnames=RR_HEADER, restval="")
    writer.writeheader()
    writer.writerows(relations)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("20260901-0800-gleif-goldencopy-rr-golden-copy.csv", text.getvalue())
    return content.getvalue()


class FakeGleif:
    """Fictional API, golden-copy metadata and RR archive, paged two records at a time."""

    def __init__(
        self,
        records: Sequence[JSONObject],
        relations: Sequence[dict[str, str]] = (),
        others: Sequence[JSONObject] = (),
    ) -> None:
        self.records = list(records)
        self.others = list(others)
        self.archive = archive(list(relations))
        self.requested: list[str] = []

    def download(self, url: str, **request: object) -> bytes:
        if url == gleif.GOLDEN_URL:
            entry = {"url": RR_URL, "size": len(self.archive)}
            return json.dumps({"data": {"rr": {"full_file": {"csv": entry}}}}).encode()
        if url == RR_URL:
            return self.archive
        assert url.startswith(f"{gleif.API_URL}?")
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        if "filter[lei]" in query:
            wanted = query["filter[lei]"].split(",")
            self.requested.extend(wanted)
            found: list[JSONValue] = [
                item for item in self.records + self.others if item["id"] in wanted
            ]
            meta: JSONObject = {"pagination": {"total": len(found)}}
            return json.dumps({"meta": meta, "data": found}).encode()
        size = min(int(query["page[size]"]), 2)
        index = 0 if query["page[cursor]"] == "*" else int(query["page[cursor]"])
        data: list[JSONValue] = list(self.records[index : index + size])
        page: JSONObject = {
            "meta": {"pagination": {"total": len(self.records)}},
            "data": data,
            "links": {},
        }
        if index + size < len(self.records):
            following = {**query, "page[cursor]": str(index + size)}
            page["links"] = {"next": f"{gleif.API_URL}?{urlencode(following)}"}
        return json.dumps(page).encode()


def run(fake: FakeGleif, *, as_of: date = DAY, apply: bool = True, limit: int | None = None) -> str:
    output = StringIO()
    options: dict[str, object] = {"as_of": as_of, "stdout": output}
    if limit is not None:
        options["limit"] = limit
    with (
        patch("ligacoes.core.gleif.download", side_effect=fake.download),
        patch("ligacoes.core.gleif.REQUEST_INTERVAL", 0),
    ):
        call_command("import_gleif", *(["--apply"] if apply else []), **options)
    return output.getvalue()


def holder(scheme: str, external_id: str) -> Entity:
    return SourceIdentity.objects.get(source=scheme, external_id=external_id).entity


@pytest.mark.django_db
def test_natural_person_records_are_never_stored_requested_or_linked():
    company = record(lei(1), name="Companhia Fictícia Alfa, S.A.", nipc=NIPC_A)
    person = record(lei(2), name="Tomé Fictício Arruda", nipc=PERSON_NIF, legal_form="8888")
    trader = record(lei(3), name="Olga Fictícia Pires", nipc=NIPC_B, category="SOLE_PROPRIETOR")
    fake = FakeGleif(
        [company, person, trader],
        [relation(lei(1), lei(2)), relation(lei(3), lei(1)), relation(lei(1), lei(3))],
    )
    dry = run(fake, apply=False)
    assert "Sem escritas" in dry
    assert "2 registos excluídos" in dry
    assert not Entity.objects.exists()
    output = run(fake)
    assert PERSON_NIF not in dry + output
    assert Entity.objects.get() == holder("lei", lei(1))
    assert set(SourceIdentity.objects.values_list("source", "external_id")) == {
        ("lei", lei(1)),
        ("nipc", NIPC_A),
    }
    # Neither person is fetched as a parent nor linked as child or parent.
    assert fake.requested == []
    assert not SourceObservation.objects.exists()
    assert not Relationship.objects.exists()


@pytest.mark.django_db
def test_existing_nipc_entity_gains_the_lei_identity_without_a_duplicate():
    existing = official_entity(
        "nipc",
        NIPC_C,
        name="Nome Editorial Fictício",
        kind="organisation",
        classification="association",
    )
    fake = FakeGleif([record(lei(4), name="NOME GLEIF FICTICIO, S.A.", nipc=f"PT{NIPC_C}")])
    run(fake)
    run(fake, as_of=LATER)
    entity = holder("lei", lei(4))
    assert entity == existing
    assert Entity.objects.count() == 1
    assert SourceIdentity.objects.filter(entity=existing).count() == 2
    entity.refresh_from_db()
    # Importers never rename or reclassify an existing organisation.
    assert (entity.name, entity.kind, entity.classification) == (
        "Nome Editorial Fictício",
        "organisation",
        "association",
    )


@pytest.mark.django_db
def test_existing_lei_entity_gains_a_free_nipc_but_never_a_nipc_held_elsewhere():
    lei_only = official_entity(
        "lei", lei(5), name="Grupo Fictício Beta", kind="company", classification="company"
    )
    other = official_entity(
        "lei", lei(6), name="Grupo Fictício Gama", kind="company", classification="company"
    )
    elsewhere = official_entity(
        "nipc", NIPC_E, name="Gama registada", kind="company", classification="company"
    )
    output = run(
        FakeGleif(
            [
                record(lei(5), name="GRUPO FICTICIO BETA, SGPS, S.A.", nipc=NIPC_D),
                record(lei(6), name="GRUPO FICTICIO GAMA, S.A.", nipc=NIPC_E),
            ]
        )
    )
    assert holder("nipc", NIPC_D) == lei_only
    assert holder("nipc", NIPC_E) == elsewhere
    assert holder("lei", lei(6)) == other
    assert "conflitos LEI/NIPC por rever=1" in output
    assert Entity.objects.count() == 3


@pytest.mark.django_db
def test_new_organisations_hold_lei_and_nipc_and_are_classified_by_legal_form():
    run(
        FakeGleif(
            [
                record(lei(7), name="Sociedade Fictícia, Lda.", nipc=NIPC_A, legal_form="USOG"),
                record(lei(8), name="Fundação Fictícia", nipc=NIPC_B, legal_form="Z0NE"),
                record(lei(9), name="Hospital Fictício, E.P.E.", nipc=NIPC_C, legal_form="A8CT"),
                record(
                    lei(10),
                    name="Fundo Fictício",
                    nipc=NIPC_F,
                    legal_form="6L6P",
                    category="FUND",
                ),
            ]
        )
    )
    expected = {
        (lei(7), NIPC_A): ("Sociedade Fictícia, Lda.", "company", "company"),
        (lei(8), NIPC_B): ("Fundação Fictícia", "organisation", "foundation"),
        (lei(9), NIPC_C): ("Hospital Fictício, E.P.E.", "company", "state_company"),
        (lei(10), NIPC_F): ("Fundo Fictício", "organisation", "other"),
    }
    for (code, nipc), fields in expected.items():
        entity = holder("lei", code)
        assert holder("nipc", nipc) == entity
        assert (entity.name, entity.kind, entity.classification) == fields
        assert entity.is_public
    assert Entity.objects.count() == 4


@pytest.mark.django_db
def test_consolidation_parents_are_published_with_role_and_foreign_parent_is_keyed_by_lei():
    child = record(lei(11), name="Filial Fictícia, S.A.", nipc=NIPC_A)
    parent = record(lei(12), name="Holding Fictícia, SGPS, S.A.", nipc=NIPC_B)
    foreign = record(
        lei(13),
        name="Fiktive Konzern AG",
        nipc="HRB 000000",
        legal_form="XXXX",
        country="DE",
        registry="RA000999",
    )
    fake = FakeGleif(
        [child, parent],
        [
            relation(lei(11), lei(12)),
            relation(
                lei(11),
                lei(13),
                kind="IS_ULTIMATELY_CONSOLIDATED_BY",
                start="2015-06-01T00:00:00Z",
                end="2025-12-31T00:00:00Z",
            ),
            # Neither fund management nor an inactive relationship is consolidation.
            relation(lei(12), lei(13), kind="IS_FUND-MANAGED_BY"),
            relation(lei(12), lei(13), status="INACTIVE"),
        ],
        [foreign],
    )
    assert "2 relações de consolidação" in run(fake, apply=False)
    run(fake)
    subject = holder("lei", lei(11))
    direct = Relationship.objects.get(role="Consolidação contabilística direta")
    ultimate = Relationship.objects.get(role="Consolidação contabilística final")
    assert Relationship.objects.count() == 2
    assert (direct.subject, direct.object, direct.kind, direct.status) == (
        subject,
        holder("nipc", NIPC_B),
        "part_of",
        "published",
    )
    assert (direct.start_date, direct.end_date, direct.temporal_status) == (
        date(2019, 3, 1),
        None,
        "current",
    )
    assert (ultimate.subject, ultimate.status, ultimate.temporal_status) == (
        subject,
        "published",
        "ended",
    )
    assert (ultimate.start_date, ultimate.end_date) == (date(2015, 6, 1), date(2025, 12, 31))
    abroad = holder("lei", lei(13))
    assert ultimate.object == abroad
    assert (abroad.name, abroad.kind, abroad.classification) == (
        "Fiktive Konzern AG",
        "company",
        "company",
    )
    sources = SourceIdentity.objects.filter(entity=abroad).values_list("source", flat=True)
    assert list(sources) == ["lei"]
    evidence = Evidence.objects.get(relationship=direct)
    assert evidence.is_public and evidence.source.is_public
    assert evidence.source.url == f"https://search.gleif.org/#/record/{lei(11)}"
    assert evidence.source.dataset == "gleif_lei"
    assert lei(11) in evidence.page_reference and lei(12) in evidence.page_reference
    assert "IS_DIRECTLY_CONSOLIDATED_BY" in evidence.page_reference


@pytest.mark.django_db
def test_removed_relationship_ceases_on_reimport():
    companies = [
        record(lei(14), name="Filial Fictícia Delta, Lda.", nipc=NIPC_A),
        record(lei(15), name="Grupo Fictício Delta, S.A.", nipc=NIPC_B),
    ]
    run(FakeGleif(companies, [relation(lei(14), lei(15))]))
    claim = Relationship.objects.get()
    assert claim.status == "published"
    output = run(FakeGleif(companies), as_of=LATER)
    assert "cessados=1" in output
    claim.refresh_from_db()
    assert claim.status == "draft"
    assert not SourceObservation.objects.filter(is_current=True).exists()
    assert not Evidence.objects.filter(relationship=claim, is_public=True).exists()


@pytest.mark.django_db
def test_limited_run_replaces_only_the_children_it_read():
    companies = [
        record(lei(16), name="Filial Fictícia Épsilon, Lda.", nipc=NIPC_A),
        record(lei(17), name="Filial Fictícia Zeta, Lda.", nipc=NIPC_B),
        record(lei(18), name="Grupo Fictício Épsilon, S.A.", nipc=NIPC_C),
    ]
    run(FakeGleif(companies, [relation(lei(16), lei(18)), relation(lei(17), lei(18))]))
    # The first child lost its parent; the unread second child keeps its claim.
    output = run(FakeGleif(companies, [relation(lei(17), lei(18))]), as_of=LATER, limit=1)
    assert "cessados=1" in output
    kept = Relationship.objects.get(subject=holder("lei", lei(17)))
    ceased = Relationship.objects.get(subject=holder("lei", lei(16)))
    assert (kept.status, ceased.status) == ("published", "draft")
