from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from datetime import date
from threading import Barrier, Event, Thread
from uuid import UUID, uuid4

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from ligacoes.core import import_jobs
from ligacoes.core.import_jobs import ImportBusy, ImportConflict, enqueue_import, run_next_import
from ligacoes.core.models import (
    Entity,
    Evidence,
    ImportRun,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    Relationship,
    ReviewEvent,
    Source,
)
from ligacoes.core.parliament_fetch import CATALOGUES, ParliamentImportError
from ligacoes.core.parliament_import import apply_snapshot
from ligacoes.core.parliament_parse import MemberRecord, ParliamentSnapshot
from ligacoes.public.selectors import public_relationships

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2025, 7, 1)
_WAIT_SECONDS = 15
_PRIVATE_CANARY = "PRIVATE_FICTITIOUS_SOURCE_PAYLOAD"


@pytest.fixture
def operator():
    user = User.objects.create_user(username="fictional-import-operator", is_staff=True)
    user.user_permissions.add(
        Permission.objects.get(codename="run_import", content_type__app_label="core")
    )
    return user


@pytest.fixture
def complete_snapshot():
    # A complete, explicitly fictitious snapshot; no official records or network access.
    return ParliamentSnapshot(
        legislature="XVII",
        as_of=DAY,
        members=tuple(
            MemberRecord(
                cadastro_id=str(number),
                name=f"Pessoa Fictícia {number}",
                start_date=date(2025, 6, 3),
                end_date=None,
                data={
                    "legislature": "XVII",
                    "roster": {
                        "DepCadId": str(number),
                        "DepNomeCompleto": f"Pessoa Fictícia {number}",
                    },
                    "biography": {"CadProfissao": "Profissão fictícia"},
                },
            )
            for number in range(1001, 1231)
        ),
        roster_url=CATALOGUES["roster"],
        biography_url=CATALOGUES["biography"],
        expected_count=230,
    )


def _enqueue(
    *, mode: str = "dry_run", request_id: UUID | None = None, requested_by: User | None = None
) -> ImportRun:
    return enqueue_import(
        request_id=request_id or uuid4(),
        mode=mode,
        legislature="XVII",
        as_of=DAY,
        requested_by=requested_by,
        origin="admin" if requested_by is not None else "github",
        confirm_apply=mode == "apply",
    )


def _editorial_counts():
    return tuple(
        model.objects.count()
        for model in (
            Entity,
            Source,
            Relationship,
            Evidence,
            ParliamentImportState,
            ParliamentMember,
            ParliamentRecord,
            ReviewEvent,
        )
    )


@contextmanager
def _database_worker(operation: Callable[[], object]) -> Generator[Future[object]]:
    completed: Future[object] = Future()

    def run():
        database = connections["default"]
        try:
            with database.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false), "
                    "set_config('lock_timeout', %s, false)",
                    ["20000", "20000"],
                )
            completed.set_result(operation())
        except BaseException as error:
            completed.set_exception(error)
        finally:
            database.close()

    worker = Thread(target=run)
    worker.start()
    try:
        yield completed
    finally:
        worker.join(timeout=2 * _WAIT_SECONDS)
        assert not worker.is_alive(), "Import database worker did not terminate"
        completed.result(timeout=0)


@pytest.mark.parametrize("mode", ["dry_run", "apply"])
def test_actual_worker_command_completes_once_without_publication(
    mode, operator, complete_snapshot, monkeypatch
):
    actor = operator if mode == "apply" else None
    run = _enqueue(mode=mode, requested_by=actor)

    def fetch(**kwargs):
        assert not connection.in_atomic_block
        assert ImportRun.objects.get(pk=run.pk).status == "running"
        return complete_snapshot

    monkeypatch.setattr(import_jobs, "fetch_snapshot", fetch)
    call_command("run_import_worker", once=True)
    run.refresh_from_db()
    created = 230 if mode == "apply" else 0
    assert run.status == "succeeded"
    assert run.result == {
        "serving": 230,
        "created_members": created,
        "created_records": created,
        "ceased_members": 0,
    }
    assert run.started_at is not None and run.finished_at is not None
    assert run.created_at <= run.started_at <= run.finished_at
    assert run.error == ""
    assert ParliamentMember.objects.count() == created
    assert ParliamentRecord.objects.count() == created
    assert not Entity.objects.filter(is_public=True).exists()
    assert not Source.objects.filter(is_public=True).exists()
    assert not Evidence.objects.filter(is_public=True).exists()
    assert not Relationship.objects.exclude(
        status="draft", reviewed_at=None, reviewed_by=None
    ).exists()
    assert not public_relationships().exists()
    assert not ReviewEvent.objects.exists()
    if mode == "dry_run":
        assert _editorial_counts() == (0,) * 8

    def forbidden_fetch(**kwargs):
        pytest.fail("A replay or idle worker fetched another snapshot")

    monkeypatch.setattr(import_jobs, "fetch_snapshot", forbidden_fetch)
    replay = _enqueue(mode=mode, request_id=run.pk, requested_by=actor)
    assert replay.status == "succeeded"
    assert run_next_import() is None
    assert ImportRun.objects.count() == 1
    assert ParliamentRecord.objects.count() == created


def test_new_apply_request_reuses_existing_drafts(complete_snapshot, monkeypatch):
    apply_snapshot(complete_snapshot)
    before = _editorial_counts()
    monkeypatch.setattr(import_jobs, "fetch_snapshot", lambda **kwargs: complete_snapshot)
    run = _enqueue(mode="apply")
    run_next_import()
    run.refresh_from_db()
    assert run.status == "succeeded"
    assert run.result == {
        "serving": 230,
        "created_members": 0,
        "created_records": 0,
        "ceased_members": 0,
    }
    assert _editorial_counts() == before
    assert not public_relationships().exists()


def test_replay_preserves_resolved_date_and_normalises_equivalent_parameters(operator, monkeypatch):
    request_id = uuid4()
    monkeypatch.setattr(timezone, "localdate", lambda: DAY)
    run = enqueue_import(
        request_id=request_id,
        mode="dry_run",
        legislature=" xvii ",
        as_of=None,
        requested_by=operator,
    )
    monkeypatch.setattr(timezone, "localdate", lambda: date(2025, 7, 2))
    replay = enqueue_import(
        request_id=request_id,
        mode="dry_run",
        legislature="XVII",
        as_of=None,
        requested_by=operator,
        confirm_apply=True,
    )
    assert replay.pk == run.pk
    assert replay.as_of == DAY
    assert replay.legislature == "XVII"
    assert ImportRun.objects.count() == 1


@pytest.mark.parametrize("changed", ["mode", "legislature", "as_of", "origin", "actor"])
def test_request_uuid_cannot_change_payload_or_actor(operator, changed):
    run = _enqueue(requested_by=operator)
    actor = operator
    if changed == "actor":
        actor = User.objects.create_user(username="another-fictional-operator", is_staff=True)
        actor.user_permissions.set(operator.user_permissions.all())
    with pytest.raises(ImportConflict):
        enqueue_import(
            request_id=run.pk,
            mode="apply" if changed == "mode" else "dry_run",
            legislature="XVI" if changed == "legislature" else "XVII",
            as_of=date(2025, 7, 2) if changed == "as_of" else DAY,
            requested_by=None if changed == "origin" else actor,
            origin="github" if changed == "origin" else "admin",
            confirm_apply=changed == "mode",
        )
    assert ImportRun.objects.count() == 1
    run.refresh_from_db()
    assert run.status == "queued" and run.mode == "dry_run"


@pytest.mark.parametrize("revocation", ["permission", "staff", "active"])
def test_enqueue_rechecks_stale_cached_admin_permissions(operator, revocation):
    assert operator.has_perm("core.run_import")
    if revocation == "permission":
        operator.user_permissions.clear()
    else:
        User.objects.filter(pk=operator.pk).update(**{f"is_{revocation}": False})
    with pytest.raises(PermissionDenied):
        _enqueue(requested_by=operator)
    assert not ImportRun.objects.exists()


def test_apply_requires_explicit_confirmation_before_any_queue_write(operator):
    with pytest.raises(ValidationError):
        enqueue_import(
            request_id=uuid4(), mode="apply", legislature="XVII", as_of=DAY, requested_by=operator
        )
    assert not ImportRun.objects.exists()
    assert _editorial_counts() == (0,) * 8


@pytest.mark.parametrize("when", ["before_fetch", "during_fetch"])
@pytest.mark.parametrize("revocation", ["permission", "staff", "active"])
def test_admin_revocation_prevents_execution_or_apply(
    operator, complete_snapshot, monkeypatch, when, revocation
):
    run = _enqueue(mode="apply", requested_by=operator)

    def revoke():
        if revocation == "permission":
            operator.user_permissions.clear()
        else:
            User.objects.filter(pk=operator.pk).update(**{f"is_{revocation}": False})

    def fetch(**kwargs):
        if when == "before_fetch":
            pytest.fail("An unauthorised run reached the network")
        revoke()
        return complete_snapshot

    monkeypatch.setattr(import_jobs, "fetch_snapshot", fetch)
    if when == "before_fetch":
        revoke()
    run_next_import()
    run.refresh_from_db()
    assert run.status == "failed"
    assert run.result == {} and run.error
    assert run.started_at is not None and run.finished_at is not None
    assert _editorial_counts() == (0,) * 8


@pytest.mark.parametrize("failure", ["fetch", "completion"])
def test_failures_redact_payloads_and_roll_back_editorial_writes(
    complete_snapshot, monkeypatch, caplog, capsys, failure
):
    run = _enqueue(mode="apply")

    def fetch(**kwargs):
        if failure == "fetch":
            raise ParliamentImportError(_PRIVATE_CANARY)
        return complete_snapshot

    original_save = ImportRun.save

    def save(instance, *args, **kwargs):
        if failure == "completion" and instance.status == "succeeded":
            assert ParliamentRecord.objects.count() == 230
            raise RuntimeError(_PRIVATE_CANARY)
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(import_jobs, "fetch_snapshot", fetch)
    monkeypatch.setattr(ImportRun, "save", save)
    call_command("run_import_worker", once=True)
    run.refresh_from_db()
    assert run.status == "failed"
    assert run.result == {}
    assert 0 < len(run.error) <= 240
    assert _editorial_counts() == (0,) * 8
    captured = capsys.readouterr()
    assert _PRIVATE_CANARY not in run.error + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("race", ["duplicate", "different", "conflicting"])
def test_simultaneous_submissions_have_one_durable_winner(race):
    request_id = uuid4()
    barrier = Barrier(2, timeout=_WAIT_SECONDS)

    def submit(second=False):
        barrier.wait()
        try:
            return _enqueue(
                request_id=uuid4() if second and race == "different" else request_id,
                mode="apply" if second and race == "conflicting" else "dry_run",
            ).pk
        except ImportBusy:
            return "busy"
        except ImportConflict:
            return "conflict"

    with ExitStack() as workers:
        first = workers.enter_context(_database_worker(submit))
        second = workers.enter_context(_database_worker(lambda: submit(True)))
        results = [first.result(timeout=_WAIT_SECONDS), second.result(timeout=_WAIT_SECONDS)]
    winner = ImportRun.objects.get()
    assert winner.status == "queued"
    if race == "duplicate":
        assert results == [winner.pk, winner.pk]
    else:
        assert results.count(winner.pk) == 1
        assert results.count("busy" if race == "different" else "conflict") == 1


def test_database_rejects_a_second_active_job_without_service_checks():
    run = _enqueue()
    with pytest.raises(IntegrityError), transaction.atomic():
        ImportRun.objects.create(
            id=uuid4(), mode="apply", legislature="XVII", as_of=DAY, origin="github"
        )
    assert list(ImportRun.objects.values_list("pk", flat=True)) == [run.pk]


def test_overlapping_worker_cannot_recover_live_owner_during_networking(
    complete_snapshot, monkeypatch
):
    run = _enqueue(mode="apply")
    reached, release = Event(), Event()
    backend: Future[int] = Future()

    def fetch(**kwargs):
        assert not connection.in_atomic_block
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            row = cursor.fetchone()
            assert row is not None
            backend.set_result(row[0])
        reached.set()
        assert release.wait(timeout=_WAIT_SECONDS)
        return complete_snapshot

    monkeypatch.setattr(import_jobs, "fetch_snapshot", fetch)
    with _database_worker(run_next_import):
        try:
            assert reached.wait(timeout=_WAIT_SECONDS)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT xact_start FROM pg_stat_activity WHERE pid = %s", [backend.result()]
                )
                assert cursor.fetchone() == (None,)
            assert run_next_import() is None
            with pytest.raises(ImportBusy):
                _enqueue()
            run.refresh_from_db()
            assert run.status == "running" and run.finished_at is None
            assert _editorial_counts() == (0,) * 8
        finally:
            release.set()
    run.refresh_from_db()
    assert run.status == "succeeded"
    assert ParliamentRecord.objects.count() == 230
    assert not public_relationships().exists()


class _WorkerCrash(BaseException):
    pass


@pytest.mark.parametrize("during", ["fetch", "apply"])
def test_interrupted_run_is_recovered_as_failed_without_automatic_reapply(
    complete_snapshot, monkeypatch, during
):
    run = _enqueue(mode="apply")

    def fetch(**kwargs):
        if during == "fetch":
            raise _WorkerCrash
        return complete_snapshot

    def apply_then_crash(snapshot):
        apply_snapshot(snapshot)
        assert ParliamentRecord.objects.count() == 230
        raise _WorkerCrash

    monkeypatch.setattr(import_jobs, "fetch_snapshot", fetch)
    monkeypatch.setattr(import_jobs, "apply_snapshot", apply_then_crash)
    with pytest.raises(_WorkerCrash):
        run_next_import()
    run.refresh_from_db()
    assert run.status == "running"
    assert run.finished_at is None
    assert _editorial_counts() == (0,) * 8
    assert run_next_import() is None
    run.refresh_from_db()
    assert run.status == "failed" and run.finished_at is not None
    assert run.result == {}
    assert _enqueue(mode="apply", request_id=run.pk).status == "failed"
    assert _editorial_counts() == (0,) * 8


@pytest.mark.parametrize("interruption", ["crash", "exception"])
def test_interruption_after_commit_keeps_success_and_never_reapplies(
    complete_snapshot, monkeypatch, interruption
):
    run = _enqueue(mode="apply")
    monkeypatch.setattr(import_jobs, "fetch_snapshot", lambda **kwargs: complete_snapshot)
    original_commit = connection.commit
    interrupted = False

    def commit_then_crash():
        nonlocal interrupted
        completed = ImportRun.objects.filter(pk=run.pk, status="succeeded").exists()
        original_commit()
        if completed and not interrupted:
            interrupted = True
            if interruption == "crash":
                raise _WorkerCrash
            raise RuntimeError(_PRIVATE_CANARY)

    with monkeypatch.context() as patch:
        patch.setattr(connection, "commit", commit_then_crash)
        if interruption == "crash":
            with pytest.raises(_WorkerCrash):
                run_next_import()
        else:
            completed = run_next_import()
            assert completed is not None and completed.status == "succeeded"
    run.refresh_from_db()
    assert run.status == "succeeded"
    assert run.result["created_records"] == 230
    before = _editorial_counts()
    assert run_next_import() is None
    assert _enqueue(mode="apply", request_id=run.pk).status == "succeeded"
    assert _editorial_counts() == before
    assert ParliamentRecord.objects.count() == 230


def test_lost_session_cannot_finish_after_new_owner_recovers_and_runs_next_job(
    complete_snapshot, monkeypatch
):
    old = _enqueue(mode="apply")
    lost, release = Event(), Event()

    def fetch(**kwargs):
        if not lost.is_set():
            connection.close()
            lost.set()
            assert release.wait(timeout=_WAIT_SECONDS)
        return complete_snapshot

    def stale_worker():
        with pytest.raises(RuntimeError):
            run_next_import()

    monkeypatch.setattr(import_jobs, "fetch_snapshot", fetch)
    with _database_worker(stale_worker):
        try:
            assert lost.wait(timeout=_WAIT_SECONDS)
            assert run_next_import() is None
            old.refresh_from_db()
            assert old.status == "failed"
            new = _enqueue()
            run_next_import()
            new.refresh_from_db()
            assert new.status == "succeeded"
        finally:
            release.set()
    old.refresh_from_db()
    assert old.status == "failed" and old.result == {}
    assert _editorial_counts() == (0,) * 8
    assert ImportRun.objects.filter(status="succeeded").count() == 1


def test_worker_refuses_pending_migrations_before_claiming_job(monkeypatch):
    run = _enqueue()
    recorder = MigrationRecorder(connection)
    recorder.record_unapplied("core", "0003_import_run")

    def forbidden_fetch(**kwargs):
        pytest.fail("A schema-incompatible worker fetched source data")

    monkeypatch.setattr(import_jobs, "fetch_snapshot", forbidden_fetch)
    try:
        with pytest.raises(CommandError):
            call_command("run_import_worker", once=True)
    finally:
        recorder.record_applied("core", "0003_import_run")
    run.refresh_from_db()
    assert run.status == "queued" and run.started_at is None
    assert _editorial_counts() == (0,) * 8


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_worker_rejects_invalid_poll_interval_without_claiming(interval):
    run = _enqueue()
    with pytest.raises(CommandError):
        call_command("run_import_worker", once=True, poll_interval=interval)
    run.refresh_from_db()
    assert run.status == "queued" and run.started_at is None
