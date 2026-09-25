import json
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from django.test import Client
from django.utils import timezone

from ligacoes.core.import_jobs import enqueue_import
from ligacoes.core.models import ImportRun

pytestmark = pytest.mark.django_db

# Deliberately fictional, deterministic protocol fixtures; never deployment credentials.
TOKEN = "fictional-import-api-token-for-tests-only"  # noqa: S105
RUN_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"
URL = "/ops/imports/"
DETAIL_URL = f"{URL}{RUN_ID}/"
PAYLOAD = {"request_id": RUN_ID, "as_of": "2025-09-01"}


@pytest.fixture(autouse=True)
def import_token(settings):
    settings.IMPORT_API_TOKEN = TOKEN


@pytest.fixture
def api_client():
    return Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")


@pytest.mark.parametrize("token", ["", "short", "x" * 31])
def test_disabled_api_cannot_enqueue_or_reveal_existing_jobs(api_client, settings, token):
    assert api_client.post(URL, PAYLOAD, content_type="application/json").status_code == 202
    settings.IMPORT_API_TOKEN = token
    assert api_client.post(URL, PAYLOAD, content_type="application/json").status_code == 404
    assert api_client.get(DETAIL_URL).status_code == 404
    assert ImportRun.objects.get(pk=RUN_ID).status == "queued"
    assert ImportRun.objects.count() == 1


@pytest.mark.parametrize(
    "authorization",
    ["", f"Basic {TOKEN}", "Bearer incorrect-token", f"Bearer  {TOKEN}", f"Bearer {TOKEN} "],
)
def test_only_exact_bearer_auth_can_create_or_inspect_jobs(api_client, authorization):
    assert api_client.post(URL, PAYLOAD, content_type="application/json").status_code == 202
    client = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=authorization)
    response = client.post(
        URL, {**PAYLOAD, "request_id": OTHER_ID}, content_type="application/json"
    )
    assert response.status_code == 403
    assert client.get(DETAIL_URL).status_code == 403
    assert "Location" not in response
    assert TOKEN not in response.content.decode()
    assert ImportRun.objects.count() == 1


def test_minimum_length_token_can_enqueue(settings):
    settings.IMPORT_API_TOKEN = "fictional-" + "x" * 22
    client = Client(
        enforce_csrf_checks=True,
        HTTP_AUTHORIZATION=f"Bearer {settings.IMPORT_API_TOKEN}",
    )
    response = client.post(URL, PAYLOAD, content_type="application/json")
    assert response.status_code == 202
    assert ImportRun.objects.get(pk=RUN_ID).status == "queued"


def test_superuser_session_is_not_import_api_authentication(api_client, django_user_model):
    assert api_client.post(URL, PAYLOAD, content_type="application/json").status_code == 202
    user = django_user_model.objects.create_user(
        username="fictional-import-api-administrator", is_staff=True, is_superuser=True
    )
    cookie_client = Client(enforce_csrf_checks=True)
    cookie_client.force_login(user)
    assert cookie_client.post(URL, PAYLOAD, content_type="application/json").status_code == 403
    assert cookie_client.get(DETAIL_URL).status_code == 403
    assert ImportRun.objects.count() == 1


def test_bearer_post_without_csrf_or_admin_enqueues_validation_only(api_client, settings):
    settings.ENABLE_ADMIN = False
    response = api_client.post(URL, PAYLOAD, content_type="application/json")
    assert response.status_code == 202
    run = ImportRun.objects.get(pk=RUN_ID)
    assert (run.mode, run.legislature, run.as_of, run.origin, run.requested_by_id) == (
        "dry_run",
        "XVII",
        date(2025, 9, 1),
        "github",
        None,
    )
    assert run.status == "queued"
    assert run.started_at is None
    assert response.json()["id"] == RUN_ID
    assert response.json()["status"] == "queued"
    assert response["Cache-Control"] == "no-store"
    assert "Location" not in response


@pytest.mark.parametrize("as_of", ["", None])
def test_omitted_or_empty_date_resolves_to_local_calendar_day(api_client, as_of, monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: datetime(2025, 9, 1, 23, 30, tzinfo=UTC))
    payload = {"request_id": RUN_ID}
    if as_of is not None:
        payload["as_of"] = as_of
    response = api_client.post(URL, payload, content_type="application/json")
    assert response.status_code == 202
    # Lisbon is UTC+1 on this date: the local day is already 2 September.
    assert response.json()["as_of"] == "2025-09-02"


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", None),
        ("request_id", 123),
        ("request_id", "not-a-uuid"),
        ("mode", None),
        ("mode", True),
        ("mode", "publish"),
        ("mode", []),
        ("legislature", 17),
        ("legislature", "XVII; command"),
        ("legislature", ""),
        ("as_of", None),
        ("as_of", 20250901),
        ("as_of", "2025-02-30"),
        ("as_of", "20250901"),
        ("as_of", "2025-W36-1"),
        ("confirm_apply", "true"),
        ("confirm_apply", 1),
        ("confirm_apply", None),
    ],
)
def test_invalid_parameter_types_and_values_never_enqueue(api_client, field, value):
    response = api_client.post(URL, {**PAYLOAD, field: value}, content_type="application/json")
    assert response.status_code == 400
    assert not ImportRun.objects.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://example.org/fictional-source"),
        ("source", "biography"),
        ("expected_count", 1),
        ("publish", True),
        ("origin", "admin"),
        ("requested_by", 1),
        ("status", "succeeded"),
    ],
)
def test_request_cannot_expand_source_or_publication_scope(api_client, field, value):
    response = api_client.post(URL, {**PAYLOAD, field: value}, content_type="application/json")
    assert response.status_code == 400
    assert not ImportRun.objects.exists()


@pytest.mark.parametrize(
    "body",
    [
        "",
        "{",
        "[]",
        "null",
        "true",
        "{}",
        '{"request_id": "' + RUN_ID + '", "mode": "dry_run", "mode": "apply"}',
        '{"request_id": "' + RUN_ID + '", "confirm_apply": NaN}',
        '{"request_id": "' + RUN_ID + '", "as_of": Infinity}',
        b"\xff",
        "[" * 1500 + "]" * 1500,
    ],
)
def test_malformed_ambiguous_or_non_object_json_never_enqueue(api_client, body):
    response = api_client.post(URL, body, content_type="application/json")
    assert response.status_code == 400
    assert not ImportRun.objects.exists()


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "application/json; charset=utf-16"],
)
def test_only_utf8_json_is_accepted(api_client, content_type):
    response = api_client.generic("POST", URL, json.dumps(PAYLOAD), content_type=content_type)
    assert response.status_code == 400
    assert not ImportRun.objects.exists()


def test_body_limit_rejects_large_requests_and_accepts_exact_boundary(api_client):
    body = json.dumps(PAYLOAD)
    padded = body + " " * (4096 - len(body.encode("utf-8")))
    response = api_client.post(URL, padded + " ", content_type="application/json")
    assert response.status_code == 400
    assert not ImportRun.objects.exists()
    assert api_client.post(URL, padded, content_type="application/json").status_code == 202
    assert ImportRun.objects.get(pk=RUN_ID).status == "queued"


@pytest.mark.parametrize("confirmation", [False, None])
def test_apply_requires_explicit_boolean_confirmation(api_client, confirmation):
    payload: dict[str, str | bool] = {**PAYLOAD, "mode": "apply"}
    if confirmation is not None:
        payload["confirm_apply"] = confirmation
    assert api_client.post(URL, payload, content_type="application/json").status_code == 400
    assert not ImportRun.objects.exists()
    payload["confirm_apply"] = True
    assert api_client.post(URL, payload, content_type="application/json").status_code == 202
    assert ImportRun.objects.get(pk=RUN_ID).mode == "apply"


def test_same_request_replays_job_without_reexecution_even_after_completion(api_client):
    first = api_client.post(URL, PAYLOAD, content_type="application/json")
    replay = api_client.post(URL, PAYLOAD, content_type="application/json")
    assert replay.status_code == 202
    assert replay.json() == first.json()
    finished = timezone.now()
    ImportRun.objects.filter(pk=RUN_ID).update(
        status="succeeded",
        started_at=finished,
        finished_at=finished,
        result={"serving": 230, "created_members": 0, "created_records": 0, "ceased_members": 0},
    )
    completed = api_client.post(URL, PAYLOAD, content_type="application/json")
    assert completed.status_code == 202
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["result"]["serving"] == 230
    assert ImportRun.objects.count() == 1


def test_changed_request_or_another_active_job_conflicts(api_client):
    assert api_client.post(URL, PAYLOAD, content_type="application/json").status_code == 202
    for changes in [
        {"mode": "apply", "confirm_apply": True},
        {"as_of": "2025-09-02"},
        {"legislature": "XVI"},
        {"request_id": OTHER_ID},
    ]:
        response = api_client.post(URL, {**PAYLOAD, **changes}, content_type="application/json")
        assert response.status_code == 409
    assert ImportRun.objects.count() == 1
    assert ImportRun.objects.get(pk=RUN_ID).mode == "dry_run"


def test_api_cannot_replay_an_admin_request(api_client, django_user_model):
    user = django_user_model.objects.create_user(
        username="fictional-admin-import-owner", is_staff=True, is_superuser=True
    )
    run = enqueue_import(
        request_id=UUID(RUN_ID),
        mode="dry_run",
        legislature="XVII",
        as_of=date(2025, 9, 1),
        requested_by=user,
        origin="admin",
    )
    response = api_client.post(URL, PAYLOAD, content_type="application/json")
    assert response.status_code == 409
    run.refresh_from_db()
    assert run.origin == "admin"
    assert run.requested_by_id == user.pk


def test_status_only_exposes_safe_metadata_counts_and_generic_failure(api_client):
    assert api_client.post(URL, PAYLOAD, content_type="application/json").status_code == 202
    finished = timezone.now()
    ImportRun.objects.filter(pk=RUN_ID).update(
        status="failed",
        started_at=finished,
        finished_at=finished,
        error="PRIVATE_EXCEPTION_FICTIONAL_TOKEN_AND_RECORD",
        result={
            "serving": 230,
            "created_members": True,
            "created_records": -1,
            "ceased_members": "PRIVATE_COUNT_VALUE",
            "source": "PRIVATE_FICTIONAL_SOURCE_MATERIAL",
            "person": {"name": "PRIVATE_FICTIONAL_PERSON"},
        },
    )
    response = api_client.get(DETAIL_URL)
    assert response.status_code == 200
    assert set(response.json()) == {
        "id",
        "mode",
        "legislature",
        "as_of",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "result",
        "error",
    }
    assert response.json()["status"] == "failed"
    assert response.json()["result"] == {"serving": 230}
    assert "PRIVATE_" not in response.content.decode()
    assert TOKEN not in response.content.decode()
    assert response["Cache-Control"] == "no-store"


@pytest.mark.parametrize("method", ["get", "head", "put", "patch", "delete", "options"])
def test_enqueue_route_rejects_other_methods(api_client, method):
    response = getattr(api_client, method)(URL)
    assert response.status_code == 405
    assert response["Allow"] == "POST"
    assert not ImportRun.objects.exists()


@pytest.mark.parametrize("method", ["post", "head", "put", "patch", "delete", "options"])
def test_status_route_rejects_other_methods(api_client, method):
    assert api_client.post(URL, PAYLOAD, content_type="application/json").status_code == 202
    response = getattr(api_client, method)(DETAIL_URL)
    assert response.status_code == 405
    assert response["Allow"] == "GET"
    assert ImportRun.objects.get(pk=RUN_ID).status == "queued"


def test_missing_jobs_and_malformed_paths_never_redirect(api_client):
    for url in [DETAIL_URL, f"{URL}not-a-uuid/", URL.rstrip("/"), DETAIL_URL.rstrip("/")]:
        response = api_client.get(url)
        assert response.status_code == 404
        assert "Location" not in response
    assert not ImportRun.objects.exists()
