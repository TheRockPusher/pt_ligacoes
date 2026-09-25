"""Durable Parliament jobs; only the owning PostgreSQL session may finish a run."""

from dataclasses import asdict
from datetime import date
from uuid import UUID

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import ImportRun, editorial_transaction
from .parliament_fetch import ParliamentImportError, validate_legislature
from .parliament_import import ImportResult, apply_snapshot, fetch_snapshot

# Separate from the editorial transaction lock: held across bounded source networking.
_LOCK_NAMESPACE = 0x50544C47
_LOCK_RESOURCE = 0x494D5054
_FAILURE = "A importação falhou; nenhum rascunho desta execução foi aplicado."
_INTERRUPTED = "A execução foi interrompida; não será repetida automaticamente."
_WORKER_ERROR = "O executor de importações não conseguiu concluir a operação com segurança."


class ImportBusy(Exception):
    """A different queued or running request already occupies the importer."""


class ImportConflict(Exception):
    """A request identifier has already been used for different parameters."""


def _require_admin(actor_id: int | None) -> None:
    # Never reuse request.user or its permission cache across queue/fetch boundaries.
    actor = User.objects.filter(pk=actor_id, is_active=True, is_staff=True).first()
    if actor is None or not actor.has_perm("core.run_import"):
        raise PermissionDenied("Não tem permissão para executar importações.")


def _replay(
    run: ImportRun,
    *,
    mode: str,
    legislature: str,
    as_of: date | None,
    origin: str,
    actor_id: int | None,
) -> ImportRun:
    if (
        run.mode != mode
        or run.legislature != legislature
        or (as_of is not None and run.as_of != as_of)
        or run.origin != origin
        or run.requested_by_id != actor_id
    ):
        raise ImportConflict("Este identificador já pertence a outro pedido.")
    # Omitted dates retain the original resolution, even on a later day's replay.
    return run


def enqueue_import(
    *,
    request_id: UUID,
    mode: str,
    legislature: str,
    as_of: date | None,
    requested_by: User | None = None,
    origin: str = "admin",
    confirm_apply: bool = False,
) -> ImportRun:
    if not isinstance(request_id, UUID):
        raise ValidationError("O identificador do pedido deve ser um UUID.")
    if mode not in ImportRun.Mode.values or not isinstance(confirm_apply, bool):
        raise ValidationError("O modo ou a confirmação da importação é inválido.")
    if mode == ImportRun.Mode.APPLY and not confirm_apply:
        raise ValidationError("Confirme explicitamente a aplicação de rascunhos.")
    if not isinstance(legislature, str):
        raise ValidationError("A legislatura é inválida.")
    legislature = legislature.strip().upper()
    try:
        validate_legislature(legislature)
    except ParliamentImportError:
        raise ValidationError("A legislatura deve ser indicada em numeração romana.") from None
    if as_of is not None and type(as_of) is not date:
        raise ValidationError("A data de referência é inválida.")
    if origin not in ImportRun.Origin.values:
        raise ValidationError("A origem da importação é inválida.")
    actor_id = requested_by.pk if requested_by is not None else None
    if origin == ImportRun.Origin.ADMIN:
        _require_admin(actor_id)
    elif requested_by is not None:
        raise ValidationError("Os pedidos do GitHub não podem indicar um utilizador.")

    existing = ImportRun.objects.filter(pk=request_id).first()
    if existing is not None:
        return _replay(
            existing,
            mode=mode,
            legislature=legislature,
            as_of=as_of,
            origin=origin,
            actor_id=actor_id,
        )
    try:
        with transaction.atomic():
            return ImportRun.objects.create(
                id=request_id,
                mode=mode,
                legislature=legislature,
                as_of=as_of if as_of is not None else timezone.localdate(),
                origin=origin,
                requested_by_id=actor_id,
            )
    except IntegrityError as error:
        # The insert can race either an identical UUID or the global active-job index.
        existing = ImportRun.objects.filter(pk=request_id).first()
        if existing is not None:
            return _replay(
                existing,
                mode=mode,
                legislature=legislature,
                as_of=as_of,
                origin=origin,
                actor_id=actor_id,
            )
        diagnostic = getattr(error.__cause__, "diag", None)
        if getattr(diagnostic, "constraint_name", None) == "import_single_active":
            raise ImportBusy("Já existe uma importação em fila ou em execução.") from None
        raise


def _owns_lock(session: object) -> bool:
    # A reconnect must not turn the former owner into a new writer/finaliser.
    if connection.connection is not session:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid() AND classid = %s AND objid = %s "
            "AND objsubid = 2 AND granted)",
            [_LOCK_NAMESPACE, _LOCK_RESOURCE],
        )
        row = cursor.fetchone()
    return row is not None and row[0]


def _require_owner(session: object) -> None:
    if not _owns_lock(session):
        raise RuntimeError(_WORKER_ERROR)


def _check_actor(run: ImportRun) -> None:
    if run.origin == ImportRun.Origin.ADMIN:
        _require_admin(run.requested_by_id)


def run_next_import() -> ImportRun | None:
    """Claim at most one run; a lost session never retries or finalises draft writes."""
    session: object | None = None
    acquired = False
    run: ImportRun | None = None
    try:
        if not connection.get_autocommit():
            raise RuntimeError(_WORKER_ERROR)
        with connection.cursor() as cursor:
            session = connection.connection
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [_LOCK_NAMESPACE, _LOCK_RESOURCE])
            row = cursor.fetchone()
            acquired = row is not None and row[0]
        if not acquired:
            return None
        _require_owner(session)
        with transaction.atomic():
            # An active owner would still hold the session lock. These runs are orphans.
            ImportRun.objects.filter(status=ImportRun.Status.RUNNING).update(
                status=ImportRun.Status.FAILED,
                finished_at=timezone.now(),
                result={},
                error=_INTERRUPTED,
            )
            run = (
                ImportRun.objects.select_for_update()
                .filter(status=ImportRun.Status.QUEUED)
                .order_by("created_at", "pk")
                .first()
            )
            if run is None:
                return None
            run.status = ImportRun.Status.RUNNING
            run.started_at = timezone.now()
            run.save(update_fields=["status", "started_at"])

        _check_actor(run)
        # Autocommit here: do not retain row locks/transactions during remote I/O.
        snapshot = fetch_snapshot(legislature=run.legislature, as_of=run.as_of, expected_count=230)
        if (
            snapshot.legislature != run.legislature
            or snapshot.as_of != run.as_of
            or snapshot.expected_count != 230
            or len(snapshot.members) != 230
        ):
            raise ParliamentImportError("A complete matching snapshot is required.")
        with editorial_transaction():
            _require_owner(session)
            current = ImportRun.objects.select_for_update().get(pk=run.pk)
            if current.status != ImportRun.Status.RUNNING:
                raise RuntimeError(_WORKER_ERROR)
            _check_actor(current)
            result = (
                apply_snapshot(snapshot)
                if current.mode == ImportRun.Mode.APPLY
                else ImportResult(len(snapshot.members), 0, 0, 0)
            )
            # Revocation during application also rolls back the entire transaction.
            _check_actor(current)
            _require_owner(session)
            current.status = ImportRun.Status.SUCCEEDED
            current.result = asdict(result)
            current.finished_at = timezone.now()
            current.save(update_fields=["status", "result", "finished_at"])
        return current
    except Exception:
        # No exception text, tracebacks, source payloads or opaque URLs reach logs/history.
        if run is not None and session is not None:
            try:
                if _owns_lock(session):
                    with transaction.atomic():
                        ImportRun.objects.filter(pk=run.pk, status=ImportRun.Status.RUNNING).update(
                            status=ImportRun.Status.FAILED,
                            finished_at=timezone.now(),
                            result={},
                            error=_FAILURE,
                        )
                    return ImportRun.objects.get(pk=run.pk)
            except Exception:
                raise RuntimeError(_WORKER_ERROR) from None
        # An uncertain commit must be reconciled by the next owner, never labelled failed here.
        raise RuntimeError(_WORKER_ERROR) from None
    finally:
        if acquired and connection.connection is session:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(%s, %s)", [_LOCK_NAMESPACE, _LOCK_RESOURCE]
                    )
            except Exception:
                # Closing the session releases its lock; do not mask a committed success.
                connection.close()
