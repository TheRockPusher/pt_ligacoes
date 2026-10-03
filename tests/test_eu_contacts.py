from collections import Counter
from datetime import date
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from ligacoes.core import eu_contacts
from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Event,
    IdentitySuggestion,
    SourceIdentity,
)
from ligacoes.core.official_http import OfficialHTTPError

AS_OF = date(2026, 9, 20)
MEP = "900001"
OTHER_MEP = "900099"
TR_PT = "111111111111-11"
TR_FOREIGN = "222222222222-22"
TR_SOLO = "333333333333-33"
TR_PENDING = "444444444444-44"
TR_UNI = "555555555555-55"
TR_ELSEWHERE = "666666666666-66"
MOEDAS_FILE = "meetingscommissionrepresentatives1419"


def registrant(tr_id: str, name: str, category: str, country: str, extra: str = "") -> str:
    return (
        "<interestRepresentative>"
        f"<identificationCode>{tr_id}</identificationCode>"
        f"<name><originalName>{name}</originalName></name><acronym/>"
        f"<webSiteURL>www.example.org{extra}</webSiteURL>"
        f"<registrationCategory>{category}</registrationCategory>"
        f"<headOffice><address>Rua Fictícia 1</address><country>{country}</country></headOffice>"
        "</interestRepresentative>"
    )


REGISTER_XML = (
    "<?xml version='1.1' encoding='UTF-8'?>"
    '<ListOfIRPublicDetail xmlns="http://intragate.ec.europa.eu/transparencyregister/odp">'
    '<metaData xmlns=""><numberOfIR>6</numberOfIR></metaData><resultList xmlns="">'
    + registrant(
        TR_PT,
        "Associação Fictícia de Exemplo",
        "Trade and business associations",
        "PORTUGAL",
        "&#x2;&#xb;&#x1d;&#11;",
    )
    + registrant(TR_FOREIGN, "Imaginary Widgets Inc.", "Companies &amp; groups", "BELGIUM")
    + registrant(TR_SOLO, "Consultor Inventado", "Self-employed individuals", "PORTUGAL")
    + registrant(TR_PENDING, "Grupo Pendente SA", "Companies &amp; groups", "PORTUGAL")
    + registrant(TR_UNI, "Universidade Imaginária", "Academic institutions", "PORTUGAL")
    + registrant(
        TR_ELSEWHERE, "Far Away Council", "Think tanks and research institutions", "FRANCE"
    )
    + "</resultList></ListOfIRPublicDetail>"
).encode()

EP_HEADER = (
    "title,member_id,member_name,meeting_date,member_capacity,procedure_reference,"
    "attendees,lobbyist_id\r\n"
)


def ep_csv(*rows: str) -> bytes:
    return (EP_HEADER + "".join(f"{row}\r\n" for row in rows)).encode()


EP_ROWS = ep_csv(
    # Resolved: MEP with an ep identity, organisation with a TR id.
    f"Mercado interno,{MEP},EXEMPLO Ana,2026-09-10,Rapporteur,2026/0001(COD),"
    f"Associação Fictícia de Exemplo,{TR_PT}",
    # A free-text organisation without a TR id stays unresolved.
    f"Energia,{MEP},EXEMPLO Ana,2026-09-11,Member,,Cooperativa Solar Fictícia Lda,",
    # A natural person (no organisation marker) is dropped; the TR organisation remains.
    f"Clima,{MEP},EXEMPLO Ana,2026-09-12,Member,,Imaginary Widgets Inc.|Joana Inventada,"
    f"{TR_FOREIGN}",
    # Only a natural person attended: no event.
    f"Reunião,{MEP},EXEMPLO Ana,2026-09-13,Member,,Rui Imaginário,",
    # Self-employed registrant: a natural person, dropped with its name.
    f"Consulta,{MEP},EXEMPLO Ana,2026-09-14,Member,,Consultor Inventado,{TR_SOLO}",
    # Future-dated, other MEP and test rows are dropped.
    f"Futuro,{MEP},EXEMPLO Ana,2026-09-29,Member,,Imaginary Widgets Inc.,{TR_FOREIGN}",
    f"Outro,{OTHER_MEP},OUTRO Rui,2026-09-10,Member,,Imaginary Widgets Inc.,{TR_FOREIGN}",
    "Tests de Prod,,,2026-09-10,Member,,Test,",
)


def ec_meeting(cabinet: str, day: str, subject: str, entities: str) -> str:
    return (
        f"<meeting><cabinet>{cabinet}</cabinet><date>{day}</date><location>Brussels</location>"
        f"<subject>{subject}</subject><entities>{entities}</entities>"
        "<representatives><representative><name>Carlos Moedas</name><title>Commissioner"
        "</title></representative><representative><name>Staff Inventado</name>"
        "<title>Cabinet member</title></representative></representatives></meeting>"
    )


def entity(name: str, tr_id: str) -> str:
    return f"<entity><name>{name}</name><id>{tr_id}</id></entity>"


CABINET = "Cabinet of Commissioner Carlos Moedas"
EC_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><meetings file_generated_on="2023-11-30">'
    + ec_meeting(
        CABINET,
        "2019-05-13",
        "Inova&amp;#231;&amp;#227;o &amp;quot;aberta&amp;quot;",
        entity("Associa&amp;#231;&#227;o Fict&#237;cia", TR_PT)
        + entity("Far Away Council", TR_ELSEWHERE),
    )
    + ec_meeting(
        CABINET,
        "2019-05-14",
        "Sem registo",
        entity("Imaginary Widgets Inc.", TR_FOREIGN) + entity("Fundação Sem Registo", ""),
    )
    + ec_meeting("Cabinet of Commissioner Someone Else", "2019-05-15", "Outro", entity("X", TR_PT))
    + ec_meeting(f"{CABINET} ", "2019-05-16", "Rótulo diferente", entity("X", TR_PT))
    + "</meetings>"
).encode()
EMPTY_EC = b'<?xml version="1.0" encoding="UTF-8"?><meetings file_generated_on="2026-09-20"/>'


def fake_download(url: str, **_: object) -> bytes:
    parts = urlsplit(url)
    if parts.hostname == "ec.europa.eu" and parts.path.startswith("/transparencyregister/"):
        return REGISTER_XML
    if parts.hostname == "www.europarl.europa.eu":
        query = parse_qs(parts.query)
        return EP_ROWS if query["fromDate"] == ["01/09/2026"] else ep_csv()
    if parts.hostname == "ec.europa.eu":
        return EC_XML if parse_qs(parts.query)["name"] == [MOEDAS_FILE] else EMPTY_EC
    if parts.hostname == "data.europarl.europa.eu":
        return b'{"data": [{"identifier": "%s", "label": "Ana EXEMPLO"}]}' % MEP.encode()
    raise AssertionError(url)


@pytest.fixture(autouse=True)
def network():
    with (
        patch.object(eu_contacts, "download", side_effect=fake_download),
        patch.object(eu_contacts, "REQUEST_INTERVAL", 0),
    ):
        yield


@pytest.fixture
def mep() -> Entity:
    return official_entity("ep", MEP, name="Ana Exemplo", kind="person", classification="")


def run(*args: str) -> str:
    out = StringIO()
    call_command("import_eu_contacts", *args, "--as-of", AS_OF.isoformat(), stdout=out)
    return out.getvalue()


def test_register_sanitises_invalid_references_and_keeps_minimal_fields() -> None:
    registrants = eu_contacts.parse_register(REGISTER_XML)

    assert set(registrants) == {TR_PT, TR_FOREIGN, TR_SOLO, TR_PENDING, TR_UNI, TR_ELSEWHERE}
    assert registrants[TR_PT].portuguese and not registrants[TR_FOREIGN].portuguese
    assert registrants[TR_SOLO].person


def test_classification_follows_register_category() -> None:
    assert eu_contacts.classify("Academic institutions", "Universidade Imaginária") == (
        "university",
        "higher_education",
    )
    assert eu_contacts.classify("Academic institutions", "Centro de Estudos") == (
        "organisation",
        "association",
    )
    assert eu_contacts.classify("Professional consultancies", "X")[1] == "company"
    assert eu_contacts.classify("Organisations representing churches", "X")[1] == "other"


@pytest.mark.django_db
def test_register_creates_portuguese_organisations_and_waits_for_pending_suggestions() -> None:
    candidate = Entity.objects.create(name="Grupo Pendente", kind="company", slug="grupo-pendente")
    IdentitySuggestion.objects.create(
        scheme="eu_tr",
        external_id=TR_PENDING,
        name_as_published="Grupo Pendente SA",
        candidate=candidate,
        basis="Wikidata",
    )

    run("--dataset", "register", "--apply")

    created = set(
        SourceIdentity.objects.filter(source="eu_tr").values_list("external_id", flat=True)
    )
    # Portuguese head offices only; self-employed (natural persons) and pending ids are not created.
    assert created == {TR_PT, TR_UNI}
    university = SourceIdentity.objects.get(source="eu_tr", external_id=TR_UNI).entity
    assert (university.kind, university.classification) == ("university", "higher_education")
    assert Entity.objects.filter(kind="company").count() == 1


@pytest.mark.django_db
def test_ep_meetings_publish_resolved_meetings_and_drop_persons(mep: Entity) -> None:
    run("--dataset", "ep-meetings", "--from", "2026-08", "--apply")

    events = {event.title: event for event in Event.objects.filter(dataset="ep_reunioes")}
    assert set(events) == {"Mercado interno", "Energia", "Clima"}
    resolved = events["Mercado interno"]
    assert resolved.status == Event.Status.PUBLISHED
    assert resolved.scope == "2026-09"
    assert resolved.details == {"capacity": "Rapporteur", "procedure": "2026/0001(COD)"}
    assert resolved.record_url.startswith(f"https://www.europarl.europa.eu/meps/pt/{MEP}/")
    assert {p.entity for p in resolved.parties.all()} == {
        mep,
        SourceIdentity.objects.get(source="eu_tr", external_id=TR_PT).entity,
    }
    # The cited foreign organisation is created from its TR id; the person is dropped.
    clima = events["Clima"]
    assert clima.status == Event.Status.PUBLISHED
    assert "Joana Inventada" not in {p.name for p in clima.parties.all()}
    unresolved = events["Energia"]
    assert unresolved.status == Event.Status.DRAFT
    assert unresolved.parties.filter(entity=None, name="Cooperativa Solar Fictícia Lda").exists()
    assert not Event.objects.filter(parties__name__in=["Consultor Inventado", "Rui Imaginário"])


@pytest.mark.django_db
def test_ep_meetings_cover_every_imported_mep(mep: Entity) -> None:
    official_entity("ep", OTHER_MEP, name="Rui Outro", kind="person", classification="")

    run("--dataset", "ep-meetings", "--from", "2026-09", "--apply")

    other = Event.objects.get(dataset="ep_reunioes", title="Outro")
    assert other.status == Event.Status.PUBLISHED


@pytest.mark.django_db
def test_ep_meeting_of_mep_without_identity_stays_draft() -> None:
    # No imported MEPs: the PT list comes from the EP API and the MEP stays unresolved.
    run("--dataset", "ep-meetings", "--from", "2026-09", "--apply")

    event = Event.objects.get(dataset="ep_reunioes", title="Mercado interno")
    assert event.status == Event.Status.DRAFT
    assert event.parties.filter(entity=None, identifier=f"ep:{MEP}").exists()
    assert not SourceIdentity.objects.filter(source="ep").exists()


def test_future_and_foreign_ep_rows_are_dropped() -> None:
    dropped: Counter[str] = Counter()

    meetings = eu_contacts.parse_ep_csv(
        EP_ROWS, members=frozenset({MEP}), as_of=AS_OF, dropped=dropped
    )

    assert "Futuro" not in {m.title for m in meetings}
    assert dropped == Counter({"ep_future_or_undated": 1, "ep_other_rows": 2})


def test_ec_parsing_keeps_only_the_exact_cabinet_and_unescapes_twice() -> None:
    meetings = eu_contacts.parse_ec_xml(EC_XML, key=MOEDAS_FILE, as_of=AS_OF, dropped=Counter())

    assert [m.day for m in meetings] == [date(2019, 5, 13), date(2019, 5, 14)]
    first = meetings[0]
    assert first.title == 'Inovação "aberta"'
    assert first.attendees == (
        eu_contacts.Attendee(tr_id=TR_PT, name="Associação Fictícia"),
        eu_contacts.Attendee(tr_id=TR_ELSEWHERE, name="Far Away Council"),
    )
    assert first.actor_id == "cabinet:cabinet-of-commissioner-carlos-moedas"


@pytest.mark.django_db
def test_ec_meetings_link_cabinet_and_registered_organisations() -> None:
    run("--dataset", "ec-meetings", "--apply")

    events = {e.title: e for e in Event.objects.filter(dataset="ce_reunioes")}
    assert set(events) == {'Inovação "aberta"', "Sem registo"}
    published = events['Inovação "aberta"']
    assert published.status == Event.Status.PUBLISHED
    host = published.parties.get(role="host")
    assert host.entity is not None and host.entity.classification == "eu_institution"
    assert published.parties.filter(role="attendee").count() == 2
    assert events["Sem registo"].status == Event.Status.DRAFT
    # Commissioners and cabinet staff are never stored as parties.
    assert not Event.objects.filter(parties__name__in=["Carlos Moedas", "Staff Inventado"])


@pytest.mark.django_db
def test_ec_scheme_identifies_only_organisations() -> None:
    with pytest.raises(ValidationError):
        official_entity("ec", "cabinet:x", name="Pessoa", kind="person", classification="")
    person = Entity.objects.create(name="Pessoa Inventada", kind="person", slug="pessoa-inventada")
    with pytest.raises(ValidationError):
        SourceIdentity(source="ec", external_id="cabinet:y", entity=person).full_clean()


@pytest.mark.django_db
def test_dry_run_writes_nothing() -> None:
    output = run("--dataset", "all", "--from", "2026-09")

    assert "Sem escritas" in output
    assert not Event.objects.exists() and not SourceIdentity.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("status", [202, 403])
def test_all_skips_blocked_ep_export_and_preserves_existing_meetings(
    mep: Entity, status: int
) -> None:
    run("--dataset", "ep-meetings", "--from", "2026-09", "--apply")
    existing = list(
        Event.objects.filter(dataset="ep_reunioes").values_list("pk", "status", "fingerprint")
    )
    attempts = []

    def blocked(url: str, **kwargs: object) -> bytes:
        if urlsplit(url).hostname == "www.europarl.europa.eu":
            attempts.append(url)
            if len(attempts) == 2:
                raise OfficialHTTPError(f"A fonte oficial devolveu HTTP {status}.")
            # A successful earlier month must not become a partial applied snapshot.
            return EP_ROWS
        return fake_download(url, **kwargs)

    out, err = StringIO(), StringIO()
    with patch.object(eu_contacts, "download", side_effect=blocked):
        call_command(
            "import_eu_contacts",
            "--dataset",
            "all",
            "--from",
            "2026-08",
            "--apply",
            as_of=AS_OF,
            stdout=out,
            stderr=err,
        )
    assert len(attempts) == 2
    assert "bloqueada" in err.getvalue() and "ep_reunioes ignorado" in err.getvalue()
    assert "0 reuniões PE em 0 meses" in out.getvalue()
    assert Event.objects.filter(dataset="ce_reunioes", status="published").exists()
    assert (
        list(Event.objects.filter(dataset="ep_reunioes").values_list("pk", "status", "fingerprint"))
        == existing
    )


@pytest.mark.django_db
def test_explicit_ep_meetings_reports_access_block_without_writes(mep: Entity) -> None:
    def blocked(url: str, **kwargs: object) -> bytes:
        if urlsplit(url).hostname == "www.europarl.europa.eu":
            raise OfficialHTTPError("A fonte oficial devolveu HTTP 202.")
        return fake_download(url, **kwargs)

    with (
        patch.object(eu_contacts, "download", side_effect=blocked),
        pytest.raises(CommandError, match="bloqueada"),
    ):
        run("--dataset", "ep-meetings", "--from", "2026-09", "--apply")
    assert not Event.objects.exists()
    assert not SourceIdentity.objects.filter(source="eu_tr").exists()


@pytest.mark.django_db
def test_all_does_not_suppress_unexpected_ep_failure(mep: Entity) -> None:
    def failed(url: str, **kwargs: object) -> bytes:
        if urlsplit(url).hostname == "www.europarl.europa.eu":
            raise OfficialHTTPError("A fonte oficial devolveu HTTP 500.")
        return fake_download(url, **kwargs)

    with (
        patch.object(eu_contacts, "download", side_effect=failed),
        pytest.raises(CommandError, match="bloqueio ou falha"),
    ):
        run("--dataset", "all", "--from", "2026-09", "--apply")
    assert not Event.objects.exists()
