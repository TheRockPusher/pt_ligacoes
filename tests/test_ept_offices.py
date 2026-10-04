import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from ligacoes.core import ept_offices
from ligacoes.core.enrichment import ObservationInput, get_source_identity, sync_observations
from ligacoes.core.ept_offices import (
    PAGE_SIZE,
    EptOfficesError,
    apply_snapshot,
    classify,
    fetch_holders,
    parse_listing,
    role_class,
)
from ligacoes.core.identity import ar_institution, ar_person
from ligacoes.core.models import (
    Entity,
    EntityAlias,
    Evidence,
    IdentitySuggestion,
    Relationship,
    ReviewEvent,
    Source,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue

DAY = date(2026, 9, 1)
LATER = date(2026, 9, 15)
PERSON_NIF = "123456789"


def row(
    row_id: int,
    *,
    holder_id: int,
    holder: str,
    entity_id: int = 9200,
    entity: str = "Junta de Freguesia de Vila Fictícia",
    role: str = "Presidente da Junta de Freguesia",
    role_id: int = 700,
    begin: str | None = "2021-10-17T23:00:00",
    end: str | None = None,
) -> JSONObject:
    return {
        "id": row_id,
        "entityId": entity_id,
        "roleId": role_id,
        "holderId": holder_id,
        "entity": entity,
        "role": role,
        "holder": holder,
        "beginDate": begin,
        "endDate": end,
    }


def pages(*rows: JSONObject, page_size: int = PAGE_SIZE) -> list[JSONValue]:
    items: list[JSONValue] = list(rows)
    count = (len(items) + page_size - 1) // page_size
    return [
        {
            "code": 0,
            "description": "",
            "data": {
                "elapseTime": "0,021 seconds",
                "items": items[(number - 1) * page_size : number * page_size],
                "pageNumber": number,
                "pageSize": page_size,
                "pageCount": count,
                "total": len(items),
            },
        }
        for number in range(1, max(1, count) + 1)
    ]


def run(*rows: JSONObject, as_of: date = DAY, apply: bool = True) -> str:
    wire = pages(*rows)

    def download(url: str, **request: object) -> bytes:
        body = request["body"]
        assert isinstance(body, bytes)
        return json.dumps(wire[json.loads(body)["pageNumber"] - 1]).encode()

    output = StringIO()
    with patch("ligacoes.core.ept_offices.download", side_effect=download):
        if apply:
            call_command("import_ept_offices", "--apply", as_of=as_of, stdout=output)
        else:
            call_command("import_ept_offices", as_of=as_of, stdout=output)
    return output.getvalue()


def publish_parliament_office(cadastro_id: str, name: str) -> Entity:
    deputy = ar_person(cadastro_id, name)
    identity = SourceIdentity.objects.get(source="parliament", external_id=cadastro_id)
    sync_observations(
        source="parliament",
        scope=f"test-deputy:{cadastro_id}",
        observations=(
            ObservationInput(
                external_id=f"deputy:{cadastro_id}",
                revision="fictional-1",
                category="parliament_body",
                passage="Mandato parlamentar inteiramente fictício.",
                source_url="https://example.org/ar-ficticia",
                publisher="Assembleia fictícia",
                reference=f"Deputado fictício {cadastro_id}",
                title="Composição fictícia",
                identity=identity,
                object=ar_institution(),
                kind="public_office",
            ),
        ),
        as_of=DAY,
    )
    return deputy


def publish_government_office(official_id: str, name: str) -> Entity:
    person = get_source_identity(
        source="government", external_id=f"person:{official_id}", name=name
    )
    portfolio = get_source_identity(
        source="government",
        external_id="government:gc25",
        name="XXV Governo Constitucional",
        entity_kind="organisation",
    )
    sync_observations(
        source="government",
        scope=f"test-government:{official_id}",
        observations=(
            ObservationInput(
                external_id=f"appointment:{official_id}",
                revision="fictional-1",
                category="government_office",
                passage="Cargo governamental inteiramente fictício.",
                source_url="https://example.org/governo-ficticio",
                publisher="Governo fictício",
                reference=f"Mandato fictício {official_id}",
                title="Composição fictícia",
                identity=person,
                object=portfolio.entity,
                kind="public_office",
            ),
        ),
        as_of=DAY,
    )
    return person.entity


def ept_person(holder_id: str) -> Entity | None:
    identity = SourceIdentity.objects.filter(source="ept", external_id=holder_id).first()
    return identity.entity if identity is not None else None


@pytest.mark.django_db
def test_dry_run_writes_nothing_and_apply_links_a_new_holder_with_lisbon_civil_dates():
    holder = row(
        11,
        holder_id=4001,
        holder="Tomé  Fictício Arruda ",
        begin="2021-10-17T23:00:00",
        end="2025-10-12T22:00:00",
    )
    assert "Sem escritas" in run(holder, apply=False)
    assert not Entity.objects.exists()
    assert not SourceObservation.objects.exists()
    run(holder)
    person = ept_person("4001")
    assert person is not None
    assert (person.name, person.kind, person.is_public) == (
        "Tomé Fictício Arruda",
        "person",
        True,
    )
    relation = Relationship.objects.get()
    assert relation.status == Relationship.Status.PUBLISHED
    assert relation.subject == person
    assert relation.object.classification == "parish"
    assert SourceIdentity.objects.get(source="ept", external_id="entity:9200").entity == (
        relation.object
    )
    assert (relation.kind, relation.role, relation.role_class) == (
        "public_office",
        "Presidente da Junta de Freguesia",
        "leadership",
    )
    # 23:00/22:00 without offset is Lisbon midnight stored as UTC: the next civil day.
    assert (relation.start_date, relation.end_date) == (date(2021, 10, 18), date(2025, 10, 13))
    assert relation.temporal_status == "ended"
    evidence = Evidence.objects.get(relationship=relation)
    assert evidence.is_public and evidence.source.is_public
    assert evidence.source.dataset == "ept_titulares"
    assert "registo 11" in evidence.page_reference


@pytest.mark.django_db
def test_state_company_row_is_a_directorship_and_a_reversed_end_is_dropped():
    run(
        row(
            21,
            holder_id=4002,
            holder="Inês Fictícia Barros",
            entity_id=9300,
            entity="Águas Fictícias do Vale, E.M., S.A.",
            role="Vogal Executiva",
            begin="2024-01-10T00:00:00",
            end="2023-12-31T00:00:00",
        )
    )
    relation = Relationship.objects.get()
    assert relation.kind == "directorship"
    assert (relation.object.kind, relation.object.classification) == ("company", "state_company")
    assert relation.role_class == "member"
    assert (relation.start_date, relation.end_date) == (date(2024, 1, 10), None)
    assert relation.temporal_status == "unknown"
    assert "antecede o início" in relation.description


@pytest.mark.django_db
def test_party_organs_and_candidacies_are_skipped_unread():
    rows = (
        row(
            31,
            holder_id=5001,
            holder="PRIVATE_PARTY_OFFICER",
            entity_id=4284,
            entity="PRIVATE_PARTY_LABEL",
            begin="PRIVATE_UNPARSED",
        ),
        row(
            32,
            holder_id=5002,
            holder="PRIVATE_NEW_PARTY_OFFICER",
            entity_id=9901,
            entity="Partido Fictício da Serra",
        ),
        row(
            33,
            holder_id=5003,
            holder="PRIVATE_CANDIDATE",
            entity_id=9902,
            entity="Presidência Fictícia",
            role="Candidato a Presidente da República",
        ),
    )
    assert "3 excluídos" in run(*rows, apply=False)
    run(*rows)
    assert not Entity.objects.exists()
    assert not SourceIdentity.objects.exists()
    assert not SourceObservation.objects.exists()
    assert not IdentitySuggestion.objects.exists()


@pytest.mark.django_db
def test_national_crosswalks_corroborate_aliases_without_duplicating_mandates():
    deputy = publish_parliament_office("9001", "Rita Fictícia Moura")
    minister = publish_government_office("fict-1", "Nuno Fictício Paiva")
    EntityAlias.objects.create(
        entity=minister,
        name="Nuno Manuel Fictício Paiva",
        normalised="nuno manuel ficticio paiva",
        scheme="government",
        external_id="person:fict-1",
    )
    # Same-name people with no corroborating office do not prevent a unique office match.
    for slug, name in (
        ("rita-homonima", "Rita Fictícia Moura"),
        ("nuno-homonimo", "Nuno Fictício Paiva"),
    ):
        Entity.objects.create(name=name, slug=slug, kind="person", is_public=True)
    claims = SourceObservation.objects.count()
    relations = Relationship.objects.count()
    run(
        row(
            41,
            holder_id=6001,
            holder="Dra. Rita Fictícia Moura",
            entity_id=4508,
            entity="Assembleia da República - XVII Legislatura",
            role="Deputada",
        ),
        row(
            42,
            holder_id=6002,
            holder="Nuno Manuel Fictício Paiva",
            entity_id=4509,
            entity="XXV Governo Constitucional",
            role="Ministro da Economia Fictícia",
        ),
    )
    assert ept_person("6001") == deputy
    assert ept_person("6002") == minister
    assert EntityAlias.objects.filter(
        entity=minister,
        scheme="ept",
        external_id="6002",
        name="Nuno Manuel Fictício Paiva",
    ).exists()
    assert SourceObservation.objects.count() == claims
    assert Relationship.objects.count() == relations
    assert not SourceIdentity.objects.filter(
        source="ept", external_id__startswith="entity:"
    ).exists()


@pytest.mark.django_db
def test_government_crosswalk_links_other_offices_before_their_publication():
    minister = publish_government_office("fict-2", "Eva Fictícia")
    run(
        row(
            1,
            holder_id=1,
            holder="Eva Fictícia",
            entity_id=4509,
            entity="XXV Governo Constitucional",
            role="Ministra Fictícia",
        ),
        row(
            2,
            holder_id=1,
            holder="Eva Fictícia",
            entity_id=4300,
            entity="Conselho de Estado Fictício",
            role="Membro",
        ),
    )
    office = SourceObservation.objects.get(source="ept")
    assert office.identity is not None
    assert office.relationship is not None
    assert office.identity.entity == minister
    assert office.relationship.subject == minister
    assert office.relationship.status == "published"
    assert Relationship.objects.filter(subject=minister).count() == 2


@pytest.mark.django_db
def test_same_institution_without_overlapping_dates_does_not_merge_namesakes():
    minister = publish_government_office("fict-3", "Ivo Fictício")
    relationship = Relationship.objects.get(subject=minister)
    Relationship.objects.filter(pk=relationship.pk).update(
        start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    run(
        row(
            1,
            holder_id=1,
            holder="Ivo Fictício",
            entity_id=4509,
            entity="XXV Governo Constitucional",
            begin="2026-06-01T00:00:00",
        )
    )
    assert ept_person("1") != minister
    assert Relationship.objects.count() == 1


@pytest.mark.django_db
def test_uncorroborated_namesake_gets_a_separate_public_office_and_advisory_suggestion():
    namesake = Entity.objects.create(
        name="Carla Fictícia Lopes", slug="carla-ficticia", kind="person", is_public=True
    )
    run(row(51, holder_id=7001, holder="Carla Fictícia Lopes"))
    suggestion = IdentitySuggestion.objects.get(scheme="ept", external_id="7001")
    assert (suggestion.candidate, suggestion.status) == (namesake, "pending")
    person = ept_person("7001")
    assert person is not None and person != namesake
    observation = SourceObservation.objects.get(scope="offices:7001")
    assert observation.identity is not None
    assert observation.relationship is not None
    assert observation.identity.entity == person
    assert observation.relationship.status == Relationship.Status.PUBLISHED
    assert list(Entity.objects.filter(kind="person").order_by("pk")) == sorted(
        [namesake, person], key=lambda entity: entity.pk
    )


@pytest.mark.django_db
def test_absent_holder_ceases_on_the_next_complete_list():
    kept = row(61, holder_id=8001, holder="Teresa Fictícia Neves")
    gone = row(
        62,
        holder_id=8002,
        holder="Duarte Fictício Sá",
        entity_id=9201,
        entity="Junta de Freguesia de Outra Fictícia",
    )
    run(kept, gone)
    assert Relationship.objects.filter(status="published").count() == 2
    run(kept, as_of=LATER)
    ceased = SourceObservation.objects.get(scope="offices:8002")
    assert not ceased.is_current
    assert ceased.relationship is not None
    assert ceased.relationship.status != Relationship.Status.PUBLISHED
    current = SourceObservation.objects.get(scope="offices:8001", is_current=True)
    assert current.relationship is not None
    assert current.relationship.status == Relationship.Status.PUBLISHED


@pytest.mark.django_db
def test_personal_tax_numbers_never_reach_storage():
    tainted = row(71, holder_id=9101, holder="Beatriz Fictícia Cunha")
    tainted["nif"] = PERSON_NIF
    wire_nif = row(
        72,
        holder_id=9102,
        holder="Rui Fictício Mota",
        entity_id=9202,
        entity="Câmara Municipal de Fictícia",
    )
    wire_nif["nif"] = PERSON_NIF
    run(tainted, wire_nif)
    assert SourceObservation.objects.filter(is_current=True).count() == 2
    nine_digits = re.compile(r"(?<!\d)\d{9}(?!\d)")
    for model in (
        Entity,
        SourceIdentity,
        IdentitySuggestion,
        SourceObservation,
        Relationship,
        Evidence,
        Source,
    ):
        for stored in model.objects.values():
            for field, value in stored.items():
                # Revisions are hashes, not source values.
                if isinstance(value, str) and field != "revision":
                    assert not nine_digits.search(value)


@pytest.mark.django_db
def test_complete_holder_enumeration_includes_excluded_and_national_rows():
    wire = pages(
        row(1, holder_id=1, holder="Ana Fictícia", entity_id=4284),
        row(
            2, holder_id=2, holder="Berta Fictícia", entity_id=510, entity="Assembleia da República"
        ),
        row(3, holder_id=2, holder="Berta Fictícia"),
        row(4, holder_id=3, holder="Caio Fictício", role="Candidato a Presidente"),
    )
    with patch("ligacoes.core.ept_offices._post_page", return_value=wire[0]):
        assert fetch_holders(as_of=DAY) == {
            "1": "Ana Fictícia",
            "2": "Berta Fictícia",
            "3": "Caio Fictício",
        }
    assert not Entity.objects.exists()


@pytest.mark.django_db
def test_bulk_offices_publish_aliases_evidence_and_remain_idempotent():
    rows = [
        row(
            index,
            holder_id=10000 + index,
            holder=f"Pessoa Fictícia {index}",
            entity_id=9200 + index % 5,
            entity=f"Câmara Municipal Fictícia {index % 5}",
            role="Vereadora",
            role_id=129,
        )
        for index in range(1, 251)
    ]
    with CaptureQueriesContext(connection) as queries:
        run(*rows)
    assert len(queries) < 150
    assert Relationship.objects.filter(status="published", role="Vereadora").count() == 250
    assert Evidence.objects.filter(is_public=True, source__is_public=True).count() == 250
    assert Source.objects.filter(dataset="ept_titulares").count() == 1
    assert EntityAlias.objects.filter(scheme="ept").count() == 250
    assert ReviewEvent.objects.filter(action="auto_publish").count() == 250
    with CaptureQueriesContext(connection) as queries:
        run(*rows, as_of=LATER)
    assert len(queries) < 100
    assert Relationship.objects.count() == 250
    assert SourceObservation.objects.count() == 250
    assert Evidence.objects.count() == 250
    assert Source.objects.count() == 1
    assert ReviewEvent.objects.count() == 250


@pytest.mark.django_db
@pytest.mark.parametrize("changed_field", ["begin", "end", "title", "publisher", "url"])
def test_projection_changes_create_new_revisions_even_when_passage_is_unchanged(changed_field):
    original = row(1, holder_id=1, holder="Pessoa Fictícia")
    # The displayed passage can stay unchanged while a structured date is corrected.
    with patch("ligacoes.core.ept_offices._date_passage", return_value=""):
        run(original)
        first = SourceObservation.objects.get()
        if changed_field in {"begin", "end"}:
            changed = {
                **original,
                "beginDate" if changed_field == "begin" else "endDate": "2025-01-01T00:00:00",
            }
            run(changed, as_of=LATER)
        else:
            dataset = {
                "title": replace(
                    ept_offices.DATASET, title="Titulares fictícios — título corrigido"
                ),
                "publisher": replace(
                    ept_offices.DATASET, publisher="Publicador fictício corrigido"
                ),
                "url": replace(ept_offices.DATASET, url="https://example.org/titulares-ficticios"),
            }[changed_field]
            with patch.object(ept_offices, "DATASET", dataset):
                run(original, as_of=LATER)
    current = SourceObservation.objects.get(is_current=True)
    assert current.revision != first.revision
    assert current.passage == first.passage
    assert current.relationship is not None
    assert current.relationship.status == Relationship.Status.PUBLISHED
    assert Relationship.objects.get(pk=first.relationship_id).status == Relationship.Status.DRAFT


@pytest.mark.django_db
def test_changed_returning_and_rejected_offices_preserve_revision_and_editorial_decisions():
    original = row(1, holder_id=1, holder="Pessoa Fictícia")
    run(original)
    first = SourceObservation.objects.get()
    run(as_of=LATER)
    assert not SourceObservation.objects.get(pk=first.pk).is_current
    assert not Evidence.objects.get(pk=first.evidence_id).is_public
    run(original, as_of=LATER)
    assert SourceObservation.objects.get(pk=first.pk).is_current
    assert Relationship.objects.get(pk=first.relationship_id).status == "published"
    changed = {**original, "role": "Vereador", "roleId": 17}
    run(changed, as_of=LATER)
    assert SourceObservation.objects.count() == 2
    assert Relationship.objects.get(pk=first.relationship_id).status == "draft"
    current = SourceObservation.objects.get(is_current=True)
    Relationship.objects.filter(pk=current.relationship_id).update(
        status="rejected", reviewed_by=None, reviewed_at=None
    )
    run(changed, as_of=LATER)
    assert Relationship.objects.get(pk=current.relationship_id).status == "rejected"
    run(as_of=LATER)
    run({**changed, "role": "Vereadora"}, as_of=LATER)
    assert not Relationship.objects.filter(status="published").exists()


@pytest.mark.django_db
def test_late_batch_failure_rolls_back_entities_and_all_offices():
    snapshot = parse_listing(
        pages(row(1, holder_id=1, holder="Pessoa Fictícia")),
        as_of=DAY,
        retrieved_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
    )
    with (
        patch.object(Relationship.objects, "bulk_create", side_effect=ValidationError("Falha")),
        pytest.raises(ValidationError),
    ):
        apply_snapshot(snapshot)
    assert not Entity.objects.exists()
    assert not SourceIdentity.objects.exists()
    assert not SourceObservation.objects.exists()
    assert not Source.objects.exists()


@pytest.mark.django_db
def test_older_complete_snapshot_cannot_cease_or_rewrite_offices():
    office = row(1, holder_id=1, holder="Pessoa Fictícia")
    run(office, as_of=LATER)
    snapshot = parse_listing(pages(), as_of=DAY, retrieved_at=datetime(2026, 9, 1, 12, tzinfo=UTC))
    with pytest.raises(ValidationError):
        apply_snapshot(snapshot)
    assert SourceObservation.objects.get().is_current
    assert Relationship.objects.get().status == "published"


@pytest.mark.parametrize(
    "problem",
    ["page_size", "shifted_ids", "holder_label", "missing_key", "tax_id_in_label"],
)
def test_ambiguous_or_incomplete_lists_fail_closed(problem):
    first = row(81, holder_id=9201, holder="Paula Fictícia Reis")
    second = row(82, holder_id=9202, holder="Luís Fictício Gama")
    wire = pages(first, second)
    if problem == "page_size":
        wire = pages(first, second, page_size=1)
    elif problem == "shifted_ids":
        second["id"] = 81
    elif problem == "holder_label":
        second["holderId"] = 9201
    elif problem == "missing_key":
        del second["endDate"]
    else:
        second["holder"] = f"Luís {PERSON_NIF}"
    with pytest.raises(EptOfficesError):
        parse_listing(wire, as_of=DAY, retrieved_at=datetime(2026, 9, 1, 12, tzinfo=UTC))


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Presidente", "leadership"),
        ("Ministra da Saúde", "leadership"),
        ("Ministro Adjunto e da Coesão Territorial", "leadership"),
        ("Vice-Presidente", "deputy_leadership"),
        ("Secretário de Estado do Tesouro", "deputy_leadership"),
        ("Diretor Nacional Adjunto", "deputy_leadership"),
        ("Vogal suplente", "substitute"),
        ("Vereadora", "member"),
        ("Chefe de Gabinete", "staff"),
        ("Embaixador", "other"),
    ],
)
def test_role_classes_follow_the_shared_mapping(label, expected):
    assert role_class(label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Câmara Municipal de Vila Fictícia", ("organisation", "municipality")),
        ("Junta de Freguesia de Fictícia", ("organisation", "parish")),
        ("Instituto Fictício do Mar, I.P.", ("organisation", "public_body")),
        ("Entidade Reguladora dos Serviços Fictícios", ("organisation", "regulator")),
        ("Gabinete do Secretário de Estado Fictício", ("organisation", "government_office")),
        ("Gabinete de Estratégia Fictícia", ("organisation", "public_body")),
        ("Hospital Fictício, E.P.E.", ("company", "state_company")),
        ("Transportes Fictícios, E.I.M., S.A.", ("company", "state_company")),
        ("Faculdade de Letras da Universidade Fictícia", ("university", "higher_education")),
    ],
)
def test_new_entities_are_classified_from_their_published_label(label, expected):
    assert classify(label) == expected
