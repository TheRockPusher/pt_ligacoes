import json
from datetime import date
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ligacoes.core import european_parliament as ep
from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Evidence,
    IdentitySuggestion,
    Relationship,
    SourceIdentity,
    SourceObservation,
    Term,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue

DAY = date(2026, 9, 1)
LATER = date(2026, 9, 15)
PERSON = "990001"
# Terms 1-8 have invented contiguous dates; only their existence matters here.
TERMS: dict[int, tuple[str, str | None]] = {
    **{n: (f"{1974 + 5 * n}-07-01", f"{1979 + 5 * n}-06-30") for n in range(1, 9)},
    9: ("2019-07-02", "2024-07-15"),
    10: ("2024-07-16", None),
}
BIRTH_DAY = "1970-01-02"
BIRTH_PLACE = "Vila Fictícia de Baixo"
PARTY = "Partido Nacional Fictício"
CONTACTS = ("example.org", "FEMALE", "0000 0000")


def period(start: str, end: str | None) -> JSONObject:
    value: JSONObject = {"id": f"time-period/{start}", "type": "PeriodOfTime", "startDate": start}
    if end:
        value["endDate"] = end
    return value


def membership(
    person: str,
    suffix: str,
    organisation: str,
    role: str,
    classification: str | None = None,
    *,
    start: str = "2024-07-16",
    end: str | None = None,
) -> JSONObject:
    item: JSONObject = {
        "id": f"membership/{person}-{suffix}",
        "type": "Membership",
        "identifier": f"{person}-{suffix}",
        "organization": organisation,
        "role": f"def/ep-roles/{role}",
        "memberDuring": period(start, end),
        "contactPoint": [{"id": "fictional-office", "telephone": "+00 0000 0000"}],
    }
    if classification:
        item["membershipClassification"] = f"def/ep-entities/{classification}"
    return item


def mandate(person: str, term: int = 10, suffix: str = "m-1") -> JSONObject:
    start, end = TERMS[term]
    return membership(person, suffix, f"org/ep-{term}", "MEMBER_PARLIAMENT", start=start, end=end)


def national_party(person: str) -> JSONObject:
    return membership(person, "f-9", "org/7009", "MEMBER", "NATIONAL_POLITICAL_GROUP")


def mep(person: str, given: str, family: str, memberships: list[JSONValue]) -> JSONObject:
    return {
        "id": f"person/{person}",
        "type": "Person",
        "identifier": person,
        "label": f"{given} {family.upper()}",
        "givenName": given,
        "familyName": family,
        "sortLabel": family.upper(),
        "bday": BIRTH_DAY,
        "placeOfBirth": BIRTH_PLACE,
        "hasGender": "http://publications.europa.eu/resource/authority/human-sex/FEMALE",
        "hasEmail": "mailto:deputada.ficticia@example.org",
        "img": f"https://example.org/fictional/{person}.jpg",
        "hasMembership": memberships,
    }


def body(
    org: str,
    classification: str,
    *,
    pt: str | None,
    en: str = "Fictional body",
    start: str = "2024-07-16",
    end: str | None = None,
) -> JSONObject:
    """An all-language detail page; a missing Portuguese label falls back to English."""
    labels: JSONObject = {"en": en, "fr": "Organe fictif"}
    if pt:
        labels["pt"] = pt
    return {
        "id": f"org/{org}",
        "type": "Organization",
        "identifier": org,
        "isVersionOf": "org/FICTIONAL",
        "label": "FIC",
        "temporal": period(start, end),
        "prefLabel": labels,
        "classification": f"def/ep-entities/{classification}",
    }


BODIES: dict[str, JSONObject] = {
    "7001": body("7001", "EU_POLITICAL_GROUP", pt="Grupo Fictício dos Exemplos"),
    "7002": body("7002", "COMMITTEE_PARLIAMENTARY_STANDING", pt="Comissão Fictícia dos Sonhos"),
    "7003": body("7003", "DELEGATION_PARLIAMENTARY", pt="Delegação Fictícia à Terra do Nunca"),
    "7009": body("7009", "NATIONAL_POLITICAL_GROUP", pt=PARTY),
    "6001": body(
        "6001",
        "EU_POLITICAL_GROUP",
        pt=None,
        en="Fictional Group of Examples",
        start="2019-07-02",
        end="2024-07-15",
    ),
}


def full_member(person: str = PERSON) -> list[JSONValue]:
    return [
        mandate(person),
        membership(person, "f-1", "org/7001", "CHAIR_VICE", "EU_POLITICAL_GROUP"),
        membership(
            person,
            "f-2",
            "org/7002",
            "MEMBER_SUBSTITUTE",
            "COMMITTEE_PARLIAMENTARY_STANDING",
            start="2024-07-19",
            end="2025-01-31",
        ),
        membership(person, "f-3", "org/7003", "CHAIR", "DELEGATION_PARLIAMENTARY"),
        national_party(person),
    ]


class FakeEp:
    """Fictional EP API v2: the PT list, MEP details, terms and corporate bodies."""

    def __init__(self, meps: list[JSONObject], *, failures: tuple[int, ...] = ()) -> None:
        self.meps = {str(item["identifier"]): item for item in meps}
        # Statuses answered (empty body, ``Retry-After: 7``) before any real response.
        self.failures = list(failures)
        self.requested: list[str] = []

    @staticmethod
    def page(data: list[JSONValue]) -> bytes:
        return json.dumps({"data": data, "@context": []}).encode()

    def get(self, url: str, *, deadline: float, max_bytes: int) -> ep._Response:
        ep.validate_url(url)
        self.requested.append(url)
        if self.failures:
            return ep._Response(self.failures.pop(0), "7", b"")
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        name = parts.path.removeprefix("/api/v2/")
        if name == "meps":
            offset = int(query["offset"])
            found: list[JSONValue] = [
                {"id": f"person/{key}", "type": "Person", "identifier": key, "label": "X"}
                for key in sorted(self.meps)[offset : offset + int(query["limit"])]
            ]
            if not found and offset:
                return ep._Response(204, "", b"")
            return ep._Response(200, "", self.page(found))
        if name.startswith("meps/"):
            return ep._Response(200, "", self.page([self.meps[name.removeprefix("meps/")]]))
        reference = name.removeprefix("corporate-bodies/")
        if reference.startswith("ep-"):
            number = int(reference[3:])
            start, end = TERMS[number]
            term: JSONObject = {
                "id": f"org/{reference}",
                "type": ["ParliamentaryTerm", "Organization"],
                "identifier": reference,
                "label": str(number),
                "temporal": period(start, end),
                "classification": "def/ep-entities/EU_INSTITUTION",
            }
            return ep._Response(200, "", self.page([term]))
        return ep._Response(200, "", self.page([BODIES[reference]]))


def run(fake: FakeEp, *, as_of: date = DAY, apply: bool = True) -> str:
    output = StringIO()
    with (
        patch("ligacoes.core.european_parliament._get", side_effect=fake.get),
        patch("ligacoes.core.european_parliament.REQUEST_INTERVAL", 0),
    ):
        call_command(
            "import_european_parliament",
            *(["--apply"] if apply else []),
            as_of=as_of,
            stdout=output,
        )
    return output.getvalue()


def holder(external_id: str) -> Entity:
    return SourceIdentity.objects.get(source="ep", external_id=external_id).entity


def stored_text() -> str:
    """Every stored field that could carry imported content."""
    return str(
        [
            list(Entity.objects.values()),
            list(SourceIdentity.objects.values()),
            list(SourceObservation.objects.values()),
            list(Relationship.objects.values()),
            list(Evidence.objects.values()),
            list(IdentitySuggestion.objects.values()),
        ]
    )


@pytest.mark.django_db
def test_national_political_group_is_never_fetched_or_stored():
    fake = FakeEp([mep(PERSON, "Beatriz Fictícia", "Lemos", full_member())])
    dry = run(fake, apply=False)
    assert "1 pertenças a partidos nacionais excluídas" in dry
    assert "Sem escritas" in dry
    assert not Entity.objects.exists()
    output = run(fake)
    assert PARTY not in dry + output
    assert not any("corporate-bodies/7009" in url for url in fake.requested)
    text = stored_text()
    assert PARTY not in text
    assert "org/7009" not in text
    assert "org:7009" not in text
    assert "NATIONAL_POLITICAL_GROUP" not in text
    assert not SourceObservation.objects.filter(external_id=f"{PERSON}-f-9").exists()


@pytest.mark.django_db
def test_new_mep_is_public_with_mandate_group_committee_and_delegation_claims():
    output = run(FakeEp([mep(PERSON, "Beatriz Fictícia", "Lemos", full_member())]))
    assert "pessoas criadas=1" in output
    person = holder(PERSON)
    assert (person.name, person.kind, person.is_public) == (
        "Beatriz Fictícia Lemos",
        "person",
        True,
    )
    parliament = holder("institution:parlamento-europeu")
    assert (parliament.name, parliament.classification) == ("Parlamento Europeu", "parliament")
    claims = Relationship.objects.filter(subject=person)
    assert set(claims.values_list("status", flat=True)) == {"published"}
    group, committee, delegation = holder("org:7001"), holder("org:7002"), holder("org:7003")
    fields = ("object", "kind", "role", "role_class", "term__code", "temporal_status")
    assert set(claims.values_list(*fields)) == {
        (
            parliament.pk,
            "public_office",
            "Deputado/a ao Parlamento Europeu",
            "member",
            "EP10",
            "current",
        ),
        (group.pk, "membership", "Vice-Presidente", "deputy_leadership", "EP10", "current"),
        (committee.pk, "membership", "Membro suplente", "substitute", "EP10", "ended"),
        (delegation.pk, "membership", "Presidente", "leadership", "EP10", "current"),
    }
    served = claims.get(object=committee)
    assert (served.start_date, served.end_date) == (date(2024, 7, 19), date(2025, 1, 31))
    assert (group.name, group.classification) == (
        "Grupo Fictício dos Exemplos (10.ª legislatura do PE)",
        "parliamentary_group",
    )
    assert committee.classification == "parliamentary_committee"
    assert delegation.classification == "parliamentary_delegation"
    term = Term.objects.get(kind="ep_term", code="EP10")
    assert (term.label, term.start_date, term.end_date, term.institution) == (
        "10.ª legislatura do Parlamento Europeu",
        date(2024, 7, 16),
        None,
        parliament,
    )
    evidence = Evidence.objects.select_related("source").get(relationship=served)
    assert evidence.is_public and evidence.source.is_public
    assert evidence.source.url == f"https://www.europarl.europa.eu/meps/pt/{PERSON}"
    assert evidence.source.dataset == "ep_deputados"
    for fragment in (PERSON, "org/7002", "COMMITTEE_PARLIAMENTARY_STANDING", "2024-07-19"):
        assert fragment in evidence.page_reference
    structure = Relationship.objects.get(subject=committee)
    assert (structure.kind, structure.object, structure.status) == (
        "part_of",
        parliament,
        "published",
    )
    # Birth data, gender, contacts and photos are never retained.
    text = stored_text()
    for private in (BIRTH_DAY, "date(1970, 1, 2)", BIRTH_PLACE, *CONTACTS):
        assert private not in text


@pytest.mark.django_db
def test_mep_with_pending_namesake_suggestion_stays_private():
    namesake = official_entity(
        "parliament", "4242", name="Beatriz Fictícia Lemos", kind="person", classification=""
    )
    output = run(FakeEp([mep(PERSON, "Beatriz Fictícia", "Lemos", full_member())]))
    assert "a aguardar revisão de identidade=1" in output
    assert not SourceIdentity.objects.filter(source="ep", external_id=PERSON).exists()
    suggestion = IdentitySuggestion.objects.get(scheme="ep", external_id=PERSON)
    assert (suggestion.candidate, suggestion.status) == (namesake, "pending")
    candidates = SourceObservation.objects.filter(scope=f"ep:{PERSON}", is_current=True)
    assert candidates.count() == 4
    assert set(candidates.values_list("identity", "subject_name", "subject_reference")) == {
        (None, "Beatriz Fictícia Lemos", f"ep:{PERSON}")
    }
    assert not candidates.exclude(relationship=None).exists()
    assert not Relationship.objects.filter(subject__kind="person").exists()


@pytest.mark.django_db
def test_bodies_are_versioned_per_term_with_english_fallback_names():
    memberships: list[JSONValue] = [
        mandate(PERSON, 9, "m-9"),
        mandate(PERSON, 10, "m-10"),
        membership(
            PERSON,
            "f-1",
            "org/6001",
            "MEMBER",
            "EU_POLITICAL_GROUP",
            start="2019-07-02",
            end="2024-07-15",
        ),
        membership(PERSON, "f-2", "org/7001", "MEMBER", "EU_POLITICAL_GROUP"),
    ]
    run(FakeEp([mep(PERSON, "Tomás Inventado", "Reis", memberships)]))
    older, current = holder("org:6001"), holder("org:7001")
    assert older != current
    assert older.name == "Fictional Group of Examples (9.ª legislatura do PE)"
    assert current.name == "Grupo Fictício dos Exemplos (10.ª legislatura do PE)"
    claims = Relationship.objects.filter(subject=holder(PERSON))
    assert set(claims.values_list("object", "kind", "term__code", "temporal_status")) == {
        (older.pk, "membership", "EP9", "ended"),
        (current.pk, "membership", "EP10", "current"),
        (holder("institution:parlamento-europeu").pk, "public_office", "EP9", "ended"),
        (holder("institution:parlamento-europeu").pk, "public_office", "EP10", "current"),
    }
    assert set(claims.values_list("status", flat=True)) == {"published"}
    assert Term.objects.get(code="EP9").end_date == date(2024, 7, 15)


@pytest.mark.django_db
def test_removed_membership_ceases_on_reimport():
    run(FakeEp([mep(PERSON, "Beatriz Fictícia", "Lemos", full_member())]))
    claim = Relationship.objects.get(object=holder("org:7003"))
    assert claim.status == "published"
    reduced = [
        item
        for item in full_member()
        if not (isinstance(item, dict) and item["identifier"] == f"{PERSON}-f-3")
    ]
    output = run(FakeEp([mep(PERSON, "Beatriz Fictícia", "Lemos", reduced)]), as_of=LATER)
    assert "cessados=2" in output  # the membership and the delegation's structure claim
    claim.refresh_from_db()
    assert claim.status == "draft"
    assert not Evidence.objects.filter(relationship=claim, is_public=True).exists()
    kept = Relationship.objects.filter(subject=holder(PERSON)).exclude(pk=claim.pk)
    assert kept.count() == 3
    assert all(item.status == "published" for item in kept)


@pytest.mark.django_db
def test_rate_limit_and_transient_server_error_are_retried_with_backoff():
    person = mep(PERSON, "Beatriz Fictícia", "Lemos", [mandate(PERSON)])
    fake = FakeEp([person], failures=(429, 503))
    with patch("ligacoes.core.european_parliament.time.sleep") as sleep:
        output = run(fake, apply=False)
    # Retry-After is honoured for 429; a 5xx backs off by attempt (second attempt: 20 s).
    assert [item.args for item in sleep.call_args_list] == [(7,), (20,)]
    assert fake.requested[0] == fake.requested[1] == fake.requested[2]
    assert "1 deputados/as" in output


@pytest.mark.django_db
def test_persistent_server_errors_abort_without_writes():
    person = mep(PERSON, "Beatriz Fictícia", "Lemos", [mandate(PERSON)])
    fake = FakeEp([person], failures=(500,) * ep.MAX_ATTEMPTS)
    with (
        patch("ligacoes.core.european_parliament.time.sleep"),
        pytest.raises(CommandError, match="recusou pedidos repetidamente"),
    ):
        run(fake)
    assert len(fake.requested) == ep.MAX_ATTEMPTS
    assert not Entity.objects.exists()
