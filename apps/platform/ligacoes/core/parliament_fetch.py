"""Only the two official AR catalogues and their selected JSON downloads are fetched."""

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

CATALOGUES = {
    "roster": "https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx",
    "biography": "https://www.parlamento.pt/Cidadania/Paginas/DARegistoBiografico.aspx",
}
PREFIXES = {"roster": "InformacaoBase", "biography": "RegistoBiografico"}
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT = 20
TOTAL_TIMEOUT = 90


class ParliamentImportError(ValueError):
    """An incomplete, ambiguous or unsafe snapshot must not reach the database."""


@dataclass(frozen=True)
class Download:
    content: bytes
    url: str


def validate_legislature(legislature: str) -> None:
    if not re.fullmatch(r"[IVXLCDM]{1,12}", legislature):
        raise ParliamentImportError("Legislature must be a Roman numeral.")


def validate_url(url: str, dataset: str, legislature: str) -> None:
    """Allow exact routes/query shapes, not a suffix-based hostname allowlist."""
    validate_legislature(legislature)
    if dataset not in CATALOGUES or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ParliamentImportError("Invalid official source URL.")
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
            or "\\" in url
        ):
            raise ValueError
    except ValueError as exc:
        raise ParliamentImportError("Invalid official source URL.") from exc
    if any(len(values) != 1 or not values[0] for values in query.values()):
        raise ParliamentImportError("Ambiguous official source query.")
    catalogue = urlsplit(CATALOGUES[dataset])
    if (
        parsed.netloc == catalogue.netloc
        and parsed.path == catalogue.path
        and (not query or set(query) == {"t", "Path"})
    ):
        return
    filename = f"{PREFIXES[dataset]}{legislature}_json.txt"
    if (
        parsed.netloc == "app.parlamento.pt"
        and parsed.path == "/webutils/docs/doc.txt"
        and set(query) == {"path", "fich", "Inline"}
        and query["fich"] == [filename]
        and query["Inline"] == ["true"]
    ):
        return
    raise ParliamentImportError("URL is outside the required official source routes.")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        # Resolve once, reject mixed public/private answers, then connect to the checked
        # numeric address. TLS still authenticates the original hostname (no DNS rebinding).
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
        if not resolved or any(
            not address.is_global or address.is_multicast or address.is_reserved
            for address in resolved
        ):
            raise ParliamentImportError("Official host resolved to a non-public address.")
        family, socktype, protocol, _, address = addresses[0]
        raw = socket.socket(family, socktype, protocol)
        try:
            raw.settimeout(TIMEOUT)
            raw.connect(address)
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.sock = context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_url(url: str, dataset: str, legislature: str) -> Download:
    """Bounded HTTPS, no proxy/cookies, and every redirect revalidated before connecting."""
    deadline = time.monotonic() + TOTAL_TIMEOUT
    for _ in range(4):
        if time.monotonic() >= deadline:
            raise ParliamentImportError("Official source exceeded the time limit.")
        validate_url(url, dataset, legislature)
        parsed = urlsplit(url)
        connection = _PinnedHTTPSConnection(parsed.hostname or "", timeout=TIMEOUT)
        try:
            connection.request(
                "GET",
                parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                headers={
                    "User-Agent": "LigacoesPT-editorial-import/1",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ParliamentImportError("Official redirect has no destination.")
                target = urljoin(url, location)
                validate_url(target, dataset, legislature)
                if urlsplit(target).netloc != parsed.netloc:
                    raise ParliamentImportError("Cross-host redirects are not permitted.")
                url = target
                continue
            if response.status != 200:
                raise ParliamentImportError(f"Official source returned HTTP {response.status}.")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ParliamentImportError("Compressed source responses are not accepted.")
            size = response.getheader("Content-Length")
            if size is not None and (not size.isdecimal() or int(size) > MAX_BYTES):
                raise ParliamentImportError("Official source exceeds the size limit.")
            content = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ParliamentImportError("Official source exceeded the time limit.")
                if connection.sock is not None:
                    connection.sock.settimeout(min(TIMEOUT, remaining))
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ParliamentImportError("Official source exceeds the size limit.")
            if size is not None and len(content) != int(size):
                raise ParliamentImportError("Official source response was truncated.")
            return Download(bytes(content), url)
        except (OSError, http.client.HTTPException) as exc:
            raise ParliamentImportError("Could not retrieve the official source safely.") from exc
        finally:
            connection.close()
    raise ParliamentImportError("Too many official source redirects.")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = ""

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.label += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href is not None:
            self.links.append((self.href, self.label.strip()))
            self.href = None


def discover_download(dataset: str, legislature: str) -> Download:
    validate_legislature(legislature)
    catalogue = CATALOGUES[dataset]
    filename = f"{PREFIXES[dataset]}{legislature}_json.txt"
    page = fetch_url(catalogue, dataset, legislature)
    for depth in range(2):
        links = _Links()
        try:
            links.feed(page.content.decode("utf-8-sig"))
        except UnicodeError as exc:
            raise ParliamentImportError("Official catalogue is not UTF-8.") from exc
        downloads = {
            urljoin(page.url, href)
            for href, _ in links.links
            if parse_qs(urlsplit(href).query).get("fich") == [filename]
        }
        if len(downloads) == 1:
            return fetch_url(downloads.pop(), dataset, legislature)
        if downloads or depth:
            raise ParliamentImportError("Official catalogue has no unique JSON download.")
        folders = {
            urljoin(page.url, href)
            for href, label in links.links
            if label == f"{legislature} Legislatura"
        }
        if len(folders) != 1:
            raise ParliamentImportError("Official catalogue has no unique legislature folder.")
        page = fetch_url(folders.pop(), dataset, legislature)
    raise ParliamentImportError("Official download was not discovered.")
