"""Bearer-only access to the bounded Parliament import queue, never a worker."""

import json
import re
from datetime import date
from hmac import compare_digest
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest, JsonResponse
from django.views.decorators.common import no_append_slash
from django.views.decorators.csrf import csrf_exempt

from .import_jobs import ImportBusy, ImportConflict, enqueue_import
from .models import ImportRun

MAX_REQUEST_BYTES = 4096
REQUEST_FIELDS = {"request_id", "mode", "legislature", "as_of", "confirm_apply"}
RESULT_FIELDS = {"serving", "created_members", "created_records", "ceased_members"}


def _response(data: dict, *, status: int = 200) -> JsonResponse:
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _authorize(request: HttpRequest) -> JsonResponse | None:
    token = settings.IMPORT_API_TOKEN
    if not isinstance(token, str) or len(token) < 32:
        return _response({"error": "Não encontrado."}, status=404)
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or not compare_digest(
        authorization[7:].encode("utf-8"), token.encode("utf-8")
    ):
        return _response({"error": "Acesso recusado."}, status=403)
    return None


def _method_not_allowed(method: str) -> JsonResponse:
    response = _response({"error": "Método não permitido."}, status=405)
    response["Allow"] = method
    return response


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _parameters(request: HttpRequest) -> tuple[UUID, str, str, date | None, bool]:
    content_params = request.content_params or {}
    if request.content_type != "application/json" or content_params.get(
        "charset", "utf-8"
    ).lower() not in {"utf-8", "utf8"}:
        raise ValueError("JSON with UTF-8 encoding required")
    length = request.META.get("CONTENT_LENGTH", "")
    if length and not 0 <= int(length) <= MAX_REQUEST_BYTES:
        raise ValueError("Request too large")
    # Bound the read itself, including requests without a trustworthy length header.
    body = request.read(MAX_REQUEST_BYTES + 1)
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("Request too large")
    data = json.loads(
        body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    if not isinstance(data, dict) or data.keys() - REQUEST_FIELDS:
        raise ValueError("Unsupported import parameters")
    request_id = data.get("request_id")
    mode = data.get("mode", "dry_run")
    legislature = data.get("legislature", "XVII")
    as_of = data.get("as_of", "")
    confirm_apply = data.get("confirm_apply", False)
    if (
        not isinstance(request_id, str)
        or not isinstance(mode, str)
        or mode not in {"dry_run", "apply"}
        or not isinstance(legislature, str)
        or not isinstance(as_of, str)
        or not isinstance(confirm_apply, bool)
    ):
        raise ValueError("Invalid import parameter type")
    if as_of and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of):
        raise ValueError("ISO calendar date required")
    return (
        UUID(request_id),
        mode,
        legislature,
        date.fromisoformat(as_of) if as_of else None,
        confirm_apply,
    )


def _representation(run: ImportRun) -> dict:
    # Even a malformed stored result must not turn this endpoint into a data export.
    result = run.result if isinstance(run.result, dict) else {}
    return {
        "id": str(run.pk),
        "mode": run.mode,
        "legislature": run.legislature,
        "as_of": run.as_of.isoformat(),
        "status": run.status,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "result": {
            key: value
            for key, value in result.items()
            if key in RESULT_FIELDS and type(value) is int and value >= 0
        },
        "error": "A importação falhou. Consulte o histórico protegido."
        if run.status == "failed"
        else "",
    }


@csrf_exempt
@no_append_slash
def import_request(request: HttpRequest) -> JsonResponse:
    denied = _authorize(request)
    if denied is not None:
        return denied
    if request.method != "POST":
        return _method_not_allowed("POST")
    try:
        request_id, mode, legislature, as_of, confirm_apply = _parameters(request)
    except (ValueError, RecursionError, OSError):
        return _response({"error": "Pedido de importação inválido."}, status=400)
    try:
        run = enqueue_import(
            request_id=request_id,
            mode=mode,
            legislature=legislature,
            as_of=as_of,
            confirm_apply=confirm_apply,
            requested_by=None,
            origin="github",
        )
    except ValidationError:
        return _response({"error": "Parâmetros de importação inválidos."}, status=400)
    except (ImportBusy, ImportConflict):
        return _response({"error": "Pedido em conflito com uma importação existente."}, status=409)
    return _response(_representation(run), status=202)


@csrf_exempt
@no_append_slash
def import_detail(request: HttpRequest, run_id: UUID) -> JsonResponse:
    denied = _authorize(request)
    if denied is not None:
        return denied
    if request.method != "GET":
        return _method_not_allowed("GET")
    run = ImportRun.objects.filter(pk=run_id).first()
    if run is None:
        return _response({"error": "Não encontrado."}, status=404)
    return _response(_representation(run))
