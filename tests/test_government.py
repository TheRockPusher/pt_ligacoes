import importlib
import json
import time
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError

from ligacoes.core.government import (
    AREA_TEMPLATES,
    EDGE,
    MAX_BYTES,
    ORIGIN,
    TEMPLATE_NAMES,
    GovernmentConfig,
    GovernmentImportError,
    GovernmentSnapshot,
    JSONObject,
    JSONValue,
    _Collector,
    _object,
    apply_snapshot,
    discover_context_id,
    parse_bootstrap,
    parse_snapshot,
    validate_url,
)
from ligacoes.core.models import (
    Entity,
    EntityAlias,
    Evidence,
    IdentitySuggestion,
    ParliamentStatusInterval,
    Relationship,
    ReviewEvent,
    Source,
    SourceIdentity,
    SourceObservation,
    SourceSyncState,
    Term,
)
from ligacoes.core.services import withdraw_relationship
from ligacoes.public.selectors import public_relationships

DAY = date(2026, 9, 25)
START = date(2025, 6, 6)


def guid(number: int) -> str:
    return str(UUID(int=number))


def config() -> GovernmentConfig:
    return GovernmentConfig(
        government="gc25",
        government_id=guid(1),
        prime_minister_id=guid(100),
        government_name="Governo Fictício",
        start_date=date(2025, 6, 5),
        end_date=None,
        templates=tuple(
            (name, guid(1000 + index)) for index, name in enumerate(sorted(TEMPLATE_NAMES))
        ),
        build_id="build-fictional",
        deployment="dpl=dpl_fictional123",
        app_url=ORIGIN + "/_next/static/chunks/pages/_app-12345678.js?dpl=dpl_fictional123",
    )


def field(value: JSONValue) -> JSONObject:
    return {"value": value}


def row(number: int, template: str, *, name: str | None = None) -> JSONObject:
    is_pm = template == "prime-minister"
    secretary = template == "secretary-of-state"
    path = (
        "/pt/gc25/primeiro-ministro"
        if is_pm
        else (
            "/pt/gc25/area-de-governo/pasta-ficticia/"
            + ("secretarios-de-estado/atividade-ficticia" if secretary else "ministro")
            + f"/Officials/Pessoa-Ficticia-{number}"
        )
    )
    areas: list[JSONValue] = []
    if not is_pm:
        if secretary:
            areas.append(
                {
                    "id": guid(301),
                    "template": {"id": AREA_TEMPLATES[1]},
                    "governmentTitle": field("Atividade Fictícia"),
                    "governmentPreposition": field("da"),
                }
            )
        areas.append(
            {
                "id": guid(300),
                "template": {"id": AREA_TEMPLATES[2]},
                "governmentTitle": field("Pasta Fictícia"),
                "governmentPreposition": field("da"),
            }
        )
    return {
        "id": guid(number),
        "template": {"id": dict(config().templates)[template]},
        "url": {"path": path},
        "isOfficialHidden": field(""),
        "official": {
            "jsonValue": {
                "id": guid(number + 100),
                "fields": {
                    "FullName": field(name or f"Pessoa Fictícia {number}"),
                    "BirthDate": field("PRIVATE_BIRTH_CANARY"),
                    "CardPhoto": field("PRIVATE_IMAGE_CANARY"),
                },
            }
        },
        "startDate": field("20250606T000000Z"),
        "endDate": field("00010101T000000Z"),
        "governmentRole": field(
            "Primeiro-Ministro" if is_pm else ("Secretária de Estado" if secretary else "Ministro")
        ),
        "ministryPage": areas,
    }


def profile(member: JSONObject) -> JSONObject:
    official = _object(_object(member["official"])["jsonValue"])
    role = _object(member["governmentRole"])["value"]
    name = _object(_object(official["fields"])["FullName"])["value"]
    secretary = _object(member["template"])["id"] == dict(config().templates)["secretary-of-state"]
    fields: JSONObject = {
        "OfficialsHistory": [
            {
                "id": member["id"],
                "fields": {
                    "Official": deepcopy(official),
                    "StartDate": field("2025-06-06T00:00:00Z"),
                    "EndDate": field("0001-01-01T00:00:00Z"),
                    "GovernmentRole": field(role),
                    "IsOfficialHidden": field(False),
                    "Bio": field("PRIVATE_BIOGRAPHY_CANARY"),
                },
            }
        ],
    }
    if secretary:
        fields.update({"GovernmentTitle": field("Atividade Fictícia"), "IsEndedTerm": field(False)})
    return {
        "pageProps": {
            "layoutData": {
                "sitecore": {
                    "route": {"itemId": guid(301 if secretary else 302), "fields": fields},
                    "context": {
                        "governmentContext": {
                            "governmentId": guid(1),
                            "governmentAreaId": guid(300),
                            "governmentAreaTitle": "Pasta Fictícia",
                            "officialInfo": {
                                "allOfficials": [
                                    {
                                        "itemId": member["id"],
                                        "officialId": official["id"],
                                        "officialName": name,
                                        "governmentRole": role,
                                        "startDate": "20250606T000000Z",
                                        "endDate": "00010101T000000Z",
                                    }
                                ]
                            },
                        }
                    },
                }
            }
        }
    }


def page(rows: list[JSONObject], *, total: int | None = None, more: bool = False) -> JSONObject:
    return {
        "data": {
            "childs": {
                "total": len(rows) if total is None else total,
                "pageInfo": {"hasNext": more, "endCursor": "fictional-cursor"},
                "results": list[JSONValue](rows),
            }
        }
    }


def source_rows() -> list[JSONObject]:
    return [row(100, "prime-minister"), row(101, "minister"), row(102, "secretary-of-state")]


def profiles_for(rows: list[JSONObject]) -> dict[str, JSONObject]:
    result: dict[str, JSONObject] = {}
    for member in rows:
        path = str(_object(member["url"])["path"])
        if "/Officials/" in path:
            result[ORIGIN + path.removeprefix("/pt").split("/Officials/")[0]] = profile(member)
    return result


def snapshot(*, as_of: date = DAY, rows: list[JSONObject] | None = None) -> GovernmentSnapshot:
    selected = rows if rows is not None else source_rows()
    return parse_snapshot(config(), (page(selected),), profiles_for(selected), as_of=as_of)


def bootstrap() -> bytes:
    settings = config()
    selectors: list[JSONValue] = [
        {"name": name, "fields": {"Title": field(identifier)}}
        for name, identifier in settings.templates
    ]
    payload = {
        "buildId": settings.build_id,
        "props": {
            "pageProps": {
                "layoutData": {
                    "sitecore": {
                        "context": {
                            "governmentContext": {
                                "governmentId": settings.government_id,
                                "primeMinisterId": settings.prime_minister_id,
                                "governmentName": settings.government_name,
                                "startDate": "20250605T000000Z",
                                "endDate": None,
                            }
                        },
                        "route": {
                            "placeholders": {
                                "content": [
                                    {
                                        "componentName": "SearchResultsComposite",
                                        "params": {"SearchSignature": "gc"},
                                        "fields": {
                                            "data": {
                                                "datasource": {
                                                    "SearchResultsRootItem": {
                                                        "jsonValue": [
                                                            {
                                                                "id": settings.government_id,
                                                                "name": "gc25",
                                                            }
                                                        ]
                                                    },
                                                    "SearchResultsByTemplate": {
                                                        "jsonValue": selectors
                                                    },
                                                }
                                            }
                                        },
                                    }
                                ]
                            }
                        },
                    }
                }
            }
        },
    }
    return (
        f'<script src="{settings.app_url}"></script>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    ).encode()


def test_bootstrap_discovers_current_build_and_all_templates_without_fixed_person_count():
    discovered = parse_bootstrap(bootstrap(), government="gc25")
    assert discovered == config()
    assert (
        discover_context_id(
            b'x.sitecoreEdgeContextId=e.env.SITECORE_EDGE_CONTEXT_ID||"fictionalContext123"'
        )
        == "fictionalContext123"
    )
    rows = source_rows()
    result = parse_snapshot(
        discovered,
        (page(rows[:2], total=3, more=True), page(rows[2:], total=3)),
        profiles_for(rows),
        as_of=DAY,
    )
    assert {m.official_id for m in result.members} == {guid(200), guid(201), guid(202)}
    secretary = next(m for m in result.members if m.template == "secretary-of-state")
    assert (secretary.portfolio_id, secretary.portfolio, secretary.area_id, secretary.area) == (
        guid(301),
        "Atividade Fictícia",
        guid(300),
        "Pasta Fictícia",
    )
    assert secretary.start_date == START
    assert secretary.end_date is None
    assert "PRIVATE_" not in repr(result)


@pytest.mark.parametrize(
    "problem",
    ["total", "pagination", "duplicate", "missing-profile", "unknown-template", "hidden", "date"],
)
def test_incomplete_or_ambiguous_composition_is_rejected(problem: str):
    rows = source_rows()
    profiles = profiles_for(rows)
    payload = page(rows)
    result = _object(_object(payload["data"])["childs"])
    if problem == "total":
        result["total"] = 4
    elif problem == "pagination":
        _object(result["pageInfo"])["hasNext"] = True
    elif problem == "duplicate":
        rows[2]["id"] = rows[1]["id"]
    elif problem == "missing-profile":
        profiles.pop(next(iter(profiles)))
    elif problem == "unknown-template":
        rows[2]["template"] = {"id": guid(99999)}
    elif problem == "hidden":
        rows[2]["isOfficialHidden"] = field("1")
    else:
        rows[2]["endDate"] = field("20260925T000000Z")
    with pytest.raises(GovernmentImportError):
        parse_snapshot(config(), (payload,), profiles, as_of=DAY)


def test_history_disagreement_does_not_guess_a_person_or_appointment():
    rows = source_rows()
    profiles = profiles_for(rows)
    member_profile = profiles[next(iter(profiles))]
    site = _object(_object(_object(member_profile["pageProps"])["layoutData"])["sitecore"])
    context = _object(_object(site["context"])["governmentContext"])
    context["governmentAreaId"] = guid(9876)
    with pytest.raises(GovernmentImportError):
        parse_snapshot(config(), (page(rows),), profiles, as_of=DAY)


def test_untrusted_bootstrap_cannot_replace_official_app_host():
    with pytest.raises(GovernmentImportError):
        parse_bootstrap(
            bootstrap().replace(ORIGIN.encode(), b"https://127.0.0.1"), government="gc25"
        )


@pytest.mark.django_db
def test_fictional_wire_cli_dry_run_writes_nothing_then_apply_auto_publishes(capsys):
    rows = source_rows()
    profiles = profiles_for(rows)
    settings = config()

    def offline_wire(_self, url: str, *, body: bytes | None = None) -> bytes:
        validate_url(url, "gc25", method="POST" if body is not None else "GET")
        if body is not None:
            request = json.loads(body)
            assert request["variables"]["cursor"] is None
            assert "2026-09-25T00:00:00Z" in request["query"]
            return json.dumps(page(rows)).encode()
        if url == settings.app_url:
            return b'x.sitecoreEdgeContextId=e.env.SITECORE_EDGE_CONTEXT_ID||"fictionalContext123"'
        if url == ORIGIN + "/gc25/governo/composicao":
            return bootstrap()
        prefix = ORIGIN + "/_next/data/" + settings.build_id
        source = ORIGIN + url.removeprefix(prefix).split(".json")[0]
        return json.dumps(profiles[source]).encode()

    with (
        patch("ligacoes.core.government._Collector.fetch", offline_wire),
        patch("ligacoes.core.government.timezone.localdate", return_value=DAY),
    ):
        call_command("import_government", as_of=DAY)
        assert not SourceObservation.objects.exists()
        assert not Entity.objects.exists()
        call_command("import_government", "--apply", as_of=DAY)
    offices_observed = SourceObservation.objects.filter(category="government_office")
    assert offices_observed.filter(is_current=True).count() == 3
    offices = Relationship.objects.filter(kind="public_office")
    assert offices.count() == 3
    assert (
        offices.filter(status="published", reviewed_by=None, reviewed_at__isnull=False).count() == 3
    )
    for relation in offices:
        assert relation.subject.is_public and relation.object.is_public
        events = ReviewEvent.objects.filter(relationship=relation)
        assert list(events.values_list("action", flat=True)) == ["auto_publish"]
        assert events.get().reviewer is None
    evidence = Evidence.objects.filter(relationship__in=offices)
    assert evidence.count() == 3
    assert not evidence.filter(is_public=False).exists()
    assert not Source.objects.filter(evidence__in=evidence, is_public=False).exists()
    assert public_relationships(at=DAY).filter(kind="public_office").count() == 3
    assert set(offices.values_list("temporal_status", flat=True)) == {"current"}
    assert set(
        Relationship.objects.filter(kind="part_of").values_list("temporal_status", flat=True)
    ) == {"current"}
    assert "Pessoa Fictícia" not in capsys.readouterr().out


@pytest.mark.django_db
def test_offline_apply_is_idempotent_and_same_name_never_merges():
    existing = Entity.objects.create(
        name="Pessoa Fictícia 101", kind="person", slug="manual-ficticia"
    )
    apply_snapshot(snapshot())
    first = SourceIdentity.objects.get(source="government", external_id=f"person:{guid(201)}")
    assert first.entity_id != existing.pk
    assert first.used_at is not None
    assert SourceIdentity.objects.get(external_id=f"portfolio:{guid(301)}").used_at is not None
    apply_snapshot(snapshot())
    # Three offices plus three portfolio-in-Government claims, once each.
    assert SourceObservation.objects.count() == 6
    assert Relationship.objects.count() == 6
    # The unrelated namesake, three people, three portfolios and the Government.
    assert Entity.objects.count() == 8


@pytest.mark.parametrize("government", ("gc21", "gc22", "gc23", "gc24", "gc25"))
def test_command_accepts_all_supported_governments(government):
    with patch("ligacoes.core.management.commands.import_government.fetch_snapshot") as fetch:
        fetch.return_value = replace(snapshot(), government=government)
        call_command("import_government", government=government, as_of=DAY)
    fetch.assert_called_once_with(government=government, as_of=DAY)


@pytest.mark.django_db
def test_government_appointment_corroborates_parliament_suspension():
    parsed = snapshot()
    member = parsed.members[0]
    person = Entity.objects.create(
        name=member.name, kind="person", slug="corroborated-deputy", is_public=True
    )
    SourceIdentity.objects.create(source="parliament", external_id="fictional-42", entity=person)
    ParliamentStatusInterval.objects.create(
        cadastro_id="fictional-42",
        entity=person,
        legislature="XVII",
        status="Suspenso",
        start=member.start_date,
        end=None,
    )
    apply_snapshot(parsed)
    identity = SourceIdentity.objects.get(
        source="government", external_id=f"person:{member.official_id}"
    )
    assert identity.entity == person
    assert (
        Relationship.objects.filter(
            subject=person, kind="public_office", status="published"
        ).count()
        == 1
    )
    assert EntityAlias.objects.filter(entity=person, name=member.name).exists()


@pytest.mark.django_db
def test_person_resolution_receives_every_appointment_interval():
    from ligacoes.core.identity import resolve_person

    parsed = snapshot()
    first = parsed.members[0]
    second = replace(
        first,
        appointment_id=guid(999),
        name="Pessoa Fictícia Nome Completo",
        start_date=first.start_date + timedelta(days=10),
    )
    with patch("ligacoes.core.government.resolve_person", wraps=resolve_person) as resolve:
        apply_snapshot(replace(parsed, members=(first, second)))
    assert resolve.call_count == 1
    contexts = resolve.call_args.kwargs["offices"]
    assert [office.start for office in contexts] == [first.start_date, second.start_date]
    assert all(office.institution.classification == "government" for office in contexts)
    identity = SourceIdentity.objects.get(
        source="government", external_id=f"person:{first.official_id}"
    )
    assert set(
        EntityAlias.objects.filter(entity=identity.entity).values_list("name", flat=True)
    ) == {first.name, second.name}


@pytest.mark.django_db
def test_exclusive_source_cessation_does_not_show_office_on_cessation_day():
    parsed = snapshot()
    cessation = DAY + timedelta(days=1)
    members = tuple(
        replace(member, end_date=cessation) if member.appointment_id == guid(102) else member
        for member in parsed.members
    )
    apply_snapshot(replace(parsed, members=members))
    observation = SourceObservation.objects.get(external_id=f"appointment:{guid(102)}")
    relation = observation.relationship
    assert relation is not None
    assert observation.effective_end == DAY
    assert relation.end_date == DAY
    assert f"limite exclusivo): {cessation.isoformat()}" in observation.passage
    assert relation.status == "published"
    assert public_relationships(at=DAY).filter(pk=relation.pk).exists()
    assert not public_relationships(at=cessation).filter(pk=relation.pk).exists()


@pytest.mark.django_db
def test_change_cessation_return_republish_and_never_overwrite_editorial_prose():
    apply_snapshot(snapshot())
    observation = SourceObservation.objects.get(external_id=f"appointment:{guid(102)}")
    relation = observation.relationship
    assert relation is not None
    assert relation.status == "published"
    relation.description = "Texto editorial fictício revisto."
    relation.save()
    evidence = observation.evidence
    assert evidence is not None
    assert evidence.is_public
    rows = source_rows()
    ceased = apply_snapshot(snapshot(rows=rows[:2], as_of=DAY + timedelta(days=1)))
    assert ceased["ceased"] == 1
    relation.refresh_from_db()
    evidence.refresh_from_db()
    assert relation.status == "draft"
    assert relation.reviewed_at is None
    assert relation.description == "Texto editorial fictício revisto."
    assert not evidence.is_public
    assert ReviewEvent.objects.filter(relationship=relation, action="invalidate").count() == 1
    returned = apply_snapshot(snapshot(as_of=DAY + timedelta(days=2)))
    assert returned["published"] == 1
    relation.refresh_from_db()
    evidence.refresh_from_db()
    assert relation.status == "published"
    assert relation.reviewed_by is None
    assert relation.description == "Texto editorial fictício revisto."
    assert evidence.is_public
    changed = source_rows()
    changed[2] = row(102, "secretary-of-state", name="Nome Fictício Corrigido")
    apply_snapshot(snapshot(rows=changed, as_of=DAY + timedelta(days=3)))
    assert SourceObservation.objects.filter(external_id=f"appointment:{guid(102)}").count() == 2
    current = SourceObservation.objects.get(external_id=f"appointment:{guid(102)}", is_current=True)
    assert current.passage.startswith("Nome Fictício Corrigido")
    relation.refresh_from_db()
    evidence.refresh_from_db()
    assert relation.status == "draft"
    assert not evidence.is_public
    assert current.relationship is not None
    assert current.relationship.status == "published"
    assert not public_relationships(at=DAY + timedelta(days=3)).filter(pk=relation.pk).exists()
    with pytest.raises(ValidationError):
        apply_snapshot(snapshot())


@pytest.mark.django_db
def test_withdrawn_office_is_never_republished_by_later_imports(reviewer):
    apply_snapshot(snapshot())
    observation = SourceObservation.objects.get(external_id=f"appointment:{guid(102)}")
    relation = observation.relationship
    assert relation is not None
    withdraw_relationship(relation, reviewer)
    assert apply_snapshot(snapshot())["published"] == 0
    relation.refresh_from_db()
    assert relation.status == "rejected"
    rows = source_rows()
    apply_snapshot(snapshot(rows=rows[:2], as_of=DAY + timedelta(days=1)))
    assert apply_snapshot(snapshot(as_of=DAY + timedelta(days=2)))["published"] == 0
    relation.refresh_from_db()
    assert relation.status == "rejected"
    assert not public_relationships(at=DAY + timedelta(days=2)).filter(pk=relation.pk).exists()


@pytest.mark.django_db
def test_failed_apply_rolls_back_new_identities_and_claims():
    with (
        patch(
            "ligacoes.core.government.sync_observations",
            side_effect=DatabaseError("falha fictícia"),
        ),
        pytest.raises(DatabaseError),
    ):
        apply_snapshot(snapshot())
    assert not Entity.objects.exists()
    assert not SourceIdentity.objects.exists()
    assert not SourceObservation.objects.exists()


@pytest.mark.parametrize(
    "url,method",
    [
        ("http://portugal.gov.pt/gc25/governo/composicao", "GET"),
        ("https://portugal.gov.pt.evil.example/gc25/governo/composicao", "GET"),
        ("https://user@portugal.gov.pt/gc25/governo/composicao", "GET"),
        ("https://portugal.gov.pt:443/gc25/governo/composicao", "GET"),
        ("https://portugal.gov.pt/gc25/governo/composicao?url=http://127.0.0.1", "GET"),
        ("https://portugal.gov.pt/_next/data/x/gc25/../private.json", "GET"),
        (
            "https://portugal.gov.pt/_next/data/x/gc25/area-de-governo/a/ministro.json?dpl=dpl_12345678&dpl=dpl_99999999",
            "GET",
        ),
        (EDGE + "?sitecoreContextId=fictionalContext123&query=mutation", "POST"),
        (EDGE + "?sitecoreContextId=fictionalContext123", "GET"),
        (
            "https://edge-platform.sitecorecloud.io/v1/private?sitecoreContextId=fictionalContext123",
            "POST",
        ),
    ],
)
def test_exact_routes_reject_untrusted_destinations(url: str, method: str):
    with pytest.raises(GovernmentImportError):
        validate_url(url, "gc25", method=method)


@pytest.mark.parametrize(
    "problem", ["redirect", "compressed", "oversized", "truncated", "request-limit"]
)
def test_fetch_limits_and_redirects_fail_closed(problem: str):
    response = MagicMock(status=200)
    response.__enter__.return_value = response
    headers: dict[str, str] = {}
    response.read1.side_effect = [b"{}", b""]
    if problem == "redirect":
        response.status = 302
        headers["Location"] = "https://127.0.0.1/gc25/governo/composicao"
    elif problem == "compressed":
        headers["Content-Encoding"] = "gzip"
    elif problem == "oversized":
        headers["Content-Length"] = str(MAX_BYTES + 1)
    elif problem == "truncated":
        headers["Content-Length"] = "100"
    response.getheader.side_effect = lambda name, default=None: headers.get(name, default)
    connection = MagicMock(sock=None)
    connection.getresponse.return_value = response
    collector = _Collector("gc25")
    if problem == "request-limit":
        collector.requests = 180
    with (
        patch("ligacoes.core.government.open_connection") as opened,
        pytest.raises(GovernmentImportError),
    ):
        opened.return_value.__enter__.return_value = connection
        collector.fetch(ORIGIN + "/gc25/governo/composicao")


def test_expired_collection_deadline_never_opens_a_socket():
    collector = _Collector("gc25")
    collector.deadline = time.monotonic() - 1
    with (
        patch("socket.socket", side_effect=AssertionError("Nenhuma rede após o prazo")),
        pytest.raises(GovernmentImportError),
    ):
        collector.fetch(ORIGIN + "/gc25/governo/composicao")


HISTORIC_START = date(2019, 10, 26)
HISTORIC_END = date(2022, 3, 30)
MOVE = date(2021, 12, 4)
ABOLISHED = date(2021, 2, 18)
AREA_A = "/gc22/area-de-governo/justica-ficticia/ministro"
AREA_B = "/gc22/area-de-governo/interna-ficticia/ministro"
PM_OFFICE = "/gc22/primeiro-ministro/secretarios-de-estado/assuntos-ficticios"
# Portfolio page path -> (page item, area id, area title, secretariat title, IsEndedTerm).
PORTFOLIO_PAGES: dict[str, tuple[int, int, str, str | None, bool | None]] = {
    AREA_A: (611, 610, "Justiça Fictícia", None, None),
    AREA_B: (621, 620, "Interna Fictícia", None, None),
    PM_OFFICE: (630, 500, "Primeiro-Ministro", "Assuntos Fictícios", True),
}


def historic_config() -> GovernmentConfig:
    return replace(
        config(),
        government="gc22",
        government_id=guid(2),
        prime_minister_id=guid(500),
        government_name="Governo Fictício Anterior",
        start_date=HISTORIC_START,
        end_date=HISTORIC_END,
    )


def stamp(day: date | None) -> str:
    return day.strftime("%Y%m%dT000000Z") if day else "00010101T000000Z"


def iso(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}T00:00:00Z"


def area(number: int, template: int, title: str) -> JSONObject:
    return {
        "id": guid(number),
        "template": {"id": AREA_TEMPLATES[template]},
        "governmentTitle": field(title),
        "governmentPreposition": field("da"),
    }


def appointment(
    number: int,
    template: str,
    *,
    person: int,
    role: str,
    path: str,
    areas: list[JSONValue],
    start: date,
    end: date | None,
) -> JSONObject:
    official = "" if template == "prime-minister" else f"/Officials/Pessoa-Ficticia-{person}"
    return {
        "id": guid(number),
        "template": {"id": dict(config().templates)[template]},
        "url": {"path": "/pt" + path + official},
        "isOfficialHidden": field("0"),
        "official": {
            "jsonValue": {
                "id": guid(person),
                "fields": {"FullName": field(f"Pessoa Fictícia {person - 100}")},
            }
        },
        "startDate": field(stamp(start)),
        "endDate": field(stamp(end)),
        "governmentRole": field(role),
        "ministryPage": areas,
    }


def historic_rows() -> list[JSONObject]:
    area_a = area(610, 2, "Justiça Fictícia")
    return [
        appointment(
            500,
            "prime-minister",
            person=900,
            role="Primeiro-Ministro",
            path="/gc22/primeiro-ministro",
            areas=[],
            start=HISTORIC_START,
            end=HISTORIC_END,
        ),
        appointment(
            501,
            "minister",
            person=901,
            role="Ministra",
            path=AREA_A,
            areas=[area_a],
            start=HISTORIC_START,
            end=MOVE,
        ),
        # From the exclusive boundary day, one person holds two portfolios at once
        # (the same global person id as the gc25 minister fixture).
        appointment(
            502,
            "minister",
            person=201,
            role="Ministro",
            path=AREA_A,
            areas=[area_a],
            start=MOVE,
            end=HISTORIC_END,
        ),
        appointment(
            503,
            "minister",
            person=201,
            role="Ministro",
            path=AREA_B,
            areas=[area(620, 2, "Interna Fictícia")],
            start=HISTORIC_START,
            end=HISTORIC_END,
        ),
        # A Secretary of State in the PM's office, whose portfolio was abolished mid-term.
        appointment(
            504,
            "secretary-of-state",
            person=903,
            role="Secretário de Estado",
            path=PM_OFFICE,
            areas=[area(630, 1, "Assuntos Fictícios")],
            start=HISTORIC_START,
            end=ABOLISHED,
        ),
    ]


def historic_profiles(rows: list[JSONObject]) -> dict[str, JSONObject]:
    profiles: dict[str, JSONObject] = {}
    for path, (item, area_id, area_title, title, ended) in PORTFOLIO_PAGES.items():
        history: list[JSONValue] = []
        summaries: list[JSONValue] = []
        for member in rows:
            if not str(_object(member["url"])["path"]).startswith(f"/pt{path}/Officials/"):
                continue
            official = _object(_object(member["official"])["jsonValue"])
            start = str(_object(member["startDate"])["value"])
            end = str(_object(member["endDate"])["value"])
            role = _object(member["governmentRole"])["value"]
            history.append(
                {
                    "id": member["id"],
                    "fields": {
                        "Official": deepcopy(official),
                        "StartDate": field(iso(start)),
                        "EndDate": field(iso(end)),
                        "GovernmentRole": field(role),
                        "IsOfficialHidden": field(False),
                    },
                }
            )
            summaries.append(
                {
                    "itemId": member["id"],
                    "officialId": official["id"],
                    "officialName": _object(_object(official["fields"])["FullName"])["value"],
                    "governmentRole": role,
                    "startDate": start,
                    "endDate": end,
                }
            )
        route_fields: JSONObject = {"OfficialsHistory": history}
        if title is not None:
            route_fields["GovernmentTitle"] = field(title)
        if ended is not None:
            route_fields["IsEndedTerm"] = field(ended)
        profiles[ORIGIN + path] = {
            "pageProps": {
                "layoutData": {
                    "sitecore": {
                        "route": {"itemId": guid(item), "fields": route_fields},
                        "context": {
                            "governmentContext": {
                                "governmentId": guid(2),
                                "governmentAreaId": guid(area_id),
                                "governmentAreaTitle": area_title,
                                "officialInfo": {"allOfficials": summaries},
                            }
                        },
                    }
                }
            }
        }
    return profiles


def historic_snapshot() -> GovernmentSnapshot:
    rows = historic_rows()
    return parse_snapshot(historic_config(), (page(rows),), historic_profiles(rows), as_of=DAY)


def test_past_government_keeps_its_full_history_in_documented_historic_shapes():
    parsed = historic_snapshot()
    members = {member.appointment_id: member for member in parsed.members}
    assert len(members) == 5
    # The PM page is the appointment itself; "/acerca" is not assumed.
    assert members[guid(500)].source_url == ORIGIN + "/gc22/primeiro-ministro"
    secretariat = members[guid(504)]
    assert (
        secretariat.portfolio_id,
        secretariat.portfolio,
        secretariat.area_id,
        secretariat.area,
        secretariat.source_url,
    ) == (guid(630), "Assuntos Fictícios", guid(500), "Primeiro-Ministro", ORIGIN + PM_OFFICE)
    assert secretariat.end_date == ABOLISHED
    held = [m.portfolio_id for m in parsed.members if m.official_id == guid(201)]
    assert held == [guid(610), guid(620)]
    assert (parsed.start_date, parsed.end_date) == (HISTORIC_START, HISTORIC_END)


@pytest.mark.parametrize(
    "problem", ["overlap", "abolished-but-open", "pm-office-with-ministry", "missing-history"]
)
def test_historic_relaxations_keep_portfolio_invariants(problem: str):
    rows = historic_rows()
    if problem == "overlap":
        rows[2]["startDate"] = field(stamp(MOVE - timedelta(days=3)))
    elif problem == "abolished-but-open":
        rows[4]["endDate"] = field(stamp(None))
    elif problem == "pm-office-with-ministry":
        rows[4]["ministryPage"] = [
            area(630, 1, "Assuntos Fictícios"),
            area(610, 2, "Justiça Fictícia"),
        ]
    profiles = historic_profiles(rows)
    if problem == "missing-history":
        profiles = historic_profiles([row for row in rows if row["id"] != guid(502)])
    with pytest.raises(GovernmentImportError):
        parse_snapshot(historic_config(), (page(rows),), profiles, as_of=DAY)


RESHUFFLE = date(2020, 10, 15)


@pytest.mark.parametrize(
    "shape,accepted",
    [
        ("reissued", True),
        ("gap", False),
        ("other-person", False),
        ("other-role", False),
        ("open-ended", False),
    ],
)
def test_reissued_appointment_missing_from_page_history(shape: str, accepted: bool):
    # A reshuffle reissues the same person's appointment to the same portfolio; the page
    # lists only the continuing item, the composition still lists the earlier one.
    rows = historic_rows()
    rows[1]["startDate"] = field(stamp(RESHUFFLE))
    earlier = appointment(
        505,
        "minister",
        person=901,
        role="Ministra",
        path=AREA_A,
        areas=[area(610, 2, "Justiça Fictícia")],
        start=HISTORIC_START,
        end=RESHUFFLE,
    )
    if shape == "gap":
        earlier["endDate"] = field(stamp(RESHUFFLE - timedelta(days=1)))
    elif shape == "other-person":
        earlier = appointment(
            505,
            "minister",
            person=904,
            role="Ministra",
            path=AREA_A,
            areas=[area(610, 2, "Justiça Fictícia")],
            start=HISTORIC_START,
            end=RESHUFFLE,
        )
    elif shape == "other-role":
        earlier["governmentRole"] = field("Ministra de Estado")
    elif shape == "open-ended":
        earlier["endDate"] = field(stamp(None))
    profiles = historic_profiles(rows)
    rows.append(earlier)
    if not accepted:
        with pytest.raises(GovernmentImportError):
            parse_snapshot(historic_config(), (page(rows),), profiles, as_of=DAY)
        return
    parsed = parse_snapshot(historic_config(), (page(rows),), profiles, as_of=DAY)
    held = sorted((m.start_date, m.end_date) for m in parsed.members if m.official_id == guid(901))
    assert held == [(HISTORIC_START, RESHUFFLE), (RESHUFFLE, MOVE)]


@pytest.mark.django_db
def test_past_government_apply_links_offices_to_its_term_and_portfolios():
    apply_snapshot(historic_snapshot())
    term = Term.objects.get(kind="government", code="gc22")
    government = SourceIdentity.objects.get(source="government", external_id="government:gc22")
    assert term.institution == government.entity
    assert (government.entity.name, government.entity.classification) == (
        "Governo Fictício Anterior",
        "government",
    )
    # The successor's first day is the source end; the term ends the day before.
    assert (term.start_date, term.end_date) == (HISTORIC_START, HISTORIC_END - timedelta(days=1))
    offices = Relationship.objects.filter(kind="public_office")
    assert offices.filter(status="published", term=term, temporal_status="ended").count() == 5
    assert {(r.role, r.role_class) for r in offices} == {
        ("Primeiro-Ministro", "leadership"),
        ("Ministra", "leadership"),
        ("Ministro", "leadership"),
        ("Secretário de Estado", "deputy_leadership"),
    }
    abolished = SourceIdentity.objects.get(external_id=f"portfolio:{guid(630)}").entity
    assert offices.get(object=abolished).end_date == ABOLISHED - timedelta(days=1)
    person = SourceIdentity.objects.get(external_id=f"person:{guid(201)}").entity
    assert len({office.object_id for office in offices.filter(subject=person)}) == 2
    structure = Relationship.objects.filter(kind="part_of")
    assert (
        structure.filter(
            status="published", object=government.entity, term=term, temporal_status="ended"
        ).count()
        == 4
    )
    assert {r.subject.classification for r in structure} == {"government_department"}
    assert set(
        SourceObservation.objects.filter(category="organisation_structure").values_list(
            "scope", flat=True
        )
    ) == {"government-structure:gc22"}


@pytest.mark.django_db
def test_person_id_shared_by_two_governments_is_one_person():
    apply_snapshot(historic_snapshot())
    apply_snapshot(snapshot())
    person = SourceIdentity.objects.get(source="government", external_id=f"person:{guid(201)}")
    offices = Relationship.objects.filter(
        subject=person.entity, kind="public_office", status="published"
    )
    assert offices.count() == 3
    assert set(offices.values_list("term__code", flat=True)) == {"gc22", "gc25"}
    assert Entity.objects.filter(kind="person", name="Pessoa Fictícia 101").count() == 1
    assert not IdentitySuggestion.objects.exists()


@pytest.mark.django_db
def test_current_government_claims_carry_role_term_and_portfolio_structure():
    # A portfolio already known keeps its editorial classification.
    known = Entity.objects.create(
        name="Pasta Fictícia", kind="organisation", classification="public_body", slug="pasta-f"
    )
    SourceIdentity.objects.create(
        source="government", external_id=f"portfolio:{guid(300)}", entity=known
    )
    apply_snapshot(snapshot())
    term = Term.objects.get(kind="government", code="gc25")
    assert (term.label, term.start_date, term.end_date) == (
        "Governo Fictício",
        date(2025, 6, 5),
        None,
    )
    offices = SourceObservation.objects.filter(category="government_office")
    assert set(offices.values_list("scope", "dataset")) == {("government:gc25", "gov_composicao")}
    secretary = offices.get(external_id=f"appointment:{guid(102)}").relationship
    assert secretary is not None
    assert (
        secretary.role,
        secretary.role_class,
        secretary.term,
        secretary.temporal_status,
        secretary.status,
    ) == ("Secretária de Estado", "deputy_leadership", term, "current", "published")
    minister = offices.get(external_id=f"appointment:{guid(101)}").relationship
    assert minister is not None
    assert (minister.role_class, minister.object) == ("leadership", known)
    known.refresh_from_db()
    assert known.classification == "public_body"
    portfolio = SourceObservation.objects.get(
        scope="government-structure:gc25", external_id=f"portfolio:{guid(301)}"
    )
    assert portfolio.relationship is not None
    assert portfolio.relationship.status == "published"
    assert portfolio.relationship.object == term.institution
    assert portfolio.relationship.subject.classification == "government_department"
    assert "Área governativa: Pasta Fictícia" in portfolio.passage


@pytest.mark.django_db
def test_uncorroborated_public_namesake_gets_advisory_suggestion_not_a_merge():
    namesake = Entity.objects.create(
        name="Pessoa Fictícia 102", kind="person", slug="homonimo-ficticio", is_public=True
    )
    assert apply_snapshot(snapshot())["published"] == 3
    suggestion = IdentitySuggestion.objects.get()
    assert (suggestion.external_id, suggestion.candidate, suggestion.status) == (
        f"person:{guid(202)}",
        namesake,
        "pending",
    )
    observation = SourceObservation.objects.get(external_id=f"appointment:{guid(102)}")
    assert observation.identity is not None
    assert observation.identity.entity_id != namesake.pk
    assert observation.identity.entity.is_public
    assert observation.relationship is not None
    assert observation.relationship.status == "published"
    assert observation.relationship.subject == observation.identity.entity
    assert (observation.subject_name, observation.subject_reference) == (
        "Pessoa Fictícia 102",
        f"person:{guid(202)}",
    )
    assert (
        SourceIdentity.objects.get(source="government", external_id=f"person:{guid(202)}")
        == observation.identity
    )


@pytest.mark.django_db
def test_scope_migration_moves_legacy_gc25_claims_without_duplicating_them():
    migration = importlib.import_module("ligacoes.core.migrations.0010_government_scopes")
    apply_snapshot(snapshot())
    # Return to the single-scope layout written before per-Government scopes.
    migration.backwards(django_apps, None)
    offices = SourceObservation.objects.filter(category="government_office")
    assert set(offices.values_list("scope", flat=True)) == {"current-government"}
    unrelated = SourceSyncState.objects.create(
        source="parliament", scope="current-government", as_of=DAY
    )
    migration.forwards(django_apps, None)
    assert set(offices.values_list("scope", flat=True)) == {"government:gc25"}
    assert SourceSyncState.objects.filter(source="government", scope="government:gc25").exists()
    unrelated.refresh_from_db()
    assert unrelated.scope == "current-government"
    result = apply_snapshot(snapshot())
    assert (result["created"], result["changed"], result["ceased"]) == (0, 0, 0)
    assert offices.count() == 3
    assert Relationship.objects.filter(kind="public_office").count() == 3
