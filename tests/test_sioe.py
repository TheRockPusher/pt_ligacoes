import json
from base64 import b64encode
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command

from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Evidence,
    Relationship,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.official_http import OfficialHTTPError
from ligacoes.core.parliament_parse import JSONObject
from ligacoes.core.sioe import (
    API_ROOT,
    APPLICATION_ID,
    CONFIG_URL,
    HISTORY_URL,
    LOGIN_URL,
    SEARCH_URL,
    classify,
)

DAY = date(2026, 9, 1)
LATER = date(2026, 9, 15)
TOKEN = "fictional.public.token"  # noqa: S105 - fictional public-flow token for tests.
# Fictional legal-person NIPCs with valid check digits.
NIPC_A = "500000018"
NIPC_B = "500000026"
NIPC_C = "500000034"
NIPC_D = "600000010"


def stamp(day: str) -> str:
    return f"{day}T00:00:00.0000000"


def item(
    code: str,
    name: str,
    *,
    nipc: str = "",
    extinct_on: str | None = None,
    ghost: bool = False,
) -> JSONObject:
    row: JSONObject = {
        "sioeCode": code,
        "name": f" {name}",
        "classId": 1,
        "class": "Entidade",
        "active": extinct_on is None,
        "state": "Extinto" if extinct_on else "-",
        "ghost": ghost,
        "submittedBy": "Submissor Fictício",
        "submittedAt": stamp("2026-01-01"),
    }
    if nipc:
        row["nipc"] = nipc
    if extinct_on:
        row["endDate"] = stamp(extinct_on)
    return row


def tutela(
    row_id: int,
    code: str,
    name: str,
    start: str,
    end: str | None = None,
    *,
    main: bool = True,
) -> JSONObject:
    row: JSONObject = {
        "governmentBody": 900,
        "governmentBodyName": name,
        "governmentBodyAbrev": code.rsplit("_", 1)[-1],
        "governmentBodyCode": code,
        "main": main,
        "isFromGov": "_" in code,
        "startDate": stamp(start),
        "historyEntry": end is not None,
        "id": row_id,
    }
    if end:
        row["endDate"] = stamp(end)
    return row


def member(
    row_id: int,
    name: str,
    position: str,
    *,
    start: str | None,
    end: str | None = None,
    expiry: str | None = None,
    cvurl: str = "",
) -> JSONObject:
    row: JSONObject = {
        "name": name,
        "positionName": f"{position} ",
        "genderName": "Feminino",
        "documentName": "cv-ficticio.pdf",
        "cvurl": cvurl,
        "historyEntry": end is not None,
        "id": row_id,
    }
    if start:
        row["startDate"] = f"{start} 00:00:00"
    if expiry:
        row["expiryDate"] = f"{expiry} 00:00:00"
    if end:
        row["endDate"] = stamp(end)
    return row


def history(
    code: str,
    *,
    type_id: int = 10,
    tutelas: tuple[JSONObject, ...] = (),
    parents: tuple[JSONObject, ...] = (),
    relationships: tuple[JSONObject, ...] = (),
    members: tuple[JSONObject, ...] = (),
) -> JSONObject:
    return {
        "sioeCode": code,
        "classID": 1,
        "ghost": False,
        "startDate": "1800-01-01 00:00:00",
        "scopes": [{"entityType": type_id, "entityTypeName": "Tipo fictício"}],
        "caes": [],
        "governmentBodies": [*tutelas],
        "aggregatorEntities": [*parents],
        "relationships": [*relationships],
        "managementBoardMembers": [*members],
        "contacts": [{"contact": "contacto@example.org"}],
        "addresses": [{"address": "Rua Fictícia"}],
    }


# A scripted failure before the real answer: a status (with Retry-After) or an exception.
type Hiccup = tuple[int, str] | OfficialHTTPError


class FakeSioe:
    """The public-token API over fictional records; no network."""

    def __init__(
        self,
        organisations: list[JSONObject],
        histories: dict[str, JSONObject],
        types: dict[int, list[str]] | None = None,
        hiccups: dict[str, list[Hiccup]] | None = None,
    ) -> None:
        self.organisations = organisations
        self.histories = histories
        self.types = types or {}
        self.hiccups = hiccups or {}
        self.calls: list[str] = []

    def __call__(self, url: str, **request: object) -> tuple[int, str, bytes]:
        if url == CONFIG_URL:
            self.calls.append("config")
            config = {"ROOT_API": API_ROOT, "APP_DEFAULT_APPLICATION_ID": APPLICATION_ID}
            return 200, "", ("\ufeff" + json.dumps(config)).encode()
        raw = request["body"]
        assert isinstance(raw, bytes)
        body = json.loads(raw)
        if url == LOGIN_URL:
            self.calls.append("login")
            assert body["auth_key"] == b64encode(APPLICATION_ID.encode()).decode()
            return 200, "", json.dumps({"access_token": TOKEN}).encode()
        headers = request["headers"]
        assert isinstance(headers, dict) and headers["Authorization"] == f"Bearer {TOKEN}"
        if url == HISTORY_URL:
            code = body["sioeCode"]
            self.calls.append(f"history:{code}")
            pending = self.hiccups.get(code)
            if pending:
                hiccup = pending.pop(0)
                if isinstance(hiccup, OfficialHTTPError):
                    raise hiccup
                return hiccup[0], hiccup[1], b""
            return 200, "", json.dumps(self.histories[code]).encode()
        assert url == SEARCH_URL
        self.calls.append("search")
        rows = self.organisations
        if "types" in body:
            tagged = self.types.get(body["types"][0], [])
            rows = [row for row in rows if row["sioeCode"] in tagged]
        page: JSONObject = {
            "items": [*rows],
            "pageNumber": 1,
            "totalPages": 1 if rows else 0,
            "totalCount": len(rows),
            "hasPreviousPage": False,
            "hasNextPage": False,
        }
        return 200, "", json.dumps(page).encode()


def run(
    fake: FakeSioe,
    *,
    as_of: date = DAY,
    apply: bool = True,
    limit: int = 0,
    cache_dir: Path | None = None,
) -> str:
    output = StringIO()
    arguments = ["--apply"] if apply else []
    if limit:
        arguments += ["--limit", str(limit)]
    if cache_dir is not None:
        arguments += ["--cache-dir", str(cache_dir)]
    with (
        patch("ligacoes.core.sioe._exchange", side_effect=fake),
        patch("ligacoes.core.sioe.PAUSE", 0),
    ):
        call_command("import_sioe", *arguments, as_of=as_of, stdout=output)
    return output.getvalue()


def sioe_entity(code: str) -> Entity:
    return SourceIdentity.objects.get(source="sioe", external_id=code).entity


def structure(subject: Entity, kind: str = "part_of") -> list[Relationship]:
    return list(
        Relationship.objects.filter(subject=subject, kind=kind).order_by("start_date", "pk")
    )


@pytest.mark.parametrize(
    ("type_id", "higher_education", "expected"),
    [
        (7, False, ("organisation", "government_office")),
        (30, False, ("organisation", "government_office")),
        (1, False, ("organisation", "public_body")),
        (10, False, ("organisation", "public_body")),
        (14, False, ("organisation", "regulator")),
        (69, False, ("organisation", "regulator")),
        (11, False, ("company", "state_company")),
        (46, False, ("company", "state_company")),
        (42, False, ("company", "state_company")),
        (23, False, ("organisation", "municipality")),
        (21, False, ("organisation", "parish")),
        (40, False, ("university", "higher_education")),
        (10, True, ("university", "higher_education")),
        (55, True, ("university", "higher_education")),
        (11, True, ("company", "state_company")),
        (55, False, ("organisation", "foundation")),
        (54, False, ("organisation", "association")),
        (39, False, ("organisation", "association")),
        (None, False, ("organisation", "public_body")),
        (999, False, ("organisation", "public_body")),
    ],
)
def test_new_organisations_are_classified_from_the_sioe_type_vocabulary(
    type_id, higher_education, expected
):
    assert classify(type_id, higher_education=higher_education) == expected


@pytest.mark.django_db
def test_dry_run_writes_nothing_and_apply_creates_classified_anchored_organisations():
    fake = FakeSioe(
        [
            item("900000001", "Empresa Fictícia de Testes, E.P.E.", nipc=NIPC_A),
            item("900000002", "Direção-Geral Fictícia Extinta", extinct_on="2013-01-01"),
            item("900000003", "Serviço Fictício Sem Data", extinct_on="1800-01-01"),
            item("900000009", "Entidade Fictícia Fantasma", ghost=True),
        ],
        {
            "900000001": history("900000001", type_id=11),
            "900000002": history("900000002", type_id=1),
            "900000003": history("900000003", type_id=1),
        },
        types={11: ["900000001"], 1: ["900000002", "900000003"]},
    )
    assert "sem escritas" in run(fake, apply=False)
    assert not Entity.objects.exists()
    assert not SourceIdentity.objects.exists()
    run(fake)
    company = sioe_entity("900000001")
    assert (company.name, company.kind, company.classification, company.is_public) == (
        "Empresa Fictícia de Testes, E.P.E.",
        "company",
        "state_company",
        True,
    )
    assert SourceIdentity.objects.get(source="nipc", external_id=NIPC_A).entity == company
    extinct = sioe_entity("900000002")
    assert (extinct.classification, extinct.dissolution_date) == ("public_body", date(2013, 1, 1))
    # A sentinel extinction date is unknown, never a fact.
    assert sioe_entity("900000003").dissolution_date is None
    assert not SourceIdentity.objects.filter(external_id="900000009").exists()


@pytest.mark.django_db
def test_existing_nipc_organisation_gains_the_sioe_identity_and_keeps_its_profile():
    existing = official_entity(
        "nipc", NIPC_B, name="Sociedade Fictícia Prévia", kind="company", classification="company"
    )
    fake = FakeSioe(
        [item("900000011", "Sociedade Fictícia, S.A.", nipc=NIPC_B)],
        {"900000011": history("900000011", type_id=46)},
        types={46: ["900000011"]},
    )
    run(fake)
    assert sioe_entity("900000011") == existing
    existing.refresh_from_db()
    assert (existing.name, existing.classification) == ("Sociedade Fictícia Prévia", "company")
    assert Entity.objects.count() == 1
    assert SourceIdentity.objects.filter(source="nipc", entity=existing).count() == 1


@pytest.mark.django_db
def test_shared_nipc_is_neither_attached_nor_used_to_merge_organisations():
    holder = official_entity(
        "nipc",
        NIPC_C,
        name="Organização Fictícia Prévia",
        kind="organisation",
        classification="public_body",
    )
    fake = FakeSioe(
        [
            item("900000021", "Escola Fictícia Um", nipc=NIPC_C),
            item("900000022", "Escola Fictícia Dois", nipc=NIPC_C),
            item("900000023", "Instituto Fictício Atual", nipc=NIPC_D),
            item("900000024", "Instituto Fictício Antigo", nipc=NIPC_D, extinct_on="2012-12-31"),
        ],
        {
            code: history(code, type_id=27)
            for code in ("900000021", "900000022", "900000023", "900000024")
        },
    )
    run(fake)
    first, second = sioe_entity("900000021"), sioe_entity("900000022")
    assert len({first.pk, second.pk, holder.pk}) == 3
    assert SourceIdentity.objects.get(source="nipc", external_id=NIPC_C).entity == holder
    # Unique among active entities: the current body owns it, its extinct namesake does not.
    owner = SourceIdentity.objects.get(source="nipc", external_id=NIPC_D).entity
    assert owner == sioe_entity("900000023")
    assert sioe_entity("900000024") != sioe_entity("900000023")


def ministry_fake() -> FakeSioe:
    finance = "Ministério das Finanças Fictícias"
    return FakeSioe(
        [item("900000031", "Instituto Fictício de Finanças", nipc=NIPC_A)],
        {
            "900000031": history(
                "900000031",
                tutelas=(
                    tutela(501, "10003", finance, "1800-01-01", "2025-06-05"),
                    tutela(502, "XXV_MFF", finance, "2025-06-05"),
                    tutela(
                        503,
                        "XV_SRFF",
                        "Secretaria Regional Fictícia (RAM)",
                        "2020-01-01",
                        "2024-06-06",
                        main=False,
                    ),
                ),
            )
        },
    )


@pytest.mark.django_db
def test_ministry_is_part_of_its_government_only_once_that_government_exists():
    run(ministry_fake())
    ministry = sioe_entity("gov:XXV_MFF")
    assert (ministry.name, ministry.classification) == (
        "Ministério das Finanças Fictícias (XXV Governo)",
        "government_department",
    )
    assert not structure(ministry)
    government = official_entity(
        "government",
        "government:gc25",
        name="XXV Governo Constitucional Fictício",
        kind="organisation",
        classification="government",
    )
    # A regional body shares the Roman-numeral code pattern but is never a national Government.
    official_entity(
        "government",
        "government:gc15",
        name="XV Governo Constitucional Fictício",
        kind="organisation",
        classification="government",
    )
    run(ministry_fake(), as_of=LATER)
    (link,) = structure(ministry)
    assert link.object == government
    assert link.status == Relationship.Status.PUBLISHED
    assert not structure(sioe_entity("gov:XV_SRFF"))


@pytest.mark.django_db
def test_supervision_claims_publish_with_tutela_role_and_source_dates():
    run(ministry_fake())
    institute = sioe_entity("900000031")
    # The legacy numeric row names no Government and is skipped.
    regional, current = structure(institute)
    assert current.object == sioe_entity("gov:XXV_MFF")
    assert (current.role, current.start_date, current.end_date, current.temporal_status) == (
        "Tutela",
        date(2025, 6, 5),
        None,
        "current",
    )
    assert regional.object == sioe_entity("gov:XV_SRFF")
    assert (regional.start_date, regional.end_date, regional.temporal_status) == (
        date(2020, 1, 1),
        date(2024, 6, 6),
        "ended",
    )
    for relation in (current, regional):
        assert relation.status == Relationship.Status.PUBLISHED
        evidence = Evidence.objects.get(relationship=relation)
        assert evidence.is_public and evidence.source.dataset == "sioe"
    reference = Evidence.objects.get(relationship=current).page_reference
    assert reference == "SIOE 900000031 / tutela 502"


@pytest.mark.django_db
def test_succession_points_from_successor_to_predecessor_and_aggregation_to_parent():
    fake = FakeSioe(
        [
            item("900000041", "Direção Fictícia Antiga", extinct_on="2013-01-01"),
            item("900000042", "Direção-Geral Fictícia Nova"),
            item("900000043", "Divisão Fictícia Integrada"),
        ],
        {
            "900000041": history("900000041", type_id=1),
            "900000042": history(
                "900000042",
                type_id=1,
                relationships=(
                    {
                        "relatedEntity": 1,
                        "sioeCode": "900000041",
                        "name": "Direção Fictícia Antiga",
                        "typeName": "Fusão",
                        "type": 2,
                        "startDate": stamp("2013-01-01"),
                        "id": 601,
                    },
                    {
                        "sioeCode": "900000043",
                        "name": "Divisão Fictícia Integrada",
                        "typeName": "Outra relação fictícia",
                        "startDate": stamp("2014-01-01"),
                        "id": 602,
                    },
                ),
            ),
            "900000043": history(
                "900000043",
                type_id=32,
                parents=(
                    {
                        "sioeCode": "900000042",
                        "name": "Direção-Geral Fictícia Nova",
                        "isChild": False,
                        "startDate": stamp("2015-02-01"),
                        "id": 701,
                    },
                ),
            ),
        },
    )
    run(fake)
    successor, predecessor = sioe_entity("900000042"), sioe_entity("900000041")
    (succession,) = Relationship.objects.filter(kind="succession")
    assert (succession.subject, succession.object) == (successor, predecessor)
    assert succession.start_date == date(2013, 1, 1)
    assert succession.status == Relationship.Status.PUBLISHED
    (aggregation,) = structure(sioe_entity("900000043"))
    assert (aggregation.object, aggregation.start_date) == (successor, date(2015, 2, 1))
    assert aggregation.status == Relationship.Status.PUBLISHED


@pytest.mark.django_db
def test_board_members_publish_with_source_scoped_people():
    fake = FakeSioe(
        [item("900000051", "Hospital Fictício, E.P.E.", nipc=NIPC_A)],
        {
            "900000051": history(
                "900000051",
                type_id=11,
                members=(
                    member(
                        801,
                        "Dra. Marta Fictícia Exemplo",
                        "Vogal Executivo",
                        start="2023-03-03",
                        expiry="2025-12-31",
                        cvurl="https://diariodarepublica.pt/dr/detalhe/despacho/1-2023-100000001",
                    ),
                    member(
                        802,
                        "Rui Fictício Modelo",
                        "Presidente",
                        start="1800-01-01",
                        end="2022-06-30",
                        cvurl="Despacho fictício n.º 1/2020 - Diário da República",
                    ),
                ),
            )
        },
        types={11: ["900000051"]},
    )
    run(fake)
    assert Relationship.objects.filter(kind="directorship", status="published").count() == 2
    assert Entity.objects.filter(kind="person").count() == 2
    current = SourceObservation.objects.get(external_id="membro:801")
    assert current.identity is not None
    assert current.relationship is not None
    assert current.evidence is not None
    assert current.identity.source == "scoped_name"
    assert current.relationship.status == "published"
    assert current.relationship.subject == current.identity.entity
    assert current.relationship.object == sioe_entity("900000051")
    assert current.evidence.is_public and current.evidence.source.is_public
    assert (current.subject_name, current.subject_reference) == (
        "Dra. Marta Fictícia Exemplo",
        "sioe:900000051:membro:801",
    )
    assert (current.category, current.kind, current.role, current.role_class) == (
        "office_holding",
        "directorship",
        "Vogal Executivo",
        "member",
    )
    assert current.object == sioe_entity("900000051")
    # The planned term end is context, never the end of the mandate.
    assert (current.effective_start, current.effective_end) == (date(2023, 3, 3), None)
    assert current.temporal_status == "unknown"
    assert "Termo previsto do mandato: 2025-12-31" in current.passage
    assert "https://diariodarepublica.pt/dr/detalhe/despacho/1-2023-100000001" in current.passage
    assert current.reference == "SIOE 900000051 / membro 801"
    ended = SourceObservation.objects.get(external_id="membro:802")
    assert (ended.effective_start, ended.effective_end, ended.temporal_status) == (
        None,
        date(2022, 6, 30),
        "ended",
    )
    assert ended.role_class == "leadership"
    for observation in (current, ended):
        assert "1800" not in observation.passage
        assert "Feminino" not in observation.passage
        assert "cv-ficticio" not in observation.passage
        assert "Despacho fictício" not in observation.passage


@pytest.mark.django_db
def test_absent_entity_scopes_cease_on_the_next_complete_run_but_not_a_partial_one():
    kept = item("900000061", "Instituto Fictício Mantido")
    gone = item("900000062", "Instituto Fictício Removido")
    histories = {
        code: history(
            code,
            tutelas=(tutela(900 + int(code[-1]), "XXV_MFF", "Ministério Fictício", "2025-06-05"),),
            members=(member(int(code[-2:]), "Pessoa Fictícia", "Presidente", start="2025-07-01"),),
        )
        for code in ("900000061", "900000062")
    }
    run(FakeSioe([kept, gone], histories))
    (claim,) = structure(sioe_entity("900000062"))
    assert claim.status == Relationship.Status.PUBLISHED
    # A limited crawl skips the second history but must not treat it as absent.
    output = run(FakeSioe([kept, gone], histories), as_of=LATER, limit=1)
    assert "parcial" in output
    claim.refresh_from_db()
    assert claim.status == Relationship.Status.PUBLISHED
    run(FakeSioe([kept], histories), as_of=LATER)
    claim.refresh_from_db()
    assert claim.status != Relationship.Status.PUBLISHED
    assert not SourceObservation.objects.filter(
        scope__endswith=":900000062", is_current=True
    ).exists()
    kept_rows = SourceObservation.objects.filter(scope__endswith=":900000061", is_current=True)
    assert kept_rows.count() == 2


def network_error() -> OfficialHTTPError:
    error = OfficialHTTPError("Não foi possível obter a fonte oficial em segurança.")
    error.__cause__ = TimeoutError()
    return error


@pytest.mark.django_db
def test_transient_failures_are_retried_with_backoff_retry_after_and_one_token_renewal():
    fake = FakeSioe(
        [item("900000071", "Instituto Fictício Instável")],
        {"900000071": history("900000071")},
        hiccups={"900000071": [(401, ""), network_error(), (503, "7"), (429, "")]},
    )
    pauses: list[float] = []
    with (
        patch("ligacoes.core.sioe.time.sleep", side_effect=pauses.append),
        patch("ligacoes.core.sioe.BACKOFF", 1.0),
    ):
        output = run(fake, apply=False)
    assert "históricos=1" in output
    assert fake.calls.count("login") == 2
    assert fake.calls.count("history:900000071") == 5
    # The backoff doubles; a longer Retry-After wins.
    assert [pause for pause in pauses if pause] == [1.0, 7.0, 4.0]


@pytest.mark.parametrize(
    ("hiccup", "detail"),
    [((400, ""), "HTTP 400"), ((503, "3600"), "pausa longa")],
)
@pytest.mark.django_db
def test_refusals_and_long_retry_after_stop_without_retrying(hiccup, detail):
    fake = FakeSioe(
        [item("900000091", "Instituto Fictício Recusado")],
        {"900000091": history("900000091")},
        hiccups={"900000091": [hiccup]},
    )
    with patch("ligacoes.core.sioe.time.sleep"), pytest.raises(CommandError, match=detail):
        run(fake, apply=False)
    assert fake.calls.count("history:900000091") == 1


@pytest.mark.django_db
def test_exhausted_retries_name_the_step_and_a_rerun_resumes_from_the_cache(tmp_path):
    records = [
        item("900000081", "Instituto Fictício Recolhido"),
        item("900000082", "Instituto Fictício Indisponível"),
    ]
    histories = {
        code: history(
            code,
            members=(
                member(int(code[-2:]), "Pessoa Fictícia Reservada", "Vogal", start="2025-01-01"),
            ),
        )
        for code in ("900000081", "900000082")
    }
    failing = FakeSioe(records, histories, hiccups={"900000082": [(503, "")] * 5})
    with patch("ligacoes.core.sioe.time.sleep"), pytest.raises(CommandError) as failure:
        run(failing, apply=False, cache_dir=tmp_path)
    message = str(failure.value)
    assert "histórico da entidade 900000082" in message
    assert "HTTP 503; 5 tentativas" in message and "--cache-dir" in message
    assert "Pessoa" not in message
    assert failing.calls.count("history:900000082") == 5
    resumed = FakeSioe(records, histories)
    with patch("ligacoes.core.sioe.time.sleep"):
        output = run(resumed, apply=False, cache_dir=tmp_path)
    assert "históricos=2" in output
    # Searches and the history already collected come from the cache.
    assert [call for call in resumed.calls if call not in {"config", "login"}] == [
        "history:900000082"
    ]
    assert not Entity.objects.exists()
    cached = "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    for dropped in ("Feminino", "cv-ficticio", "contacto@example.org", "Submissor"):
        assert dropped not in cached
