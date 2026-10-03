import json
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command

from ligacoes.core.identity import ar_person, official_entity, reject_suggestion, suggest_identity
from ligacoes.core.models import (
    Entity,
    Evidence,
    IdentitySuggestion,
    Relationship,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue
from ligacoes.core.wikidata import QUERIES, parse_results

# Fictional identifiers only; NIPCs carry valid check digits.
NIPC_ALFA = "500000018"
NIPC_BETA = "500000026"
PERSON_VAT = "123456789"
LEI_GAMA = "529900FICTICIOGAMA12"
TR_ALFA = "900000000012-34"
EP_ID = "424242"

type Answers = dict[str, list[tuple[str, str]]]


def payload(property_id: str, rows: list[tuple[str, str]]) -> JSONObject:
    query = next(query for query in QUERIES if query.property == property_id)
    bindings: list[JSONValue] = []
    for qid, value in rows:
        item: JSONObject = {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"}
        literal: JSONObject = {"type": "literal", "value": value}
        bindings.append({"item": item, query.variable: literal})
    names: list[JSONValue] = ["item", query.variable]
    return {"head": {"vars": names}, "results": {"bindings": bindings}}


def run(answers: Answers, *, apply: bool = True, broken: str | None = None) -> str:
    def download(url: str, **request: object) -> bytes:
        body = request["body"]
        assert isinstance(body, bytes)
        sparql = parse_qs(body.decode())["query"][0]
        query = next(query for query in QUERIES if query.sparql == sparql)
        if query.property == broken:
            return b'{"results": {}}'
        return json.dumps(payload(query.property, answers.get(query.property, []))).encode()

    output = StringIO()
    with (
        patch("ligacoes.core.wikidata.download", side_effect=download),
        patch("ligacoes.core.wikidata.PAUSE", 0),
    ):
        if apply:
            call_command("import_wikidata_crosswalk", "--apply", stdout=output)
        else:
            call_command("import_wikidata_crosswalk", stdout=output)
    return output.getvalue()


def organisation(scheme: str, external_id: str, name: str) -> Entity:
    return official_entity(
        scheme, external_id, name=name, kind="organisation", classification="association"
    )


def hints() -> dict[str, Entity]:
    return {
        identity.external_id: identity.entity
        for identity in SourceIdentity.objects.select_related("entity").filter(source="wikidata")
    }


@pytest.mark.django_db
def test_qids_attach_only_through_existing_official_ids_and_create_nothing_public():
    deputy = ar_person("9001", "Rita Fictícia Moura")
    alfa = organisation("nipc", NIPC_ALFA, "Associação Fictícia Alfa")
    gama = organisation("lei", LEI_GAMA, "Associação Fictícia Gama")
    delta = organisation("eu_tr", TR_ALFA, "Associação Fictícia Delta")
    entities = Entity.objects.count()
    answers: Answers = {
        "P6199": [("Q101", "9001"), ("Q102", "9999")],
        "P1186": [("Q103", "31337")],
        "P3608": [("Q201", f"PT{NIPC_ALFA}"), ("Q202", f"PT{PERSON_VAT}")],
        "P1278": [("Q301", LEI_GAMA)],
        "P2657": [("Q401", TR_ALFA)],
    }

    dry = run(answers, apply=False)
    assert "pistas QID novas=4" in dry
    assert "Sem escritas" in dry
    assert not SourceIdentity.objects.filter(source="wikidata").exists()

    applied = run(answers)
    assert hints() == {"Q101": deputy, "Q201": alfa, "Q301": gama, "Q401": delta}
    assert "descartados=1" in applied
    assert "sem identificador oficial ligado=2" in applied
    assert PERSON_VAT not in applied
    # Hints are unreviewed and nothing else is created or published.
    assert not SourceIdentity.objects.filter(source="wikidata", reviewed_by__isnull=False).exists()
    assert Entity.objects.count() == entities
    assert not Relationship.objects.exists()
    assert not Evidence.objects.exists()
    assert not SourceObservation.objects.exists()
    assert not IdentitySuggestion.objects.exists()

    rerun = run(answers)
    assert "pistas QID novas=0; inalteradas=4" in rerun
    assert SourceIdentity.objects.filter(source="wikidata").count() == 4


def test_natural_person_and_malformed_vat_numbers_are_dropped_on_read():
    vat = next(query for query in QUERIES if query.property == "P3608")
    pairs, dropped = parse_results(
        vat,
        payload(
            "P3608",
            [
                ("Q1", f"PT{NIPC_ALFA}"),
                ("Q2", f"PT{PERSON_VAT}"),
                ("Q3", "PT290000017"),
                ("Q4", "PT500000019"),
                ("Q5", f"ES{NIPC_BETA}"),
            ],
        ),
    )
    assert pairs == [("Q1", NIPC_ALFA)]
    assert dropped == 4


@pytest.mark.django_db
def test_conflicting_qids_are_skipped_and_counted():
    alfa = organisation("nipc", NIPC_ALFA, "Associação Fictícia Alfa")
    beta = organisation("nipc", NIPC_BETA, "Associação Fictícia Beta")
    holder = ar_person("9002", "Nuno Fictício Paiva")
    twice = ar_person("9003", "Carla Fictícia Lopes")
    kept = ar_person("9004", "Teresa Fictícia Neves")
    SourceIdentity.objects.create(source="wikidata", external_id="Q501", entity=alfa)
    SourceIdentity.objects.create(source="wikidata", external_id="Q600", entity=holder)
    SourceIdentity.objects.create(source="wikidata", external_id="Q900", entity=kept)
    before = hints()
    answers: Answers = {
        # Q501 is already another entity's hint; Q601 targets a person holding Q600;
        # Q701 reaches two entities; Q801/Q802 claim the same deputy.
        "P6199": [
            ("Q601", "9002"),
            ("Q701", "9003"),
            ("Q801", "9005"),
            ("Q802", "9005"),
            ("Q900", "9004"),
        ],
        "P1186": [("Q601", EP_ID), ("Q701", "515151"), ("Q801", "616161")],
        "P3608": [("Q501", f"PT{NIPC_BETA}"), ("Q701", f"PT{NIPC_BETA}")],
    }
    ar_person("9005", "Paula Fictícia Reis")

    output = run(answers)

    assert hints() == before
    assert before["Q501"] == alfa
    assert beta.pk not in {entity.pk for entity in hints().values()}
    assert twice.pk not in {entity.pk for entity in hints().values()}
    assert "conflitos=5" in output
    assert "inalteradas=1" in output
    assert not IdentitySuggestion.objects.exists()


@pytest.fixture
def review_user(db, django_user_model: type[User]) -> User:
    user = django_user_model.objects.create_user(username="fictional-identity-reviewer")
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="core", codename="review_sourceidentity")
    )
    return user


@pytest.mark.django_db
def test_cross_source_ids_become_one_pending_suggestion_until_decided(review_user):
    deputy = ar_person("9001", "Rita Fictícia Moura")
    alfa = organisation("nipc", NIPC_ALFA, "Associação Fictícia Alfa")
    answers: Answers = {
        "P6199": [("Q101", "9001")],
        "P1186": [("Q101", EP_ID)],
        "P3608": [("Q201", f"PT{NIPC_ALFA}")],
        "P2657": [("Q201", TR_ALFA)],
    }

    dry = run(answers, apply=False)
    assert "sugestões de identidade novas=2" in dry
    assert not IdentitySuggestion.objects.exists()

    run(answers)
    output = run(answers)

    assert "novas=0; já pendentes=2" in output
    ep = IdentitySuggestion.objects.get(scheme="ep")
    tr = IdentitySuggestion.objects.get(scheme="eu_tr")
    assert (ep.external_id, ep.candidate, ep.status) == (EP_ID, deputy, "pending")
    assert (tr.external_id, tr.candidate, tr.status) == (TR_ALFA, alfa, "pending")
    assert all(value in ep.basis for value in ("Q101", "9001", EP_ID))
    assert all(value in tr.basis for value in ("Q201", NIPC_ALFA, TR_ALFA))
    # Suggestions never link by themselves.
    assert not SourceIdentity.objects.filter(source__in=("ep", "eu_tr")).exists()

    reject_suggestion(ep, review_user)
    run(answers)
    assert IdentitySuggestion.objects.filter(scheme="ep").count() == 1
    assert IdentitySuggestion.objects.get(scheme="ep").status == "rejected"


@pytest.mark.django_db
def test_mapped_distinct_profile_keeps_advisory_hint_but_same_profile_needs_none():
    deputy = ar_person("9001", "Rita Fictícia Moura")
    alfa = organisation("nipc", NIPC_ALFA, "Associação Fictícia Alfa")
    separate = official_entity(
        "ep",
        EP_ID,
        name="Luís Fictício Gama",
        kind="person",
        classification="",
    )
    SourceIdentity.objects.create(source="eu_tr", external_id=TR_ALFA, entity=alfa)

    output = run(
        {
            "P6199": [("Q101", "9001")],
            "P1186": [("Q101", EP_ID)],
            "P3608": [("Q201", f"PT{NIPC_ALFA}")],
            "P2657": [("Q201", TR_ALFA)],
        }
    )

    suggestion = IdentitySuggestion.objects.get()
    assert (suggestion.scheme, suggestion.external_id, suggestion.candidate) == (
        "ep",
        EP_ID,
        deputy,
    )
    assert suggestion.status == "pending" and "Q101" in suggestion.basis
    assert SourceIdentity.objects.get(source="ep", external_id=EP_ID).entity == separate
    assert not IdentitySuggestion.objects.filter(scheme="eu_tr").exists()
    assert set(hints()) == {"Q101", "Q201"}
    assert "sugestões de identidade novas=1" in output


@pytest.mark.django_db
def test_organisation_registers_never_suggest_a_person():
    person = ar_person("9001", "Rita Fictícia Moura")
    with pytest.raises(ValidationError):
        suggest_identity("eu_tr", TR_ALFA, candidate=person, name="Wikidata Q1", basis="Teste.")
    assert not IdentitySuggestion.objects.exists()


@pytest.mark.django_db
def test_a_malformed_answer_fails_the_whole_run_without_writes():
    ar_person("9001", "Rita Fictícia Moura")
    with pytest.raises(CommandError):
        run({"P6199": [("Q101", "9001")]}, broken="P2657")
    assert not SourceIdentity.objects.filter(source="wikidata").exists()
