from datetime import UTC, date, datetime
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from ligacoes.core.interests import (
    ACTIVITIES,
    COMPANIES,
    INTEREST_TABLES,
    INTERESTS,
    OTHER_SITUATIONS,
    ROOT,
    SERVICES,
    SUPPORTS,
    InterestsImportError,
    apply_snapshot,
    fetch_snapshot,
    parse_snapshot,
)
from ligacoes.core.models import (
    Entity,
    Relationship,
    SourceIdentity,
    SourceObservation,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue
from ligacoes.core.services import withdraw_relationship

DAY = date(2025, 7, 1)
SUBMITTED = "2025-06-20T12:30:00.123"
# Fictional legal-person NIPCs (valid check digits) and a natural-person-shaped NIF.
NIPC = "501000119"
OTHER_NIPC = "990001237"
PERSON_NIF = "123456789"


def node(key: str, value: JSONValue, **metadata: JSONValue) -> JSONObject:
    return {"key": key, "value": value, "isVisible": True, **metadata}


def activity(
    *,
    role: str = "Consultoria fictícia",
    empty: bool = False,
    tax_id: JSONValue = "PRIVATE_TAX_ID",
) -> JSONObject:
    values: list[JSONValue] = [
        role,
        "Empresa Aurora Fictícia",
        "Área fictícia",
        "PRIVATE_ADDRESS",
        "1",
        "2020-03-01T12:00:00Z",
        "2024-03-01T12:00:00Z",
        None,
        tax_id,
    ]
    return node(
        f"{ACTIVITIES}_0",
        [
            node(f"{ACTIVITIES}-col{i}", None if empty else value)
            for i, value in enumerate(values, 1)
        ],
        isEmpty=empty,
    )


def company(owner: JSONValue = "1", *, tax_id: JSONValue = "PRIVATE_TAX_ID") -> JSONObject:
    values: list[JSONValue] = [
        "Sociedade Luar Fictícia",
        "Área fictícia",
        "PRIVATE_ADDRESS",
        "PRIVATE_AMOUNT",
        "PRIVATE_PERCENTAGE",
        "2",
        tax_id,
        owner,
    ]
    return node(
        f"{COMPANIES}_0",
        [node(f"{COMPANIES}-col{i}", value) for i, value in enumerate(values, 1)],
        isEmpty=False,
    )


def support(
    recipient: JSONValue = 0, *, index: int = 0, tax_id: JSONValue = "PRIVATE_TAX_ID"
) -> JSONObject:
    values: list[JSONValue] = [
        "Bolsa de investigação fictícia",
        "Fundação Aurora Fictícia",
        "Área da entidade fictícia",
        "Área do apoio fictícia",
        "2021-03-01T12:00:00Z",
        tax_id,
        "2022-03-01T12:00:00Z",
        recipient,
        "PRIVATE_PARTICIPATED_COMPANY" if recipient == 3 else None,
    ]
    return node(
        f"{SUPPORTS}_{index}",
        [node(f"{SUPPORTS}-col{i}", value) for i, value in enumerate(values, 1)],
        isEmpty=False,
    )


def service(
    *, index: int = 0, secrecy: JSONValue = None, columns: int = 8, tax_id: JSONValue = NIPC
) -> JSONObject:
    values: list[JSONValue] = [
        "Parecer técnico fictício",
        "Associação Luar Fictícia",
        "Área fictícia",
        "PRIVATE_ADDRESS",
        "2021-05-01T12:00:00Z",
        tax_id,
        "2021-06-01T12:00:00Z",
        secrecy,
    ][:columns]
    return node(
        f"{SERVICES}_{index}",
        [node(f"{SERVICES}-col{i}", value) for i, value in enumerate(values, 1)],
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
    supports: list[JSONValue] | None = None,
    services: list[JSONValue] | None = None,
    others: list[JSONValue] | None = None,
    state: int = 8,
    nature: int = 1,
    identifier: int = 201,
) -> JSONObject:
    tables: list[JSONValue] = [
        node(ACTIVITIES, [professional if professional is not None else activity()]),
        node("cd0e811a-d9bb-49ee-b0f0-1dae0680eb22", "PRIVATE_ASSOCIATIONS"),
        node(SUPPORTS, supports or []),
        node(SERVICES, services or []),
        node(COMPANIES, [business] if business is not None else []),
        node(OTHER_SITUATIONS, others or []),
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
        "natureType": nature,
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


def listing(
    *declarations: JSONObject, page_size: int = 10, moment: int | None = None
) -> list[JSONValue]:
    items: list[JSONValue] = [
        {
            "id": value["id"],
            "stateTypeId": value["stateType"],
            "natureTypeId": value["natureType"],
            "declarativeMomentTypeId": moment,
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


def snapshot(
    identity: SourceIdentity,
    *declarations: JSONObject,
    as_of: date = DAY,
    moment: int | None = None,
):
    return parse_snapshot(
        holder_id="101",
        identity=identity,
        pages=listing(*declarations, moment=moment),
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


def test_unavailable_section_and_explicit_opposition_apply_at_the_node(identity):
    declaration = detail()
    declaration["unavailableSections"] = [{"sectionKey": ACTIVITIES, "justification": "PRIVATE"}]
    assert snapshot(identity, declaration).observations == ()
    declaration = detail(business=company())
    declaration["oppositionKeys"] = ["unrelated-private-field-col4"]
    assert len(snapshot(identity, declaration).observations) == 2
    declaration["oppositionKeys"] = [f"{ACTIVITIES}-col1"]
    assert [row.kind for row in snapshot(identity, declaration).observations] == ["shareholding"]
    declaration["oppositionKeys"] = [{"unknown-shape": "PRIVATE_OBJECTION"}]
    with pytest.raises(InterestsImportError):
        snapshot(identity, declaration)


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
    identity.reviewed_by_id = None
    assert snapshot(identity, detail()).observations


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
def reviewed_identity(db, reviewer):
    person = Entity.objects.create(
        name="Pessoa Aurora Inteiramente Fictícia",
        slug="pessoa-aurora-ficticia",
        kind="person",
        is_public=True,
    )
    return SourceIdentity.objects.create(
        source="ept",
        external_id="101",
        entity=person,
        reviewed_by=reviewer,
        reviewed_at=timezone.now(),
        review_notes="Identidade fictícia revista com contexto oficial.",
    )


@pytest.mark.django_db
def test_unmapped_holder_dry_run_does_not_write_identity():
    with patch("ligacoes.core.interests._post", side_effect=fixture_post(detail())):
        result = fetch_snapshot(holder_id="101", holder_name="Pessoa Aurora Inteiramente Fictícia")
    assert result.observations
    assert result.identity._state.adding
    assert not SourceIdentity.objects.exists()


def test_unreviewed_holder_is_not_blocked(reviewed_identity):
    reviewed_identity.reviewed_at = None
    reviewed_identity.reviewed_by = None
    reviewed_identity.save()
    with patch("ligacoes.core.interests._post", side_effect=fixture_post(detail())):
        assert fetch_snapshot(
            holder_id="101", holder_name=reviewed_identity.entity.name
        ).observations


def fixture_post(declaration: JSONObject):
    def post(endpoint: str, body: JSONObject, *, holder_id: str, deadline: float) -> JSONValue:
        if endpoint == "/search":
            return listing(declaration)[0]
        if endpoint == "/getdeclaration":
            return {"code": 0, "data": declaration}
        raise AssertionError("Unexpected endpoint in fictional fixture")

    return post


def test_cli_dry_run_then_apply_publishes_with_provenance(reviewed_identity):
    with (
        patch(
            "ligacoes.core.management.commands.import_interests.fetch_holder_listing",
            return_value={"101": (reviewed_identity.entity.name, ())},
        ),
        patch("ligacoes.core.interests._post", side_effect=fixture_post(detail())),
    ):
        call_command("import_interests", holder_id="101", stdout=StringIO())
        assert not SourceObservation.objects.exists()
        call_command("import_interests", holder_id="101", apply=True, stdout=StringIO())
    observation = SourceObservation.objects.get(is_current=True)
    assert observation.identity_id == reviewed_identity.pk
    assert observation.object is not None
    assert observation.relationship is not None
    assert observation.relationship.status == Relationship.Status.PUBLISHED
    assert observation.evidence is not None
    assert observation.evidence.source.dataset == "ept_declaracoes"


def test_moving_complete_scope_is_rejected_before_apply(reviewed_identity):
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
        fetch_snapshot(holder_id="101", holder_name=reviewed_identity.entity.name)
    assert not SourceObservation.objects.exists()


def test_editorial_withdrawal_survives_source_change_and_return(reviewed_identity, reviewer):
    apply_snapshot(snapshot(reviewed_identity, detail()))
    observation = SourceObservation.objects.get(is_current=True)
    relationship = observation.relationship
    assert relationship is not None
    withdraw_relationship(relationship, reviewer)
    changed = detail(professional=activity(role="Função fictícia corrigida"))
    apply_snapshot(snapshot(reviewed_identity, changed))
    relationship.refresh_from_db()
    assert relationship.status == Relationship.Status.REJECTED
    apply_snapshot(snapshot(reviewed_identity))
    assert not SourceObservation.objects.filter(is_current=True).exists()
    apply_snapshot(snapshot(reviewed_identity, detail()))
    relationship.refresh_from_db()
    assert relationship.status == Relationship.Status.REJECTED


def test_repeat_and_excluded_field_changes_do_not_create_new_revisions(reviewed_identity):
    identity = reviewed_identity
    first = snapshot(identity, detail())
    apply_snapshot(first)
    retained = SourceObservation.objects.get(is_current=True)
    row = activity()
    child(row, f"{ACTIVITIES}-col9")["value"] = "PRIVATE_CORRECTED_TAX_ID"
    second = snapshot(identity, detail(professional=row))
    apply_snapshot(second)
    assert second.observations[0].revision == first.observations[0].revision
    assert list(SourceObservation.objects.values_list("pk", flat=True)) == [retained.pk]


def test_supports_and_services_project_only_the_declarants_public_rows(identity):
    declaration = detail(
        professional=activity(empty=True),
        supports=[support(recipient, index=recipient) for recipient in (0, 1, 2, 3)],
        services=[
            service(index=0),
            service(index=1, secrecy="true"),
            service(index=2, columns=7, tax_id=None),
        ],
    )
    observations = snapshot(identity, declaration).observations
    assert sorted(row.reference.rsplit("; ", 1)[1] for row in observations) == [
        f"{SUPPORTS}_0",
        f"{SERVICES}_0",
        f"{SERVICES}_2",
    ]
    benefit = next(row for row in observations if SUPPORTS in row.reference)
    assert benefit.kind == "professional_activity"
    assert "Bolsa de investigação fictícia" in benefit.passage
    assert "Fundação Aurora Fictícia" in benefit.passage
    assert benefit.effective_start == date(2021, 3, 1)
    assert benefit.effective_end == date(2022, 3, 1)
    assert all(row.category == "declared_interest" for row in observations)
    assert "PRIVATE_" not in repr(observations)


@pytest.mark.parametrize("recipient", [4, "Cônjuge", True])
def test_unknown_support_recipient_fails_closed(identity, recipient):
    with pytest.raises(InterestsImportError):
        snapshot(identity, detail(supports=[support(recipient)]))


@pytest.mark.parametrize(
    "value",
    [PERSON_NIF, int(PERSON_NIF), "PT123456789", "501000110", "ES-X1234567", "Isento"],
)
def test_nif_nipc_column_keeps_nothing_but_a_legal_person_nipc(identity, value):
    observation = snapshot(identity, detail(professional=activity(tax_id=value))).observations[0]
    assert observation.object_identifier == ""
    assert observation.object_name == "Empresa Aurora Fictícia"
    assert str(value) not in repr(observation)


def test_declared_nipc_is_normalised_and_kept_as_organisation_identifier(identity):
    result = snapshot(identity, detail(professional=activity(tax_id="501 000 119")))
    observation = result.observations[0]
    assert observation.object_identifier == f"nipc:{NIPC}"
    assert observation.object_name == "Empresa Aurora Fictícia"


@pytest.mark.parametrize("reason", [0, 1, 3])
def test_restricted_nipc_cell_is_not_used(identity, reason):
    row = activity(tax_id=NIPC)
    cell = child(row, f"{ACTIVITIES}-col9")
    cell["reason"] = reason
    observation = snapshot(identity, detail(professional=row)).observations[0]
    assert observation.object_identifier == ""
    assert NIPC not in repr(observation)


@pytest.mark.parametrize(
    ("nature", "moment", "post_office"),
    [(1, 1, False), (2, 0, False), (4, 4, True), (8, 8, True), (16, 4, True), (16, 1, False)],
)
def test_cessation_and_final_declarations_are_marked_post_office(
    identity, nature, moment, post_office
):
    observation = snapshot(identity, detail(nature=nature), moment=moment).observations[0]
    assert ("pós-cargo" in observation.passage) is post_office
    assert observation.reference.endswith("; pós-cargo") is post_office


def test_valid_nipc_anchors_and_publishes_the_declared_connection(reviewed_identity):
    result = snapshot(
        reviewed_identity,
        detail(professional=activity(tax_id=NIPC), business=company(tax_id=OTHER_NIPC)),
    )
    apply_snapshot(result)
    professional, business = (
        SourceObservation.objects.get(is_current=True, kind=kind)
        for kind in ("professional_activity", "shareholding")
    )
    assert professional.object_identifier == f"nipc:{NIPC}"
    assert professional.object is not None
    assert professional.object.kind == "company"
    assert business.object is not None
    assert business.object.kind == "company"
    assert SourceIdentity.objects.get(source="nipc", external_id=NIPC).entity == professional.object
    assert professional.relationship is not None and business.relationship is not None
    assert professional.relationship.status == Relationship.Status.PUBLISHED
    assert business.relationship.status == Relationship.Status.PUBLISHED
    apply_snapshot(result)
    assert SourceObservation.objects.count() == 2
    assert Entity.objects.filter(kind__in=["organisation", "company"]).count() == 2


def test_natural_person_nif_in_tax_column_is_never_stored(reviewed_identity):
    declaration = detail(
        professional=activity(tax_id=PERSON_NIF),
        supports=[support(0, tax_id=PERSON_NIF)],
        services=[service(tax_id=PERSON_NIF)],
    )
    apply_snapshot(snapshot(reviewed_identity, declaration))
    assert SourceObservation.objects.filter(is_current=True).count() == 3
    assert not SourceIdentity.objects.filter(source="nipc").exists()
    for model in (SourceObservation, SourceIdentity, Entity):
        for row in model.objects.values():
            assert not any(isinstance(value, str) and PERSON_NIF in value for value in row.values())


def other_situation(text: str, *, secrecy: JSONValue = "false", index: int = 0) -> JSONObject:
    values: list[JSONValue] = [6, None, "PRIVATE_REMUNERATION", text, "Outra situação", secrecy]
    return node(
        f"{OTHER_SITUATIONS}_{index}",
        [node(f"{OTHER_SITUATIONS}-col{i}", value) for i, value in enumerate(values, 1)],
        isEmpty=False,
    )


@pytest.mark.parametrize(
    "role", ["Sócio-Gerente", "Administrador", "Director", "Presidente do Conselho"]
)
def test_company_role_is_preserved_without_asserting_ownership(identity, role):
    observation = snapshot(
        identity, detail(professional=activity(role=role, tax_id=NIPC))
    ).observations[0]
    assert observation.kind == "directorship"
    assert observation.role == role
    assert observation.object_identifier == f"nipc:{NIPC}"
    assert observation.dataset == "ept_declaracoes"


@pytest.mark.parametrize("table", [ACTIVITIES, SERVICES, OTHER_SITUATIONS])
@pytest.mark.parametrize("restriction", ["secrecy", "deleted"])
def test_secret_or_deleted_rows_are_discarded_before_reading_values(identity, table, restriction):
    row = (
        activity()
        if table == ACTIVITIES
        else service()
        if table == SERVICES
        else other_situation(f"Empresa Aurora, Lda, NIF {NIPC}, serviço fictício.")
    )
    if restriction == "deleted":
        row["isDeleted"] = True
    else:
        column = 6 if table == OTHER_SITUATIONS else 8
        child(row, f"{table}-col{column}")["value"] = "true"
    declaration = detail(
        professional=row if table == ACTIVITIES else activity(empty=True),
        services=[row] if table == SERVICES else [],
        others=[row] if table == OTHER_SITUATIONS else [],
    )
    assert snapshot(identity, declaration).observations == ()


@pytest.mark.parametrize("label", ["NIF", "NIPC", "com o NIF", "como o NIF", "NIF:"])
def test_other_situation_single_legal_client_has_minimal_quote(identity, label):
    text = (
        f"Empresa Aurora, Sociedade Fictícia, S.A, {label} {NIPC}, serviço de consultoria fictícia."
    )
    observation = snapshot(
        identity, detail(professional=activity(empty=True), others=[other_situation(text)])
    ).observations[0]
    assert observation.kind == "declared_client"
    assert observation.object_name == "Empresa Aurora, Sociedade Fictícia, S.A"
    assert observation.object_identifier == f"nipc:{NIPC}"
    assert observation.role == "serviço de consultoria fictícia."
    assert observation.effective_start is None and observation.effective_end is None
    assert observation.declared_on == date(2025, 6, 20)
    assert "PRIVATE_" not in repr(observation)
    assert f"{OTHER_SITUATIONS}_0" in observation.reference


def test_multiple_explicit_client_pairs_are_kept_but_ambiguous_groups_are_not(identity):
    paired = (
        f"Aurora Fictícia, Lda, NIF {NIPC}, serviço fictício; "
        f"Luar Fictícia, S.A, NIPC {OTHER_NIPC}, serviço distinto."
    )
    result = snapshot(
        identity, detail(professional=activity(empty=True), others=[other_situation(paired)])
    )
    assert {row.object_identifier for row in result.observations} == {
        f"nipc:{NIPC}",
        f"nipc:{OTHER_NIPC}",
    }
    assert len({row.external_id for row in result.observations}) == 2
    ambiguous = f"Grupo Aurora e Luar, NIF {NIPC} e NIF {OTHER_NIPC}, serviço fictício."
    assert (
        snapshot(
            identity, detail(professional=activity(empty=True), others=[other_situation(ambiguous)])
        ).observations
        == ()
    )


@pytest.mark.parametrize(
    "text",
    [
        f"Pessoa Fictícia, NIF {PERSON_NIF}, serviço fictício.",
        f"Empresa Aurora, Lda, NIF {NIPC}, serviço a pessoa com NIF {PERSON_NIF}.",
        f"Empresa Aurora, Lda, NIF {NIPC}, serviço a pessoa {PERSON_NIF}.",
    ],
)
def test_natural_person_nif_discards_entire_free_text_before_retention(identity, text):
    assert (
        snapshot(
            identity, detail(professional=activity(empty=True), others=[other_situation(text)])
        ).observations
        == ()
    )


def test_fetch_rerun_uses_retained_projection_and_never_downloads_unchanged_details(
    reviewed_identity,
):
    first_post = fixture_post(detail())
    with patch("ligacoes.core.interests._post", side_effect=first_post):
        first = fetch_snapshot(holder_id="101", holder_name=reviewed_identity.entity.name)
    apply_snapshot(first)
    observation = SourceObservation.objects.get(is_current=True)

    def cached_post(endpoint, body, **kwargs):
        assert endpoint != "/getdeclaration"
        return first_post(endpoint, body, **kwargs)

    with patch("ligacoes.core.interests._post", side_effect=cached_post):
        second = fetch_snapshot(holder_id="101", holder_name=reviewed_identity.entity.name)
    result = apply_snapshot(second)
    assert result["created"] == 0 and result["changed"] == 0
    assert SourceObservation.objects.get(is_current=True).pk == observation.pk
    assert Relationship.objects.count() == 1


@pytest.mark.django_db
def test_all_limit_is_numeric_order_and_failure_does_not_stop_later_holders():
    out, err = StringIO(), StringIO()
    seen = []

    def fetching(*, holder_id, **kwargs):
        seen.append(holder_id)
        if holder_id == "2":
            raise InterestsImportError("Falha pública fictícia")
        return type(
            "Snapshot", (), {"declaration_count": 0, "observations": (), "restricted_sections": 0}
        )()

    with (
        patch(
            "ligacoes.core.management.commands.import_interests.fetch_holder_listing",
            return_value=dict.fromkeys(("20", "3", "2"), ("Pessoa Fictícia", ())),
        ),
        patch(
            "ligacoes.core.management.commands.import_interests.fetch_snapshot",
            side_effect=fetching,
        ),
        pytest.raises(CommandError),
    ):
        call_command("import_interests", all=True, limit=2, stdout=out, stderr=err)
    assert seen == ["2", "3"]
    assert "concluídos=1; falhas=1" in out.getvalue()


def test_comma_separated_complete_legal_name_client_pairs_are_unambiguous(identity):
    text = (
        f"Aurora Fictícia, Lda, NIF {NIPC}, Luar Fictícia, S.A, NIF {OTHER_NIPC}, serviço fictício."
    )
    result = snapshot(
        identity, detail(professional=activity(empty=True), others=[other_situation(text)])
    )
    assert [(row.object_name, row.object_identifier) for row in result.observations] == [
        ("Aurora Fictícia, Lda", f"nipc:{NIPC}"),
        ("Luar Fictícia, S.A", f"nipc:{OTHER_NIPC}"),
    ]


def test_empty_declaration_cache_and_withdrawn_declaration_return(reviewed_identity):
    empty = detail(professional=activity(empty=True))
    base = fixture_post(empty)
    with patch("ligacoes.core.interests._post", side_effect=base):
        first = fetch_snapshot(holder_id="101", holder_name=reviewed_identity.entity.name)
    apply_snapshot(first)

    def no_details(endpoint, body, **kwargs):
        assert endpoint != "/getdeclaration"
        return base(endpoint, body, **kwargs)

    with patch("ligacoes.core.interests._post", side_effect=no_details):
        cached = fetch_snapshot(holder_id="101", holder_name=reviewed_identity.entity.name)
    assert not cached.observations
    apply_snapshot(snapshot(reviewed_identity, as_of=first.as_of))
    with patch("ligacoes.core.interests._post", side_effect=base) as post:
        fetch_snapshot(holder_id="101", holder_name=reviewed_identity.entity.name)
    assert any(call.args[0] == "/getdeclaration" for call in post.call_args_list)


def test_older_other_situation_table_without_secrecy_column_is_supported(identity):
    row = other_situation(f"Aurora Fictícia, Lda, NIF {NIPC}, serviço fictício.")
    cells = row["value"]
    assert isinstance(cells, list)
    cells.pop()
    result = snapshot(identity, detail(professional=activity(empty=True), others=[row]))
    assert result.observations[0].kind == "declared_client"


def test_winter_utc_literal_does_not_shift_the_declared_activity_date(identity):
    row = activity()
    child(row, f"{ACTIVITIES}-col7")["value"] = "2024-01-15T23:00:00Z"
    observation = snapshot(identity, detail(professional=row)).observations[0]
    assert observation.effective_end == date(2024, 1, 15)
    assert "2024-01-15T23:00:00Z" in observation.passage
    assert observation.temporal_status == "ended"


@pytest.mark.parametrize(
    "number",
    [
        "123 456 789",
        "123.456.789",
        "123-456-789",
        "123 456-789",
        "123\u00a0456\u00a0789",
    ],
)
@pytest.mark.parametrize("label", ["NIF ", ""])
def test_formatted_personal_nif_anywhere_discards_client_passage(identity, number, label):
    text = f"Empresa Fictícia, Lda, NIPC {NIPC}, serviços ao contribuinte {label}{number}."
    assert not snapshot(
        identity, detail(professional=activity(empty=True), others=[other_situation(text)])
    ).observations


@pytest.mark.parametrize("number", ["501 000 119", "501.000.119", "501-000-119"])
def test_formatted_legal_nipc_is_normalised_before_client_retention(identity, number):
    text = f"Empresa Fictícia, Lda, NIPC {number}, serviço fictício."
    observations = snapshot(
        identity, detail(professional=activity(empty=True), others=[other_situation(text)])
    ).observations
    assert observations[0].object_identifier == f"nipc:{NIPC}"
    assert number not in observations[0].passage


@pytest.mark.parametrize(
    "family",
    [
        "cônjuge",
        "unido de facto",
        "unida de facto",
        "companheiro",
        "companheira",
        "marido",
        "mulher",
    ],
)
def test_client_in_spouse_or_partner_context_is_excluded(identity, family):
    text = f"Empresa Fictícia, Lda, NIPC {NIPC}, serviço prestado pelo {family}."
    assert not snapshot(
        identity, detail(professional=activity(empty=True), others=[other_situation(text)])
    ).observations


@pytest.mark.parametrize(
    ("entity_id", "institution", "public_role"),
    [
        (4284, "Instituição Fictícia Excluída", "Presidente Fictício"),
        (301, "Partido Fictício da Aurora", "Cargo Fictício"),
        (301, "Instituição Fictícia de Eleição", "Candidato Fictício"),
        (301, "Instituição Fictícia de Eleição", "Candidata Fictícia"),
    ],
)
def test_party_and_candidacy_context_is_absent_from_declared_interest(
    identity, entity_id, institution, public_role
):
    declaration = detail(professional=activity(tax_id=NIPC))
    declaration["entityId"] = entity_id
    declaration["entity"] = institution
    declaration["role"] = public_role
    observation = snapshot(identity, declaration).observations[0]
    assert institution not in repr(observation)
    assert public_role not in repr(observation)
    assert "cargo público" not in observation.passage
    assert "entidade " not in observation.reference
    assert "cargo " not in observation.reference
    assert observation.reference == f"Decl. 201; titular 101; {ACTIVITIES}_0"
    assert observation.object_identifier == f"nipc:{NIPC}"


@pytest.mark.django_db
def test_cohort_does_not_pass_a_cross_holder_declaration_cache():
    from types import SimpleNamespace

    with (
        patch(
            "ligacoes.core.management.commands.import_interests.fetch_holder_listing",
            return_value=dict.fromkeys(("1", "2"), ("Pessoa Fictícia", ())),
        ),
        patch(
            "ligacoes.core.management.commands.import_interests.fetch_snapshot",
            return_value=SimpleNamespace(
                declaration_count=0, observations=(), restricted_sections=0
            ),
        ) as fetch,
    ):
        call_command("import_interests", all=True, stdout=StringIO())
    assert len(fetch.call_args_list) == 2
    assert all("declaration_cache" not in call.kwargs for call in fetch.call_args_list)


def test_substantive_role_and_kind_changes_update_claim_with_identical_passage(reviewed_identity):
    from dataclasses import replace

    from ligacoes.core.interests import revised

    first = snapshot(reviewed_identity, detail(professional=activity(tax_id=NIPC)))
    original = first.observations[0]
    assert original.revision == revised(original).revision
    apply_snapshot(first)
    changed = revised(replace(original, kind="directorship", role="Administrador fictício"))
    assert changed.passage == original.passage
    assert changed.revision != original.revision
    result = apply_snapshot(replace(first, observations=(changed,)))
    assert result["changed"] == 1
    current = SourceObservation.objects.get(is_current=True)
    assert current.kind == "directorship"
    assert current.role == "Administrador fictício"
    relationship = current.relationship
    assert relationship is not None
    assert relationship.kind == "directorship"
    assert relationship.role == "Administrador fictício"
    assert relationship.status == Relationship.Status.PUBLISHED
