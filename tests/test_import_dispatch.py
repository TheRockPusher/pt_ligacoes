import http.client
import json
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from scripts import import_dispatch

FAKE_TOKEN = secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Reply:
    status: int
    body: object
    content_type: str = "application/json"
    location: str | None = None


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    authorization: str | None
    body: bytes


@pytest.fixture
def environment() -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "fictitious/fixture",
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "1",
        "IMPORT_BASE_URL": "https://imports.example.invalid",
        "IMPORT_API_TOKEN": FAKE_TOKEN,
        "IMPORT_MODE": "dry_run",
        "IMPORT_LEGISLATURE": "XVII",
        "IMPORT_AS_OF": "2026-01-15",
        "IMPORT_CONFIRM_APPLY": "false",
    }


@pytest.fixture
def serve(monkeypatch) -> Iterator[Callable[[list[Reply]], tuple[str, list[Request]]]]:
    # Real loopback HTTP exercises requests, status codes, bodies and redirects.
    # Only TLS is substituted; production has no HTTP switch or alternate origin.
    monkeypatch.setattr(import_dispatch.http.client, "HTTPSConnection", http.client.HTTPConnection)
    servers: list[tuple[HTTPServer, Thread]] = []

    def start(replies: list[Reply]) -> tuple[str, list[Request]]:
        requests: list[Request] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append(
                    Request(self.command, self.path, self.headers.get("Authorization"), body)
                )
                reply = replies.pop(0) if replies else Reply(500, {})
                payload = (
                    reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
                )
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.content_type)
                self.send_header("Content-Length", str(len(payload)))
                if reply.location:
                    self.send_header("Location", reply.location)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"https://127.0.0.1:{server.server_port}", requests

    try:
        yield start
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive()


def representation(config: import_dispatch.Configuration, status: str) -> dict[str, object]:
    return {
        "id": str(config.request_id),
        "mode": config.mode,
        "legislature": config.legislature,
        "as_of": config.as_of.isoformat(),
        "status": status,
        "created_at": "2026-01-15T10:00:00Z",
        "started_at": None,
        "finished_at": None,
        "result": {
            "serving": 230,
            "created_members": 0,
            "created_records": 0,
            "ceased_members": 0,
        }
        if status == "succeeded"
        else {},
        "error": "",
    }


def test_submission_observes_queue_running_and_validated_full_snapshot(environment, serve):
    config = import_dispatch.configuration(environment)
    origin, requests = serve(
        [
            Reply(202, representation(config, "queued")),
            Reply(200, representation(config, "running")),
            Reply(200, representation(config, "succeeded")),
        ]
    )
    result = import_dispatch.dispatch(replace(config, origin=origin), poll_interval=0)
    assert result.status == "succeeded"
    assert result.counts == {
        "serving": 230,
        "created_members": 0,
        "created_records": 0,
        "ceased_members": 0,
    }
    assert [(request.method, request.path) for request in requests] == [
        ("POST", "/ops/imports/"),
        ("GET", f"/ops/imports/{config.request_id}/"),
        ("GET", f"/ops/imports/{config.request_id}/"),
    ]
    assert all(request.authorization == f"Bearer {FAKE_TOKEN}" for request in requests)
    assert json.loads(requests[0].body) == {
        "request_id": str(config.request_id),
        "mode": "dry_run",
        "legislature": "XVII",
        "as_of": "2026-01-15",
        "confirm_apply": False,
    }


def test_confirmed_apply_reports_draft_counts_without_publication(environment, serve):
    environment.update(IMPORT_MODE="apply", IMPORT_CONFIRM_APPLY="true")
    config = import_dispatch.configuration(environment)
    response = representation(config, "succeeded")
    response["result"] = {
        "serving": 230,
        "created_members": 230,
        "created_records": 460,
        "ceased_members": 2,
    }
    origin, requests = serve([Reply(202, response)])
    result = import_dispatch.dispatch(replace(config, origin=origin))
    assert result.counts["created_records"] == 460
    payload = json.loads(requests[0].body)
    assert payload["confirm_apply"] is True
    assert payload["mode"] == "apply"
    assert "publish" not in payload


def test_request_identity_is_stable_for_replay_but_distinct_for_new_attempt(environment, serve):
    first = import_dispatch.configuration(environment)
    replay = import_dispatch.configuration(environment)
    next_attempt = import_dispatch.configuration({**environment, "GITHUB_RUN_ATTEMPT": "2"})
    assert first.request_id == replay.request_id
    assert first.request_id != next_attempt.request_id
    origin, requests = serve([Reply(202, representation(first, "succeeded")) for _ in range(2)])
    for config in (first, replay):
        assert import_dispatch.dispatch(replace(config, origin=origin)).status == "succeeded"
    assert requests[0].body == requests[1].body


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_receive_bearer_credentials(environment, serve, status):
    config = import_dispatch.configuration(environment)
    destination, leaked = serve([Reply(200, {})])
    origin, requests = serve([Reply(status, {}, location=destination + "/capture")])
    with pytest.raises(import_dispatch.DispatchError, match="redirect"):
        import_dispatch.dispatch(replace(config, origin=origin))
    assert len(requests) == 1
    assert leaked == []


def test_poll_redirect_is_not_followed(environment, serve):
    config = import_dispatch.configuration(environment)
    destination, leaked = serve([Reply(200, {})])
    origin, requests = serve(
        [
            Reply(202, representation(config, "queued")),
            Reply(307, {}, location=destination + "/capture"),
        ]
    )
    with pytest.raises(import_dispatch.DispatchError, match="redirect"):
        import_dispatch.dispatch(replace(config, origin=origin), poll_interval=0)
    assert [request.method for request in requests] == ["POST", "GET"]
    assert leaked == []


@pytest.mark.parametrize("status", [400, 403, 404, 409, 500])
def test_rejection_never_retries_or_displays_response_body(environment, serve, status):
    config = import_dispatch.configuration(environment)
    origin, requests = serve([Reply(status, {"error": FAKE_TOKEN})])
    with pytest.raises(import_dispatch.DispatchError) as raised:
        import_dispatch.dispatch(replace(config, origin=origin))
    assert FAKE_TOKEN not in str(raised.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", "00000000-0000-0000-0000-000000000000"),
        ("mode", "apply"),
        ("legislature", "XVI"),
        ("as_of", "2026-01-16"),
        ("status", "unknown"),
        ("status", ["succeeded"]),
        ("result", {"serving": True}),
        ("result", {"serving": -1}),
        ("result", {"serving": 230.0}),
        ("result", {"serving": 2147483648}),
        ("result", {"serving": {"names": ["fictitious"]}}),
        ("result", {"serving": 230, "source_record": "fictitious-private-material"}),
        ("result", {}),
        (
            "result",
            {"serving": 229, "created_members": 0, "created_records": 0, "ceased_members": 0},
        ),
        (
            "result",
            {"serving": 230, "created_members": 1, "created_records": 0, "ceased_members": 0},
        ),
    ],
)
def test_response_identity_status_and_count_boundaries(environment, serve, key, value):
    config = import_dispatch.configuration(environment)
    response = representation(config, "succeeded")
    response[key] = value
    origin, _ = serve([Reply(202, response)])
    with pytest.raises(import_dispatch.DispatchError):
        import_dispatch.dispatch(replace(config, origin=origin))


@pytest.mark.parametrize(
    "reply",
    [
        Reply(202, b'{"status":"queued","status":"succeeded"}'),
        Reply(202, b"not-json"),
        Reply(202, b"\xff"),
        Reply(202, b"{}", content_type="text/html"),
        Reply(202, b"x" * (import_dispatch.MAX_RESPONSE_BYTES + 1)),
    ],
)
def test_malformed_and_oversized_responses_fail_closed(environment, serve, reply):
    config = import_dispatch.configuration(environment)
    origin, _ = serve([reply])
    with pytest.raises(import_dispatch.DispatchError):
        import_dispatch.dispatch(replace(config, origin=origin))


def test_running_job_cannot_return_to_queue(environment, serve):
    config = import_dispatch.configuration(environment)
    origin, _ = serve(
        [
            Reply(202, representation(config, "running")),
            Reply(200, representation(config, "queued")),
        ]
    )
    with pytest.raises(import_dispatch.DispatchError, match="transition"):
        import_dispatch.dispatch(replace(config, origin=origin), poll_interval=0)


def test_timeout_leaves_the_accepted_import_uncancelled(environment, serve):
    config = import_dispatch.configuration(environment)
    origin, requests = serve([Reply(202, representation(config, "queued"))])
    with pytest.raises(import_dispatch.DispatchError, match="not cancelled"):
        import_dispatch.dispatch(replace(config, origin=origin), timeout=0.1, poll_interval=1)
    assert [request.method for request in requests] == ["POST"]


@pytest.mark.parametrize(
    "origin",
    [
        "http://imports.example.invalid",
        "https://user:password@imports.example.invalid",
        "https://imports.example.invalid/ops",
        "https://imports.example.invalid?token=secret",
        "https://imports.example.invalid#fragment",
        "https://imports.example.invalid?",
        "https://imports.example.invalid#",
        "https://imports.example.invalid:70000",
        "https://imports.example.invalid:0",
        "https://imports.example.invalid\\@other.invalid",
        "https://imports.example.invalid\n",
        " https://imports.example.invalid",
        "https://",
        "https://imports.example.invalid:",
        "https://bad..invalid",
        "https://-bad.invalid",
    ],
)
def test_only_exact_https_origins_are_accepted(environment, origin):
    environment["IMPORT_BASE_URL"] = origin
    with pytest.raises(import_dispatch.DispatchError):
        import_dispatch.configuration(environment)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_REF", "refs/heads/untrusted"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_RUN_ID", "$(echo unsafe)"),
        ("GITHUB_RUN_ATTEMPT", "0"),
        ("GITHUB_REPOSITORY", "missing-owner"),
        ("IMPORT_MODE", "publish"),
        ("IMPORT_LEGISLATURE", "XVII; echo unsafe"),
        ("IMPORT_CONFIRM_APPLY", "yes"),
        ("IMPORT_AS_OF", "2026-02-30"),
        ("IMPORT_AS_OF", "20260115"),
        ("IMPORT_AS_OF", "2026-W03-4"),
        ("IMPORT_API_TOKEN", "short"),
        ("IMPORT_API_TOKEN", FAKE_TOKEN + "\r\nInjected: header"),
    ],
)
def test_untrusted_context_and_invalid_inputs_fail_before_submission(environment, key, value):
    environment[key] = value
    with pytest.raises(import_dispatch.DispatchError):
        import_dispatch.configuration(environment)


def test_blank_date_uses_lisbon_calendar_at_utc_day_boundary(environment, monkeypatch):
    class Clock:
        @staticmethod
        def now(zone: tzinfo) -> datetime:
            return datetime(2026, 6, 30, 23, 30, tzinfo=UTC).astimezone(zone)

    monkeypatch.setattr(import_dispatch, "datetime", Clock)
    environment["IMPORT_AS_OF"] = ""
    config = import_dispatch.configuration(environment)
    assert config.payload()["as_of"] == "2026-07-01"


def test_apply_requires_explicit_confirmation(environment):
    environment["IMPORT_MODE"] = "apply"
    with pytest.raises(import_dispatch.DispatchError):
        import_dispatch.configuration(environment)


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_cli_summary_contains_only_validated_metadata_and_aggregates(
    environment, serve, monkeypatch, tmp_path, capsys, status
):
    config = import_dispatch.configuration(environment)
    response = representation(config, status)
    unsafe = f"::error::{FAKE_TOKEN} fictitious-private-material"
    response.update(error=unsafe, source_records=[unsafe], status_url="https://untrusted.invalid")
    origin, requests = serve([Reply(202, response)])
    environment["IMPORT_BASE_URL"] = origin
    summary = tmp_path / "summary"
    environment["GITHUB_STEP_SUMMARY"] = str(summary)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert import_dispatch.main() == (0 if status == "succeeded" else 1)
    output = capsys.readouterr().out
    assert str(config.request_id) in output
    assert status in output
    assert summary.read_text() == output
    assert FAKE_TOKEN not in output
    assert "fictitious-private-material" not in output
    assert "untrusted.invalid" not in output
    assert "::error::" not in output
    assert [request.method for request in requests] == ["POST"]
    if status == "succeeded":
        assert "serving: 230" in output
