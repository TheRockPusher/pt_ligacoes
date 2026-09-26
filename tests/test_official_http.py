import http.client
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from threading import Event, Thread
from unittest.mock import patch

import pytest

from ligacoes.core.official_http import OfficialHTTPError, _PinnedHTTPSConnection, open_connection


@contextmanager
def fictional_peer(
    serve: Callable[[socket.socket, Event], None], *, tls: bool = False
) -> Iterator[None]:
    """Exercise real stdlib framing on local sockets, without external DNS or TLS."""
    client, server = socket.socketpair()
    stop = Event()
    server.settimeout(3)

    def run() -> None:
        with server, suppress(OSError):
            serve(server, stop)

    def connect(connection: _PinnedHTTPSConnection) -> None:
        # The inactivity timeout deliberately exceeds the absolute test deadline.
        client.settimeout(3)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        connection._deadline.attach(client)
        if tls:
            connection.sock = connection._deadline.wrap_tls(
                connection._context, client, connection.host
            )
        else:
            connection.sock = client

    worker = Thread(target=run, daemon=True)
    worker.start()
    try:
        with patch("ligacoes.core.official_http._PinnedHTTPSConnection.connect", connect):
            yield
    finally:
        stop.set()
        client.close()
        with suppress(OSError):
            server.shutdown(socket.SHUT_RDWR)
        server.close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("phase", ["headers", "chunk-size", "trailers", "close-delimited-body"])
def test_absolute_deadline_interrupts_dripping_response_framing(phase: str):
    started_framing = Event()

    def serve(peer: socket.socket, stop: Event) -> None:
        peer.recv(4096)
        if phase == "headers":
            prefix = b"HTTP/1.1 200 OK\r\nX-Fictional: "
        elif phase == "close-delimited-body":
            prefix = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n"
        else:
            prefix = b"HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n"
            if phase == "trailers":
                prefix += b"1\r\nx\r\n0\r\nX-Fictional: "
        peer.sendall(prefix)
        started_framing.set()
        source_deadline = time.monotonic() + 2
        while time.monotonic() < source_deadline and not stop.wait(0.01):
            peer.sendall(b"1")

    with fictional_peer(serve):
        started = time.monotonic()
        with (
            pytest.raises((OSError, http.client.HTTPException)),
            open_connection("fonte-ficticia.invalid", deadline=started + 0.3) as connection,
        ):
            connection.request("GET", "/ficcao")
            with connection.getresponse() as response:
                if phase != "headers":
                    # http.client has relinquished the socket, but the watchdog
                    # must still interrupt the response's file-wrapper reads.
                    assert connection.sock is None
                while response.read1(64):
                    pass
        elapsed = time.monotonic() - started
    assert started_framing.is_set()
    assert elapsed < 1.5


def test_absolute_deadline_interrupts_blocked_request_transmission():
    received_request = Event()

    def serve(peer: socket.socket, stop: Event) -> None:
        peer.recv(1)
        received_request.set()
        stop.wait(2)

    with fictional_peer(serve):
        started = time.monotonic()
        with (
            pytest.raises(OSError),
            open_connection("fonte-ficticia.invalid", deadline=started + 0.3) as connection,
        ):
            connection.request("POST", "/ficcao", body=b"x" * (2 * 1024 * 1024))
        elapsed = time.monotonic() - started
    assert received_request.is_set()
    assert elapsed < 1.5


def test_absolute_deadline_interrupts_tls_handshake_after_socket_transfer():
    started_handshake = Event()

    def serve(peer: socket.socket, stop: Event) -> None:
        peer.recv(65536)
        started_handshake.set()
        # An incomplete fictional handshake record keeps the peer active but
        # never completes TLS. The raw socket has already transferred its fd.
        peer.sendall(b"\x16\x03\x03\x3f\xff")
        source_deadline = time.monotonic() + 2
        while time.monotonic() < source_deadline and not stop.wait(0.01):
            peer.sendall(b"\x00")

    with fictional_peer(serve, tls=True):
        started = time.monotonic()
        with (
            pytest.raises(OSError),
            open_connection("fonte-ficticia.invalid", deadline=started + 0.3) as connection,
        ):
            connection.request("GET", "/ficcao")
        elapsed = time.monotonic() - started
    assert started_handshake.is_set()
    assert elapsed < 1.5


@pytest.mark.parametrize("chunked", [False, True])
def test_complete_fictional_response_is_returned_and_peer_is_closed(chunked: bool):
    peer_closed = Event()
    body = b'{"ficcao":true}'

    def serve(peer: socket.socket, stop: Event) -> None:
        peer.recv(4096)
        if chunked:
            framing = b"Transfer-Encoding: chunked\r\n"
            wire_body = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        else:
            framing = f"Content-Length: {len(body)}\r\n".encode()
            wire_body = body
        peer.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n" + framing + b"\r\n" + wire_body)
        if peer.recv(1) == b"":
            peer_closed.set()

    with fictional_peer(serve):
        with open_connection("fonte-ficticia.invalid", deadline=time.monotonic() + 2) as connection:
            connection.request("GET", "/ficcao")
            with connection.getresponse() as response:
                result = response.read()
        assert peer_closed.wait(1)
    assert result == body


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"]
)
def test_nonpublic_or_mixed_dns_never_opens_a_socket(address: str):
    with (
        patch(
            "socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
            ],
        ),
        patch("socket.socket", side_effect=AssertionError("Ligação recusada antes do socket")),
        pytest.raises(OfficialHTTPError),
        open_connection("fonte-ficticia.invalid", deadline=time.monotonic() + 2) as connection,
    ):
        connection.request("GET", "/ficcao")


def test_stalled_dns_returns_at_deadline_and_late_resolution_cannot_connect():
    resolving = Event()
    release = Event()
    resolved = Event()

    def resolve(*args: object, **kwargs: object) -> list[tuple]:
        resolving.set()
        release.wait(3)
        resolved.set()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    with (
        patch("socket.getaddrinfo", resolve),
        patch("socket.socket", side_effect=AssertionError("Nenhuma ligação após DNS expirado")),
    ):
        started = time.monotonic()
        try:
            with (
                pytest.raises(TimeoutError),
                open_connection("fonte-ficticia.invalid", deadline=started + 0.3) as connection,
            ):
                connection.request("GET", "/ficcao")
            elapsed = time.monotonic() - started
        finally:
            release.set()
            assert resolved.wait(2)
    assert resolving.is_set()
    assert elapsed < 1.5
