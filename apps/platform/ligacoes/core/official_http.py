"""Pinned public HTTPS for the gated official-source collectors, not Parliament.

The caller owns host/route approval and response limits. Keep stdlib HTTP parsing,
but interrupt its blocking reads/writes at the absolute collection deadline.
"""

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from queue import Empty, Queue
from threading import Lock, Thread, Timer
from urllib.parse import urljoin, urlsplit

TIMEOUT = 20
USER_AGENT = "LigacoesPT-editorial-import/1"
MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})


class OfficialHTTPError(OSError):
    """A payload-free failure at the official-source network boundary."""


class _Deadline:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self._lock = Lock()
        self._socket: socket.socket | None = None
        self._closed = False
        self._timer = Timer(max(0, deadline - time.monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Tempo máximo da consulta oficial excedido.")
        return min(TIMEOUT, remaining)

    def _expire(self) -> None:
        with self._lock:
            self._closed = True
            if self._socket is not None:
                # close() alone does not interrupt a response's makefile() reader.
                # Retain this reference even when HTTPConnection clears its sock
                # for a Connection: close response.
                with suppress(OSError):
                    self._socket.shutdown(socket.SHUT_RDWR)
                self._socket.close()

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            if self._closed or time.monotonic() >= self.deadline:
                sock.close()
                raise TimeoutError("Tempo máximo da consulta oficial excedido.")
            self._socket = sock

    def wrap_tls(self, context: ssl.SSLContext, raw: socket.socket, host: str) -> ssl.SSLSocket:
        with self._lock:
            if self._closed:
                raise TimeoutError("Tempo máximo da consulta oficial excedido.")
            self.timeout()
            # wrap_socket transfers the descriptor out of raw. No network I/O is
            # allowed while that transfer is hidden from the watchdog's lock.
            secured = context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
            self._socket = secured
        secured.settimeout(self.timeout())
        secured.do_handshake()
        self.timeout()
        return secured

    def close(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._closed = True
            if self._socket is not None:
                self._socket.close()
                self._socket = None
        self._timer.join()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    _context: ssl.SSLContext

    def __init__(self, host: str, deadline: _Deadline) -> None:
        super().__init__(host, timeout=deadline.timeout())
        self._context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._deadline = deadline

    def connect(self) -> None:
        # A resolver cannot be cancelled portably. The isolated daemon only puts
        # its result in this queue: it cannot open a socket after the caller exits.
        answers: Queue[list[tuple] | OSError] = Queue(maxsize=1)

        def resolve() -> None:
            try:
                answers.put(socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM))
            except OSError as exc:
                answers.put(exc)

        Thread(target=resolve, daemon=True).start()
        try:
            addresses = answers.get(timeout=self._deadline.timeout())
        except Empty as exc:
            raise TimeoutError("Tempo de resolução da fonte excedido.") from exc
        if isinstance(addresses, OSError):
            raise OfficialHTTPError("Não foi possível resolver a fonte oficial.") from addresses
        resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
        if not resolved or any(
            not address.is_global or address.is_multicast or address.is_reserved
            for address in resolved
        ):
            raise OfficialHTTPError("A fonte resolveu para um endereço não público.")
        family, socktype, protocol, _, address = addresses[0]
        raw = socket.socket(family, socktype, protocol)
        self._deadline.attach(raw)
        raw.settimeout(self._deadline.timeout())
        raw.connect(address)
        self.sock = self._deadline.wrap_tls(self._context, raw, self.host)


@contextmanager
def open_connection(host: str, *, deadline: float) -> Iterator[http.client.HTTPSConnection]:
    """Own one connection and its watchdog through the complete response lifetime.

    Consume responses in their own ``with`` block, including rejected responses,
    so their file wrappers close even if HTTPConnection has relinquished them.
    """
    guard = _Deadline(deadline)
    connection: _PinnedHTTPSConnection | None = None
    try:
        connection = _PinnedHTTPSConnection(host, guard)
        yield connection
        # A shutdown can look like EOF for a response without Content-Length.
        # Never accept that partial body as a successful collection.
        guard.timeout()
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            guard.close()


def _target(url: str, allowed: Callable[[str], bool]) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise OfficialHTTPError("Destino oficial inválido.") from exc
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not allowed(url)
    ):
        raise OfficialHTTPError("Destino fora das rotas oficiais autorizadas.")
    return host, (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")


def download(
    url: str,
    *,
    allowed: Callable[[str], bool],
    max_bytes: int,
    deadline: float,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Bounded HTTPS body; every redirect target is revalidated by ``allowed``."""
    if method not in {"GET", "POST"} or (body is not None and method != "POST"):
        raise OfficialHTTPError("Pedido oficial inválido.")
    request_headers = {
        **(headers or {}),
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    for _ in range(MAX_REDIRECTS + 1):
        host, target = _target(url, allowed)
        try:
            with open_connection(host, deadline=deadline) as connection:
                connection.request(method, target, body=body, headers=request_headers)
                with connection.getresponse() as response:
                    if response.status in REDIRECTS:
                        location = response.getheader("Location")
                        # Never replay a request body through a method-changing redirect.
                        if not location or (method == "POST" and response.status not in {307, 308}):
                            raise OfficialHTTPError("Redirecionamento oficial inválido.")
                        url = urljoin(url, location).split("#", 1)[0]
                        continue
                    if response.status != 200:
                        raise OfficialHTTPError(f"A fonte oficial devolveu HTTP {response.status}.")
                    if response.getheader("Content-Encoding", "identity").lower() != "identity":
                        raise OfficialHTTPError("Resposta comprimida recusada.")
                    size = response.getheader("Content-Length")
                    if size is not None and (not size.isdecimal() or int(size) > max_bytes):
                        raise OfficialHTTPError("Resposta oficial demasiado extensa.")
                    content = bytearray()
                    while chunk := response.read1(min(65536, max_bytes + 1 - len(content))):
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            raise OfficialHTTPError("Resposta oficial demasiado extensa.")
                    if size is not None and len(content) != int(size):
                        raise OfficialHTTPError("Resposta oficial truncada.")
                    return bytes(content)
        except OfficialHTTPError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise OfficialHTTPError("Não foi possível obter a fonte oficial em segurança.") from exc
    raise OfficialHTTPError("Demasiados redirecionamentos oficiais.")
