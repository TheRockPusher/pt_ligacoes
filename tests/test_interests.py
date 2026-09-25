from datetime import UTC, date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.utils import timezone

from ligacoes.core.enrichment import convert_observation
from ligacoes.core.interests import (
    ACTIVITIES,
    COMPANIES,
    INTEREST_TABLES,
    INTERESTS,
    ROOT,
    InterestsImportError,
    apply_snapshot,
    fetch_snapshot,
    parse_snapshot,
)
from ligacoes.core.models import (
    Entity,
    Relationship,
    SourceApproval,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue
from ligacoes.core.services import publish_relationship

DAY = date(2025, 7, 1)
SUBMITTED = "2025-06-20T12:30:00.123"


def node(key: str, value: JSONValue, **metadata: JSONValue) -> JSONObject:
    return {"key": key, "value": value, "isVisible": True, **metadata}


def activity(*, role: str = "Consultoria fictícia", empty: bool = False) -> JSONObject:
    values: list[JSONValue] = [
        role,
        "Empresa Aurora Fictícia",
        "Área fictícia",
        "PRIVATE_ADDRESS",
        "1",
        "2020-03-01T12:00:00Z",
        "2024-03-01T12:00:00Z",
        None,
        "PRIVATE_TAX_ID",
    ]
    return node(
        f"{ACTIVITIES}_0",
        [
            node(f"{ACTIVITIES}-col{i}", None if empty else value)
            for i, value in enumerate(values, 1)
        ],
        isEmpty=empty,
    )


def company(owner: JSONValue = "1") -> JSONObject:
    values: list[JSONValue] = [
        "Sociedade Luar Fictícia",
        "Área fictícia",
        "PRIVATE_ADDRESS",
        "PRIVATE_AMOUNT",
        "PRIVATE_PERCENTAGE",
        "2",
        "PRIVATE_TAX_ID",
        owner,
    ]
    return node(
        f"{COMPANIES}_0",
        [node(f"{COMPANIES}-col{i}", value) for i, value in enumerate(values, 1)],
        isEmpty=False,
    )


def child(parent: JSONObject, key: str) -> JSONObject:
    values = parent["value"]
    assert isinstance(values, list)
    for value in values:
        if isinstance(value, dict) and value.get("key") == key:
            return value
    raise AssertionError("Fictional fixture has no matching node")


def detail(
    *,
    professional: JSONObject | None = None,
    business: JSONObject | None = None,
    state: int = 8,
    identifier: int = 201,
) -> JSONObject:
    tables: list[JSONValue] = [
        node(ACTIVITIES, [professional if professional is not None else activity()]),
        node(COMPANIES, [business] if business is not None else []),
        node("cd0e811a-d9bb-49ee-b0f0-1dae0680eb22", "PRIVATE_ASSOCIATIONS"),
    ]
    return {
        "id": identifier,
        "holderId": 101,
        "entityId": 301,
        "boardId": 401,
        "roleId": 501,
        "holder": "Pessoa Aurora Inteiramente Fictícia",
        "entity": "Instituição Fictícia",
        "role": "Cargo Público Fictício",
        "submitedDate": SUBMITTED,
        "stateType": state,
        "natureType": 1,
        "relatedDeclarationId": None,
        "oppositionKeys": [],
        "unavailableSections": None,
        "nif": "PRIVATE_NIF",
        "attachments": ["PRIVATE_ATTACHMENT"],
        "startDate": "2025-05-01T00:00:00",
        "endDate": None,
        "data": node(
            ROOT,
            [
                node("private-income-assets", "PRIVATE_INCOME_AND_ASSETS"),
                node(INTERESTS, [node(INTEREST_TABLES, tables)]),
            ],
        ),
    }


def listing(*declarations: JSONObject, page_size: int = 10) -> list[JSONValue]:
    items: list[JSONValue] = [
        {
            "id": value["id"],
            "stateTypeId": value["stateType"],
            "natureTypeId": value["natureType"],
            "deliveryDate": value["submitedDate"],
            "relatedDeclarationId": value["relatedDeclarationId"],
        }
        for value in declarations
    ]
    count = (len(items) + page_size - 1) // page_size
    return [
        {
            "code": 0,
            "data": {
                "items": items[(number - 1) * page_size : number * page_size],
                "pageNumber": number,
                "pageSize": page_size,
                "pageCount": count,
                "total": len(items),
            },
        }
        for number in range(1, max(1, count) + 1)
    ]


def snapshot(identity: SourceIdentity, *declarations: JSONObject, as_of: date = DAY):
    return parse_snapshot(
        holder_id="101",
        identity=identity,
        pages=listing(*declarations),
        details={
            str(value["id"]): {"code": 0, "data": value}
            for value in declarations
            if value["stateType"] == 8
        },
        as_of=as_of,
    )


@pytest.fixture
def identity():
    return SourceIdentity(
        source="ept",
        external_id="101",
        entity=Entity(name="Pessoa Aurora Inteiramente Fictícia", kind="person"),
        reviewed_by_id=1,
        reviewed_at=datetime(2025, 1, 1, tzinfo=UTC),
        review_notes="Correspondência inteiramente fictícia para este teste.",
    )


def test_minimal_projection_keeps_activity_dates_separate_and_citation_reproducible(identity):
    result = snapshot(identity, detail(business=company()))
    professional, business = result.observations
    assert professional.kind == "professional_activity"
    assert professional.object is None
    assert professional.effective_start == date(2020, 3, 1)
    assert professional.effective_end == date(2024, 3, 1)
    assert professional.declared_on == date(2025, 6, 20)
    assert business.kind == "shareholding"
    assert business.object is None
    assert business.effective_start is None and business.effective_end is None
    assert "PRIVATE_" not in repr(result.observations)
    assert all(len(row.reference) <= 160 for row in result.observations)
    assert f"{ACTIVITIES}_0" in professional.reference
    assert "2025-06-20" in professional.title
    assert "Instituição Fictícia" in professional.passage
    assert "Cargo Público Fictício" in professional.passage


@pytest.mark.parametrize("key", [ROOT, INTERESTS, INTEREST_TABLES, ACTIVITIES])
def test_hidden_ancestor_never_leaks_visible_descendant(identity, key):
    declaration = detail()
    root = declaration["data"]
    assert isinstance(root, dict)
    target = root
    for part in (INTERESTS, INTEREST_TABLES, ACTIVITIES):
        if target["key"] == key:
            break
        target = child(target, part)
    target["isVisible"] = False
    assert snapshot(identity, declaration).observations == ()


@pytest.mark.parametrize("reason", [-1, 0, 1, 2, 3, 4, 5, 6, 7, 999])
def test_restricted_role_is_not_rendered_reason_text_or_claim(identity, reason):
    row = activity()
    role = child(row, f"{ACTIVITIES}-col1")
    role["reason"] = reason
    role["value"] = "PRIVATE_RESTRICTION_OR_CONTENT"
    assert snapshot(identity, detail(professional=row)).observations == ()


def test_unavailable_section_and_pending_opposition_withdraw_scope_without_guessing_shape(identity):
    declaration = detail()
    declaration["unavailableSections"] = [{"sectionKey": ACTIVITIES, "justification": "PRIVATE"}]
    assert snapshot(identity, declaration).observations == ()
    declaration = detail(business=company())
    declaration["oppositionKeys"] = [{"unknown-shape": "PRIVATE_OBJECTION"}]
    result = snapshot(identity, declaration)
    assert result.observations == ()
    assert result.restricted_sections == 2


def test_templates_and_blank_rows_never_become_interests(identity):
    assert snapshot(identity, detail(professional=activity(empty=True))).observations == ()
    row = activity(empty=True)
    row["isEmpty"] = False
    assert snapshot(identity, detail(professional=row)).observations == ()
    row = activity()
    row["isEmpty"] = True
    assert snapshot(identity, detail(professional=row)).observations == ()


@pytest.mark.parametrize("owner", [None, "", "3", "4", 3, 4])
def test_spouse_partner_and_unspecified_ownership_never_attach_to_holder(identity, owner):
    result = snapshot(identity, detail(professional=activity(empty=True), business=company(owner)))
    assert result.observations == ()


def test_hidden_ownership_does_not_infer_shareholding_from_company_or_amount(identity):
    row = company()
    child(row, f"{COMPANIES}-col8")["isVisible"] = False
    result = snapshot(identity, detail(professional=activity(empty=True), business=row))
    assert result.observations == ()


def test_date_timezone_ambiguity_retains_literal_without_false_precision(identity):
    row = activity()
    child(row, f"{ACTIVITIES}-col6")["value"] = "2020-06-30T23:00:00Z"
    observation = snapshot(identity, detail(professional=row)).observations[0]
    assert observation.effective_start is None
    assert "2020-06-30T23:00:00Z" in observation.passage
    assert observation.declared_on == date(2025, 6, 20)


@pytest.mark.parametrize("value", ["2020", "03/04/2020", "2020-02-31", "2025-06-20"])
def test_ambiguous_invalid_or_reversed_activity_dates_stop_snapshot(identity, value):
    row = activity()
    child(row, f"{ACTIVITIES}-col6")["value"] = value
    with pytest.raises(InterestsImportError):
        snapshot(identity, detail(professional=row))


def test_unknown_columns_and_duplicate_source_keys_fail_closed(identity):
    row = activity()
    values = row["value"]
    assert isinstance(values, list)
    values.append(node("unknown-field", "Unknown"))
    with pytest.raises(InterestsImportError):
        snapshot(identity, detail(professional=row))
    values.pop()
    values.append(node(f"{ACTIVITIES}-col1", "Duplicate"))
    with pytest.raises(InterestsImportError):
        snapshot(identity, detail(professional=row))


def test_identity_mismatch_never_uses_same_name_as_crosswalk(identity):
    declaration = detail()
    declaration["holderId"] = 102
    declaration["holder"] = identity.entity.name
    with pytest.raises(InterestsImportError):
        snapshot(identity, declaration)
    identity.reviewed_at = None
    with pytest.raises(InterestsImportError):
        snapshot(identity, detail())


@pytest.mark.parametrize("visibility", [None, "true", 1])
def test_unknown_visibility_does_not_default_to_public(identity, visibility):
    row = activity()
    child(row, f"{ACTIVITIES}-col1")["isVisible"] = visibility
    with pytest.raises(InterestsImportError):
        snapshot(identity, detail(professional=row))


def test_complete_scope_rejects_missing_pages_missing_details_and_duplicates(identity):
    first, second = detail(), detail(identifier=202)
    pages = listing(first, second, page_size=1)
    details: dict[str, JSONValue] = {"201": {"code": 0, "data": first}}
    with pytest.raises(InterestsImportError):
        parse_snapshot(
            holder_id="101", identity=identity, pages=pages[:1], details=details, as_of=DAY
        )
    with pytest.raises(InterestsImportError):
        parse_snapshot(holder_id="101", identity=identity, pages=pages, details=details, as_of=DAY)
    with pytest.raises(InterestsImportError):
        snapshot(identity, first, first)


@pytest.mark.parametrize("state", [1, 2, 4, 16, 32, 64])
def test_nonpublic_and_superseded_declarations_never_project(identity, state):
    assert snapshot(identity, detail(state=state)).observations == ()


@pytest.fixture
def approved_identity(db, reviewer):
    approval = SourceApproval.objects.create(
        source="ept",
        purpose="Ensaio exclusivamente fictício",
        reuse_basis="Autorização fictícia",
        allowed_scopes=["declared_interest"],
        retention_conditions="Eliminar no fim do teste",
        review_due_at=timezone.now() + timedelta(days=1),
        approved_by=reviewer,
        approved_at=timezone.now(),
        is_active=True,
    )
    person = Entity.objects.create(
        name="Pessoa Aurora Inteiramente Fictícia",
        slug="pessoa-aurora-ficticia",
        kind="person",
    )
    identity = SourceIdentity.objects.create(
        source="ept",
        external_id="101",
        entity=person,
        reviewed_by=reviewer,
        reviewed_at=timezone.now(),
        review_notes="Identidade fictícia revista com contexto oficial.",
    )
    return identity, approval


@pytest.mark.django_db
def test_missing_approval_blocks_before_any_network_even_dry_run():
    with patch("ligacoes.core.interests.open_connection") as connection:
        with pytest.raises(PermissionDenied):
            fetch_snapshot(holder_id="101")
        connection.assert_not_called()


def test_expired_approval_and_unreviewed_identity_block_before_network(approved_identity):
    identity, approval = approved_identity
    with (
        patch(
            "ligacoes.core.enrichment.timezone.now",
            return_value=approval.review_due_at + timedelta(seconds=1),
        ),
        patch("ligacoes.core.interests.open_connection") as connection,
    ):
        with pytest.raises(PermissionDenied):
            fetch_snapshot(holder_id="101")
        connection.assert_not_called()
    identity.reviewed_at = None
    identity.reviewed_by = None
    identity.save()
    with patch("ligacoes.core.interests.open_connection") as connection:
        with pytest.raises(ValidationError):
            fetch_snapshot(holder_id="101")
        connection.assert_not_called()


def fixture_post(declaration: JSONObject):
    def post(endpoint: str, body: JSONObject, *, holder_id: str, deadline: float) -> JSONValue:
        if endpoint == "/getallentities":
            return {"code": 0, "data": [{"value": 301, "label": "Instituição Fictícia"}]}
        if endpoint == "/getallroles":
            return {"code": 0, "data": [{"value": 501, "label": "Cargo Público Fictício"}]}
        if endpoint == "/search":
            return listing(declaration)[0]
        if endpoint == "/getdeclaration":
            return {"code": 0, "data": declaration}
        raise AssertionError("Unexpected endpoint in fictional fixture")

    return post


def test_cli_dry_run_and_apply_only_private_candidates(approved_identity):
    identity, _ = approved_identity
    with patch("ligacoes.core.interests._post", side_effect=fixture_post(detail())):
        call_command("import_interests", holder_id="101", stdout=StringIO())
        assert not SourceObservation.objects.exists()
        assert not Relationship.objects.exists()
        call_command("import_interests", holder_id="101", apply=True, stdout=StringIO())
    observation = SourceObservation.objects.get(is_current=True)
    assert observation.identity_id == identity.pk
    assert observation.object_id is None
    assert observation.relationship_id is None
    assert not Relationship.objects.exists()


def test_moving_complete_scope_is_rejected_before_apply(approved_identity):
    base = fixture_post(detail())
    calls = 0

    def changing_post(
        endpoint: str, body: JSONObject, *, holder_id: str, deadline: float
    ) -> JSONValue:
        nonlocal calls
        if endpoint == "/search":
            calls += 1
            if calls == 2:
                return listing()[0]
        return base(endpoint, body, holder_id=holder_id, deadline=deadline)

    with (
        patch("ligacoes.core.interests._post", side_effect=changing_post),
        pytest.raises(InterestsImportError),
    ):
        fetch_snapshot(holder_id="101")
    assert not SourceObservation.objects.exists()


def test_source_change_withdrawal_and_return_require_fresh_editorial_review(
    approved_identity, reviewer
):
    identity, _ = approved_identity
    reviewer.user_permissions.add(
        Permission.objects.get(content_type__app_label="core", codename="review_sourceobservation")
    )
    apply_snapshot(snapshot(identity, detail()))
    observation = SourceObservation.objects.get(is_current=True)
    organisation = Entity.objects.create(
        name="Empresa Aurora Fictícia",
        slug="empresa-aurora-ficticia",
        kind="company",
        is_public=True,
    )
    relationship = convert_observation(
        observation,
        reviewer,
        object=organisation,
        kind="professional_activity",
        description="Atividade fictícia com organização revista.",
        start_date=date(2020, 3, 1),
        end_date=date(2024, 3, 1),
        review_notes="Identidade, organização, tipo e datas verificados na fonte fictícia.",
    )
    observation.refresh_from_db()
    identity.entity.is_public = True
    identity.entity.save()
    evidence = observation.evidence
    assert evidence is not None
    evidence.source.is_public = True
    evidence.source.save()
    evidence.is_public = True
    evidence.save()
    publish_relationship(relationship, reviewer)
    changed = detail(professional=activity(role="Função fictícia corrigida"))
    apply_snapshot(snapshot(identity, changed))
    relationship.refresh_from_db()
    evidence.refresh_from_db()
    assert relationship.status != Relationship.Status.PUBLISHED
    assert not evidence.is_public
    apply_snapshot(snapshot(identity))
    assert not SourceObservation.objects.filter(is_current=True).exists()
    apply_snapshot(snapshot(identity, detail()))
    returned = SourceObservation.objects.get(is_current=True)
    assert returned.reviewed_at is None
    relationship.refresh_from_db()
    assert relationship.status != Relationship.Status.PUBLISHED


def test_repeat_and_excluded_field_changes_do_not_create_new_revisions(approved_identity):
    identity, _ = approved_identity
    first = snapshot(identity, detail())
    apply_snapshot(first)
    retained = SourceObservation.objects.get(is_current=True)
    row = activity()
    child(row, f"{ACTIVITIES}-col9")["value"] = "PRIVATE_CORRECTED_TAX_ID"
    second = snapshot(identity, detail(professional=row))
    apply_snapshot(second)
    assert second.observations[0].revision == first.observations[0].revision
    assert list(SourceObservation.objects.values_list("pk", flat=True)) == [retained.pk]
