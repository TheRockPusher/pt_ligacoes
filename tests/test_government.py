import json
import time
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.utils import timezone

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
    fetch_snapshot,
    parse_bootstrap,
    parse_snapshot,
    validate_url,
)
from ligacoes.core.models import (
    Entity,
    Evidence,
    Relationship,
    Source,
    SourceApproval,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.services import publish_relationship
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
@pytest.mark.parametrize("mode", ["--dry-run", "--apply"])
def test_cli_requires_recorded_approval_before_any_network_even_dry_run(mode: str):
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("Nenhuma rede autorizada")),
        pytest.raises(CommandError),
    ):
        call_command("import_government", mode, as_of=DAY)
    assert not Entity.objects.exists()
    assert not SourceObservation.objects.exists()


@pytest.mark.django_db
def test_other_source_approval_does_not_authorize_government(reviewer):
    SourceApproval.objects.create(
        source="ept",
        purpose="Finalidade fictícia",
        reuse_basis="Autorização fictícia",
        allowed_scopes=["declared_interest"],
        retention_conditions="Revisão fictícia",
        review_due_at=timezone.now() + timedelta(days=1),
        approved_by=reviewer,
        is_active=True,
    )
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("Nenhuma rede autorizada")),
        pytest.raises(PermissionDenied),
    ):
        fetch_snapshot(as_of=DAY)


@pytest.mark.django_db
def test_fictional_wire_cli_dry_run_then_apply_never_publishes(capsys, reviewer):
    SourceApproval.objects.create(
        source="government",
        purpose="Finalidade fictícia",
        reuse_basis="Autorização fictícia",
        allowed_scopes=["government_office"],
        retention_conditions="Revisão fictícia",
        review_due_at=timezone.now() + timedelta(days=1),
        approved_by=reviewer,
        is_active=True,
    )
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
    assert SourceObservation.objects.filter(is_current=True).count() == 3
    assert Relationship.objects.filter(kind="public_office", status="draft").count() == 3
    assert not Entity.objects.filter(is_public=True).exists()
    assert not Evidence.objects.filter(is_public=True).exists()
    assert not Source.objects.filter(is_public=True).exists()
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
    assert SourceObservation.objects.count() == 3
    assert Relationship.objects.count() == 3
    assert Entity.objects.count() == 7


@pytest.mark.django_db
def test_exclusive_source_cessation_does_not_show_office_on_cessation_day(reviewer):
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
    for entity in (relation.subject, relation.object):
        entity.is_public = True
        entity.save()
    evidence = observation.evidence
    assert evidence is not None
    evidence.source.is_public = True
    evidence.source.save()
    evidence.is_public = True
    evidence.save()
    publish_relationship(relation, reviewer)
    assert public_relationships(at=DAY).filter(pk=relation.pk).exists()
    assert not public_relationships(at=cessation).filter(pk=relation.pk).exists()


@pytest.mark.django_db
def test_change_cessation_return_withdraw_and_never_overwrite_editorial_prose(reviewer):
    apply_snapshot(snapshot())
    observation = SourceObservation.objects.get(external_id=f"appointment:{guid(102)}")
    relation = observation.relationship
    assert relation is not None
    relation.description = "Texto editorial fictício revisto."
    relation.save()
    for entity in (relation.subject, relation.object):
        entity.is_public = True
        entity.save()
    evidence = observation.evidence
    assert evidence is not None
    evidence.source.is_public = True
    evidence.source.save()
    evidence.is_public = True
    evidence.save()
    publish_relationship(relation, reviewer)
    rows = source_rows()
    apply_snapshot(snapshot(rows=rows[:2], as_of=DAY + timedelta(days=1)))
    relation.refresh_from_db()
    evidence.refresh_from_db()
    assert relation.status == "draft"
    assert relation.description == "Texto editorial fictício revisto."
    assert not evidence.is_public
    apply_snapshot(snapshot(as_of=DAY + timedelta(days=2)))
    relation.refresh_from_db()
    assert relation.status == "draft"
    assert relation.description == "Texto editorial fictício revisto."
    changed = source_rows()
    changed[2] = row(102, "secretary-of-state", name="Nome Fictício Corrigido")
    apply_snapshot(snapshot(rows=changed, as_of=DAY + timedelta(days=3)))
    assert SourceObservation.objects.filter(external_id=f"appointment:{guid(102)}").count() == 2
    assert SourceObservation.objects.get(
        external_id=f"appointment:{guid(102)}", is_current=True
    ).passage.startswith("Nome Fictício Corrigido")
    assert not Relationship.objects.filter(status="published").exists()
    with pytest.raises(ValidationError):
        apply_snapshot(snapshot())


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
        patch("ligacoes.core.government.require_source_approval"),
        patch("ligacoes.core.government.open_connection") as opened,
        pytest.raises(GovernmentImportError),
    ):
        opened.return_value.__enter__.return_value = connection
        collector.fetch(ORIGIN + "/gc25/governo/composicao")


def test_expired_collection_deadline_never_opens_a_socket():
    collector = _Collector("gc25")
    collector.deadline = time.monotonic() - 1
    with (
        patch("ligacoes.core.government.require_source_approval"),
        patch("socket.socket", side_effect=AssertionError("Nenhuma rede após o prazo")),
        pytest.raises(GovernmentImportError),
    ):
        collector.fetch(ORIGIN + "/gc25/governo/composicao")
