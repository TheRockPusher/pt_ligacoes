"""Submit one manual GitHub import without exposing credentials or source material."""

import http.client
import json
import os
import re
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

TOTAL_TIMEOUT = 25 * 60
REQUEST_TIMEOUT = 30
POLL_INTERVAL = 5
MAX_RESPONSE_BYTES = 16384
COUNT_KEYS = ("serving", "created_members", "created_records", "ceased_members")
STATUSES = {"queued", "running", "succeeded", "failed"}
TIMEOUT_MESSAGE = "Observation timed out; the import was not cancelled. Inspect its stored status."


class DispatchError(Exception):
    """A deliberately static, safe-to-display operational error."""


@dataclass(frozen=True)
class Configuration:
    origin: str
    token: str = field(repr=False)
    request_id: UUID
    mode: str
    legislature: str
    as_of: date
    confirm_apply: bool

    def payload(self) -> dict[str, str | bool]:
        return {
            "request_id": str(self.request_id),
            "mode": self.mode,
            "legislature": self.legislature,
            "as_of": self.as_of.isoformat(),
            "confirm_apply": self.confirm_apply,
        }


@dataclass(frozen=True)
class Run:
    status: str
    counts: dict[str, int]


def configuration(environment: Mapping[str, str]) -> Configuration:
    if (
        environment.get("GITHUB_REF") != "refs/heads/main"
        or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
    ):
        raise DispatchError("Only a manual dispatch from main is permitted.")
    origin = environment.get("IMPORT_BASE_URL", "")
    try:
        parsed = urlsplit(origin)
        valid_origin = (
            origin.startswith("https://")
            and parsed.scheme == "https"
            and parsed.hostname is not None
            and re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
                parsed.hostname,
            )
            is not None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
            and not any(character.isspace() for character in origin)
            and "?" not in origin
            and "#" not in origin
            and not parsed.netloc.endswith(":")
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise DispatchError("IMPORT_BASE_URL must be one exact HTTPS origin.")
    token = environment.get("IMPORT_API_TOKEN", "")
    if not 32 <= len(token) <= 4096 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
        raise DispatchError(
            "IMPORT_API_TOKEN must be a valid bearer token of at least 32 characters."
        )
    mode = environment.get("IMPORT_MODE", "dry_run")
    legislature = environment.get("IMPORT_LEGISLATURE", "XVII")
    confirmation = environment.get("IMPORT_CONFIRM_APPLY", "false")
    if mode not in {"dry_run", "apply"}:
        raise DispatchError("IMPORT_MODE must be dry_run or apply.")
    if not re.fullmatch(r"[IVXLCDM]{1,12}", legislature):
        raise DispatchError("IMPORT_LEGISLATURE must be a Roman numeral.")
    if confirmation not in {"true", "false"} or (mode == "apply" and confirmation != "true"):
        raise DispatchError("Draft application requires explicit IMPORT_CONFIRM_APPLY=true.")
    raw_date = environment.get("IMPORT_AS_OF", "")
    try:
        if raw_date and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_date):
            raise ValueError
        as_of = (
            date.fromisoformat(raw_date)
            if raw_date
            else datetime.now(ZoneInfo("Europe/Lisbon")).date()
        )
    except ValueError:
        raise DispatchError("IMPORT_AS_OF must be a valid YYYY-MM-DD date or blank.") from None
    repository = environment.get("GITHUB_REPOSITORY", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not all(
        re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in (run_id, attempt)
    ):
        raise DispatchError("A valid GitHub repository, run ID and attempt are required.")
    request_id = uuid5(
        NAMESPACE_URL, f"https://github.com/{repository}/actions/runs/{run_id}/{attempt}"
    )
    return Configuration(
        origin.rstrip("/"), token, request_id, mode, legislature, as_of, confirmation == "true"
    )


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DispatchError("The API returned an invalid response.")
        result[key] = value
    return result


def _request(
    config: Configuration, method: str, path: str, deadline: float, body: bytes | None = None
) -> object:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DispatchError(TIMEOUT_MESSAGE)
    origin = urlsplit(config.origin)
    connection = http.client.HTTPSConnection(origin.netloc, timeout=min(REQUEST_TIMEOUT, remaining))
    headers = {"Authorization": f"Bearer {config.token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        # http.client never follows Location: the token only reaches the configured origin.
        if 300 <= response.status < 400:
            raise DispatchError("The API redirected the request; redirects are forbidden.")
        expected_status = 202 if method == "POST" else 200
        if response.status != expected_status:
            raise DispatchError("The API rejected the request. Inspect protected import history.")
        if (
            response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise DispatchError("The API returned an invalid response.")
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise DispatchError("The API response exceeded the permitted size.")
        return json.loads(content.decode("utf-8"), object_pairs_hook=_json_object)
    except (OSError, http.client.HTTPException):
        raise DispatchError(
            "The API connection failed; inspect stored status before another dispatch."
        ) from None
    except (ValueError, RecursionError):
        raise DispatchError("The API returned an invalid response.") from None
    finally:
        connection.close()


def _run(value: object, config: Configuration) -> Run:
    if not isinstance(value, dict) or any(
        value.get(key) != expected
        for key, expected in {
            "id": str(config.request_id),
            "mode": config.mode,
            "legislature": config.legislature,
            "as_of": config.as_of.isoformat(),
        }.items()
    ):
        raise DispatchError("The API returned a different import request.")
    status = value.get("status")
    result = value.get("result")
    if not isinstance(status, str) or status not in STATUSES or not isinstance(result, dict):
        raise DispatchError("The API returned an invalid import status.")
    counts: dict[str, int] = {}
    for key, count in result.items():
        if key not in COUNT_KEYS or type(count) is not int or not 0 <= count <= 2147483647:
            raise DispatchError("The API returned invalid aggregate counts.")
        counts[key] = count
    if status == "succeeded" and (
        set(counts) != set(COUNT_KEYS)
        or counts["serving"] != 230
        or (config.mode == "dry_run" and any(counts[key] for key in COUNT_KEYS[1:]))
    ):
        raise DispatchError("The API returned invalid aggregate counts.")
    return Run(status, counts)


def dispatch(
    config: Configuration, *, timeout: float = TOTAL_TIMEOUT, poll_interval: float = POLL_INTERVAL
) -> Run:
    deadline = time.monotonic() + timeout
    value = _request(
        config, "POST", "/ops/imports/", deadline, json.dumps(config.payload()).encode("utf-8")
    )
    run = _run(value, config)
    path = f"/ops/imports/{config.request_id}/"
    while run.status in {"queued", "running"}:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DispatchError(TIMEOUT_MESSAGE)
        time.sleep(min(poll_interval, remaining))
        previous_status = run.status
        run = _run(_request(config, "GET", path, deadline), config)
        if previous_status == "running" and run.status == "queued":
            raise DispatchError("The API returned an invalid status transition.")
    return run


def _deadline_expired(_signum: int, _frame: object) -> None:
    raise DispatchError(TIMEOUT_MESSAGE)


def main() -> int:
    lines = ["## Parliament import", ""]
    exit_code = 1
    # A wall-clock bound also covers slow response bodies, not just polling/socket timeouts.
    previous_handler = signal.signal(signal.SIGALRM, _deadline_expired)
    signal.alarm(TOTAL_TIMEOUT)
    try:
        config = configuration(os.environ)
        lines.append(f"Request: `{config.request_id}`")
        run = dispatch(config)
        lines.extend([f"Mode: `{config.mode}`", f"Status: `{run.status}`", ""])
        for key in COUNT_KEYS:
            if key in run.counts:
                lines.append(f"- {key}: {run.counts[key]}")
        if run.status == "failed":
            lines.append(
                "Import failed. Inspect protected import history; no automatic retry was made."
            )
        else:
            exit_code = 0
    except DispatchError as error:
        lines.append(str(error))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
    summary = "\n".join(lines) + "\n"
    print(summary, end="")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with Path(summary_path).open("a", encoding="utf-8") as output:
                output.write(summary)
        except OSError:
            print("Could not write the GitHub step summary.", file=sys.stderr)
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
