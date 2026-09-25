from datetime import date
from uuid import UUID, uuid4

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import include, path, reverse
from django.utils import timezone
from django.utils.html import escape

from ligacoes.core.models import Entity, ImportRun, Relationship, Source

pytestmark = pytest.mark.django_db
urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("ligacoes.public.urls")),
]


@pytest.fixture(autouse=True)
def admin_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def staff(django_user_model):
    def create(username, *permissions, **flags):
        user = django_user_model.objects.create_user(
            username=username, is_staff=flags.pop("is_staff", True), **flags
        )
        for codename in permissions:
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label="core", codename=codename)
            )
        return user

    return create


@pytest.fixture
def operator(staff):
    return staff("fictional-import-operator", "run_import")


@pytest.fixture
def request_data():
    return {
        "request_id": str(uuid4()),
        "mode": "dry_run",
        "legislature": "XVII",
        "as_of": "2026-01-15",
    }


def request_url():
    return reverse("admin:core_importrun_request")


def detail_url(run):
    return reverse("admin:core_importrun_change", args=[run.pk])


@pytest.fixture
def failed_run(operator):
    return ImportRun.objects.create(
        id=uuid4(),
        mode="dry_run",
        status="failed",
        legislature="XVII",
        as_of=date(2026, 1, 15),
        origin="admin",
        requested_by=operator,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        error="Falha fictícia para teste de histórico.",
    )


def test_operator_discovers_menu_and_queues_without_fetching(client, operator, monkeypatch):
    def unexpected_fetch(*args, **kwargs):
        pytest.fail("The admin request must not fetch official sources")

    monkeypatch.setattr("ligacoes.core.parliament_fetch.fetch_url", unexpected_fetch)
    client.force_login(operator)
    history = reverse("admin:core_importrun_changelist")
    index = client.get(reverse("admin:index"))
    assert f'href="{history}"' in index.content.decode()
    listing = client.get(history)
    assert listing.status_code == 200
    assert f'href="{request_url()}"' in listing.content.decode()
    form = client.get(request_url()).context["form"]
    before = (Entity.objects.count(), Source.objects.count(), Relationship.objects.count())
    response = client.post(
        request_url(),
        {
            "request_id": str(form["request_id"].value()),
            "mode": form["mode"].value(),
            "legislature": form["legislature"].value(),
            "as_of": "",
        },
    )
    run = ImportRun.objects.get()
    assert response.status_code == 302
    assert response["Location"] == detail_url(run)
    assert (run.mode, run.status, run.origin, run.requested_by_id) == (
        "dry_run",
        "queued",
        "admin",
        operator.pk,
    )
    assert run.as_of == timezone.localdate()
    assert run.started_at is None
    assert (Entity.objects.count(), Source.objects.count(), Relationship.objects.count()) == before


def test_view_only_staff_can_read_but_cannot_queue(client, staff, failed_run, request_data):
    viewer = staff("fictional-import-viewer", "view_importrun")
    client.force_login(viewer)
    history = reverse("admin:core_importrun_changelist")
    assert f'href="{history}"' in client.get(reverse("admin:index")).content.decode()
    listing = client.get(history)
    assert listing.status_code == 200
    assert f'href="{request_url()}"' not in listing.content.decode()
    assert client.get(detail_url(failed_run)).status_code == 200
    assert client.get(request_url()).status_code == 403
    assert client.post(request_url(), request_data).status_code == 403
    assert ImportRun.objects.count() == 1


def test_staff_without_import_permissions_cannot_discover_or_access(
    client, staff, failed_run, request_data
):
    client.force_login(staff("fictional-unprivileged-staff"))
    history = reverse("admin:core_importrun_changelist")
    assert f'href="{history}"' not in client.get(reverse("admin:index")).content.decode()
    for url in (history, detail_url(failed_run), request_url()):
        assert client.get(url).status_code == 403
    assert client.post(request_url(), request_data).status_code == 403
    assert ImportRun.objects.count() == 1


@pytest.mark.parametrize("flags", [{"is_staff": False}, {"is_active": False}])
def test_run_permission_does_not_bypass_active_staff_boundary(client, staff, request_data, flags):
    user = staff("fictional-ineligible-operator", "run_import", **flags)
    client.force_login(user)
    for response in (client.get(request_url()), client.post(request_url(), request_data)):
        assert response.status_code == 302
        assert reverse("admin:login") in response["Location"]
    assert not ImportRun.objects.exists()


def test_anonymous_requests_cannot_queue(client, request_data):
    response = client.post(request_url(), request_data)
    assert response.status_code == 302
    assert reverse("admin:login") in response["Location"]
    assert not ImportRun.objects.exists()


def test_revoked_run_permission_prevents_form_submission(client, operator, request_data):
    client.force_login(operator)
    assert client.get(request_url()).status_code == 200
    operator.user_permissions.clear()
    assert client.post(request_url(), request_data).status_code == 403
    assert not ImportRun.objects.exists()


def test_csrf_and_untrusted_origin_block_queue_mutation(operator, request_data):
    client = Client(enforce_csrf_checks=True)
    client.force_login(operator)
    assert client.get(request_url()).status_code == 200
    assert client.post(request_url(), request_data).status_code == 403
    request_data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
    assert (
        client.post(request_url(), request_data, HTTP_ORIGIN="https://attacker.example").status_code
        == 403
    )
    assert not ImportRun.objects.exists()
    response = client.post(request_url(), request_data)
    assert response.status_code == 302
    assert ImportRun.objects.get().status == "queued"


def test_apply_requires_confirmation_and_preserves_request_id(client, operator, request_data):
    client.force_login(operator)
    request_data["mode"] = "apply"
    response = client.post(request_url(), request_data)
    assert response.status_code == 200
    form = response.context["form"]
    assert "confirm_apply" in form.errors
    assert str(form["request_id"].value()) == request_data["request_id"]
    assert form["confirm_apply"].field.widget.attrs["autofocus"] is True
    assert not ImportRun.objects.exists()
    request_data["confirm_apply"] = "on"
    response = client.post(request_url(), request_data)
    run = ImportRun.objects.get()
    assert response.status_code == 302
    assert run.id == UUID(request_data["request_id"])
    assert (run.mode, run.status) == ("apply", "queued")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "not-a-uuid"),
        ("request_id", ""),
        ("mode", "publish"),
        ("legislature", "XVII/../../"),
        ("as_of", "2026-02-30"),
    ],
)
def test_invalid_requests_do_not_queue(client, operator, request_data, field, value):
    client.force_login(operator)
    request_data[field] = value
    response = client.post(request_url(), request_data)
    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert not ImportRun.objects.exists()


def test_double_submission_reuses_run_and_changed_payload_conflicts(client, operator, request_data):
    client.force_login(operator)
    first = client.post(request_url(), request_data)
    second = client.post(request_url(), request_data)
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"]
    run = ImportRun.objects.get()
    request_data["legislature"] = "XVI"
    conflict = client.post(request_url(), request_data)
    assert conflict.status_code == 200
    assert conflict.context["form"].non_field_errors()
    assert str(conflict.context["form"]["request_id"].value()) == str(run.pk)
    run.refresh_from_db()
    assert (run.legislature, run.status) == ("XVII", "queued")
    assert ImportRun.objects.count() == 1


def test_other_operator_cannot_replay_someone_elses_request(client, operator, staff, request_data):
    client.force_login(operator)
    assert client.post(request_url(), request_data).status_code == 302
    client.force_login(staff("fictional-second-operator", "run_import"))
    response = client.post(request_url(), request_data)
    assert response.status_code == 200
    assert response.context["form"].non_field_errors()
    assert ImportRun.objects.get().requested_by_id == operator.pk


def test_busy_queue_keeps_existing_run_and_unsubmitted_request_id(client, operator, request_data):
    client.force_login(operator)
    assert client.post(request_url(), request_data).status_code == 302
    request_data["request_id"] = str(uuid4())
    response = client.post(request_url(), request_data)
    assert response.status_code == 200
    assert response.context["form"].non_field_errors()
    assert response.context["form"]["request_id"].value() == request_data["request_id"]
    assert ImportRun.objects.count() == 1
    assert not ImportRun.objects.filter(pk=request_data["request_id"]).exists()


def test_completed_request_replay_does_not_restart_execution(client, operator, failed_run):
    client.force_login(operator)
    response = client.post(
        request_url(),
        {
            "request_id": str(failed_run.pk),
            "mode": failed_run.mode,
            "legislature": failed_run.legislature,
            "as_of": failed_run.as_of.isoformat(),
        },
    )
    assert response.status_code == 302
    assert response["Location"] == detail_url(failed_run)
    failed_run.refresh_from_db()
    assert failed_run.status == "failed"
    assert failed_run.finished_at is not None
    assert ImportRun.objects.count() == 1


def test_even_superusers_cannot_add_change_delete_or_bulk_delete_history(client, staff, failed_run):
    client.force_login(staff("fictional-superuser", is_superuser=True))
    original = ImportRun.objects.values().get(pk=failed_run.pk)
    add_url = reverse("admin:core_importrun_add")
    delete_url = reverse("admin:core_importrun_delete", args=[failed_run.pk])
    for url in (add_url, delete_url):
        assert client.get(url).status_code == 403
        assert client.post(url, {"post": "yes"}).status_code == 403
    assert client.post(detail_url(failed_run), {"status": "queued"}).status_code == 403
    client.post(
        reverse("admin:core_importrun_changelist"),
        {"action": "delete_selected", "_selected_action": str(failed_run.pk), "post": "yes"},
    )
    assert ImportRun.objects.values().get(pk=failed_run.pk) == original
    assert ImportRun.objects.count() == 1


def test_history_escapes_errors_and_refresh_reads_latest_status(client, operator, failed_run):
    client.force_login(operator)
    payload = '<img src=x onerror="alert(1)"> — falha fictícia'
    ImportRun.objects.filter(pk=failed_run.pk).update(error=payload)
    response = client.get(detail_url(failed_run))
    body = response.content.decode()
    assert response.status_code == 200
    assert payload not in body
    assert str(escape(payload)) in body
    assert f'href="{detail_url(failed_run)}"' in body
    history = reverse("admin:core_importrun_changelist")
    assert f'href="{history}"' in client.get(history).content.decode()
    ImportRun.objects.filter(pk=failed_run.pk).update(
        status="succeeded",
        error="",
        result={"serving": 230, "created_members": 0, "created_records": 0, "ceased_members": 0},
    )
    refreshed = client.get(detail_url(failed_run))
    assert refreshed.context["original"].status == "succeeded"
    assert str(escape(payload)) not in refreshed.content.decode()
    assert "230" in refreshed.content.decode()
