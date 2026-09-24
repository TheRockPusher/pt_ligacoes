from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from threading import Event, Thread
from time import monotonic

import pytest
from django.db import connection, connections, transaction

from ligacoes.core.models import Entity, Evidence, Relationship, ReviewEvent, Source
from ligacoes.core.services import publish_relationship
from ligacoes.public.selectors import public_relationships

pytestmark = pytest.mark.django_db(transaction=True)

_WAIT_SECONDS = 10


@contextmanager
def _database_worker(operation: Callable[[], object]) -> Generator[tuple[int, Future[None]]]:
    backend: Future[int] = Future()
    completed: Future[None] = Future()

    def run():
        database = connections["default"]
        try:
            with database.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_backend_pid(), "
                    "set_config('statement_timeout', %s, false), "
                    "set_config('lock_timeout', %s, false)",
                    ["15000", "15000"],
                )
                row = cursor.fetchone()
                assert row is not None
                backend.set_result(row[0])
            operation()
        except Exception as error:
            if not backend.done():
                backend.set_exception(error)
            completed.set_exception(error)
        else:
            completed.set_result(None)
        finally:
            database.close()

    worker = Thread(target=run)
    worker.start()
    try:
        yield backend.result(timeout=_WAIT_SECONDS), completed
    finally:
        worker.join(timeout=2 * _WAIT_SECONDS)
        assert not worker.is_alive(), "Editorial database worker did not terminate"
        completed.result(timeout=0)


def _wait_for_block_or_commit(waiting_pid: int, blocker_pid: int, completed: Future[None]):
    """Allow both the old unprotected commit and a serialized database lock wait."""
    deadline = monotonic() + _WAIT_SECONDS
    while monotonic() < deadline:
        if completed.done():
            completed.result()
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type, pg_blocking_pids(pid) "
                "FROM pg_stat_activity WHERE pid = %s",
                [waiting_pid],
            )
            state = cursor.fetchone()
        if state and state[0] == "Lock" and blocker_pid in state[1]:
            return
        try:
            completed.result(timeout=0.01)
        except TimeoutError:
            continue
    pytest.fail("Concurrent editor neither committed nor waited on the paused editor")


def _pause_after_snapshot(monkeypatch: pytest.MonkeyPatch, record: Entity | Source | Evidence):
    reached = Event()
    release = Event()
    original_clean = type(record).full_clean

    def paused_clean(instance, *args, **kwargs):
        original_clean(instance, *args, **kwargs)
        if instance is record:
            reached.set()
            assert release.wait(timeout=2 * _WAIT_SECONDS), "Paused editor was not released"

    # Only schedule the selected instance; all real validation and SQL still execute.
    monkeypatch.setattr(type(record), "full_clean", paused_clean)
    return reached, release


def test_stale_evidence_delete_invalidates_its_current_reviewed_parent(catalog, reviewer):
    stale = Evidence.objects.get(pk=catalog.evidence.pk)
    current_parent = Relationship.objects.create(
        subject=catalog.person, object=catalog.company, kind="membership"
    )
    retained = Evidence.objects.create(
        relationship=current_parent,
        source=catalog.source,
        excerpt="Outra passagem fictícia que continua a suportar a relação.",
        is_public=True,
    )
    fresh = Evidence.objects.get(pk=stale.pk)
    fresh.relationship = current_parent
    fresh.save()
    publish_relationship(current_parent, reviewer)
    assert public_relationships().filter(pk=current_parent.pk).exists()

    stale.delete()

    current_parent.refresh_from_db()
    assert not Evidence.objects.filter(pk=fresh.pk).exists()
    assert Evidence.objects.filter(pk=retained.pk, is_public=True).exists()
    assert current_parent.status == "draft"
    assert current_parent.reviewed_by_id is None
    assert current_parent.reviewed_at is None
    assert not public_relationships().filter(pk=current_parent.pk).exists()
    assert list(
        ReviewEvent.objects.filter(relationship=current_parent)
        .order_by("created_at", "pk")
        .values_list("action", "reviewer_id")
    ) == [("publish", reviewer.pk), ("invalidate", None)]
    assert not ReviewEvent.objects.filter(relationship=catalog.relation).exists()

    publish_relationship(current_parent, reviewer)
    visible = public_relationships().get(pk=current_parent.pk)
    assert [item.pk for item in visible.public_evidence] == [retained.pk]


def test_overlapping_unchanged_evidence_save_cannot_publish_unreviewed_overwrite(
    catalog, reviewer, monkeypatch
):
    stale = Evidence.objects.get(pk=catalog.evidence.pk)
    original_excerpt = stale.excerpt
    reviewed_excerpt = "Passagem fictícia corrigida e revista na edição concorrente."
    reached, release = _pause_after_snapshot(monkeypatch, stale)

    def edit_and_review():
        with transaction.atomic():
            fresh = Evidence.objects.get(pk=stale.pk)
            fresh.excerpt = reviewed_excerpt
            fresh.save()
            publish_relationship(catalog.relation, reviewer)

    with ExitStack() as workers:
        try:
            stale_pid, _ = workers.enter_context(_database_worker(stale.save))
            assert reached.wait(timeout=_WAIT_SECONDS), "Stale save did not read its snapshot"
            fresh_pid, completed = workers.enter_context(_database_worker(edit_and_review))
            _wait_for_block_or_commit(fresh_pid, stale_pid, completed)
        finally:
            # Do not wait for the fresh commit while a correct lock holds it back.
            release.set()

    catalog.evidence.refresh_from_db()
    catalog.relation.refresh_from_db()
    visible = public_relationships().filter(pk=catalog.relation.pk).first()
    events = list(
        ReviewEvent.objects.filter(relationship=catalog.relation)
        .order_by("created_at", "pk")
        .values_list("action", "reviewer_id")
    )
    if catalog.evidence.excerpt == original_excerpt:
        # If the stale write follows review, restoring old text must withdraw it.
        assert visible is None
        assert catalog.relation.status == "draft"
        assert catalog.relation.reviewed_by_id is None
        assert catalog.relation.reviewed_at is None
        assert events == [("publish", reviewer.pk), ("invalidate", None)]
    else:
        assert catalog.evidence.excerpt == reviewed_excerpt
        assert catalog.relation.status == "published"
        assert catalog.relation.reviewed_by_id == reviewer.pk
        assert catalog.relation.reviewed_at is not None
        assert visible is not None
        assert [(item.pk, item.excerpt) for item in visible.public_evidence] == [
            (catalog.evidence.pk, reviewed_excerpt)
        ]
        assert events == [("publish", reviewer.pk)]


@pytest.mark.parametrize(
    ("record_name", "field", "value"),
    [
        ("person", "name", "Pessoa Alfa corrigida — personagem fictícia"),
        ("source", "title", "Documento fictício com título corrigido"),
    ],
)
def test_dependency_edit_and_publication_serialize_without_database_abort(
    published, reviewer, monkeypatch, record_name, field, value
):
    record = getattr(published, record_name)
    setattr(record, field, value)
    reached, release = _pause_after_snapshot(monkeypatch, record)

    def review():
        publish_relationship(published.relation, reviewer)

    with ExitStack() as workers:
        try:
            editor_pid, _ = workers.enter_context(_database_worker(record.save))
            assert reached.wait(timeout=_WAIT_SECONDS), "Editor did not lock its persisted record"
            reviewer_pid, completed = workers.enter_context(_database_worker(review))
            _wait_for_block_or_commit(reviewer_pid, editor_pid, completed)
        finally:
            release.set()

    record.refresh_from_db()
    published.relation.refresh_from_db()
    assert getattr(record, field) == value
    assert published.relation.status == "published"
    assert published.relation.reviewed_by_id == reviewer.pk
    assert published.relation.reviewed_at is not None
    visible = public_relationships().get(pk=published.relation.pk)
    if record_name == "person":
        assert visible.subject.name == value
    else:
        assert visible.public_evidence[0].source.title == value
    assert list(
        ReviewEvent.objects.filter(relationship=published.relation)
        .order_by("created_at", "pk")
        .values_list("action", "reviewer_id")
    ) == [("publish", reviewer.pk), ("invalidate", None), ("publish", reviewer.pk)]
