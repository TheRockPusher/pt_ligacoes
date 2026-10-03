"""Only the official AR open-data catalogues and their selected JSON downloads are fetched."""

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

_PAGES = "https://www.parlamento.pt/Cidadania/Paginas"
CATALOGUES = {
    "roster": f"{_PAGES}/DAInformacaoBase.aspx",
    "biography": f"{_PAGES}/DARegistoBiografico.aspx",
    "bodies": f"{_PAGES}/DAComposicaoOrgaos.aspx",
    "activity": f"{_PAGES}/DAatividadeDeputado.aspx",
    "delegations": f"{_PAGES}/DADelegacoesPermanentes.aspx",
    "friendship": f"{_PAGES}/DAGPA.aspx",
    # Committee hearings/audiences and external-body elections (Atividades<Leg>).
    "activities": f"{_PAGES}/DAatividades.aspx",
    # The historic interest registers are published inside the Registo Biográfico folders.
    "interests": f"{_PAGES}/DARegistoBiografico.aspx",
}
PREFIXES = {
    "roster": "InformacaoBase",
    "biography": "RegistoBiografico",
    "bodies": "OrgaoComposicao",
    "activity": "AtividadeDeputado",
    "delegations": "DelegacaoPermanente",
    "friendship": "GrupoDeAmizade",
    "activities": "Atividades",
    "interests": "RegistoInteresses",
}
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT = 20
TOTAL_TIMEOUT = 90
# app.parlamento.pt builds each JSON file on request: the response headers of a 4.5 MB
# Atividades file were measured at 40 to 105 s. Only that wait is longer; body reads keep
# TIMEOUT per chunk and everything stays within the dataset's total deadline.
FILE_WAIT = 240
# Downloaded files that outgrow the default bounds: OrgaoComposicao reaches ≈16 MB,
# AtividadeDeputadoXVII ≈38 MB and the legacy AtividadeDeputadoI ≈102 MB. Atividades
# files are ≈2 to 5 MB for recent legislatures; the bound leaves room for older, larger ones.
LARGE_DOWNLOADS: dict[str, tuple[int, int]] = {
    "bodies": (64 * 1024 * 1024, 300),
    "activity": (160 * 1024 * 1024, 900),
    "activities": (32 * 1024 * 1024, 300),
}
# Official file codes besides Roman numerals: the Constituent Assembly and the two
# periods of the I Legislatura. Their folders are labelled differently from the code.
FOLDER_LABELS = {"Cons": "Constituinte", "IA": "I Legislatura", "IB": "I Legislatura"}
LEGISLATURES = (
    "Cons",
    "IA",
    "IB",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
    "XII",
    "XIII",
    "XIV",
    "XV",
    "XVI",
    "XVII",
)


def canonical_legislature(code: str) -> str:
    value = code.strip().upper()
    return "Cons" if value in {"CONS", "CONSTITUINTE"} else value


def file_suffix(code: str) -> str:
    return "Constituinte" if canonical_legislature(code) == "Cons" else code


class ParliamentImportError(ValueError):
    """An incomplete, ambiguous or unsafe snapshot must not reach the database."""


@dataclass(frozen=True)
class Download:
    content: bytes
    url: str


def validate_legislature(legislature: str) -> None:
    """Only the published legislature scopes, including the constituent assembly."""
    if canonical_legislature(legislature) not in LEGISLATURES:
        raise ParliamentImportError("Unknown published legislature code.")


def validate_file_legislature(legislature: str) -> None:
    """A legislature code as used in official file names (Roman numeral, Cons, IA or IB)."""
    if legislature not in FOLDER_LABELS:
        validate_legislature(legislature)


def download_limits(dataset: str) -> tuple[int, int]:
    """Maximum bytes and total seconds for one dataset's JSON download."""
    return LARGE_DOWNLOADS.get(dataset, (MAX_BYTES, TOTAL_TIMEOUT))


def validate_url(url: str, dataset: str, legislature: str) -> None:
    """Allow exact routes/query shapes, not a suffix-based hostname allowlist."""
    validate_file_legislature(legislature)
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
    filename = f"{PREFIXES[dataset]}{file_suffix(legislature)}_json.txt"
    if (
        parsed.netloc == "app.parlamento.pt"
        and parsed.path == "/webutils/docs/doc.txt"
        and set(query) == {"path", "fich", "Inline"}
        and query["fich"][0]
        in (
            {filename, f"{PREFIXES[dataset]}Cons_json.txt"}
            if canonical_legislature(legislature) == "Cons"
            else {filename}
        )
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
    file_bytes, total_timeout = download_limits(dataset)
    deadline = time.monotonic() + total_timeout
    for _ in range(4):
        if time.monotonic() >= deadline:
            raise ParliamentImportError("Official source exceeded the time limit.")
        validate_url(url, dataset, legislature)
        parsed = urlsplit(url)
        # Only the JSON file route gets the larger per-dataset bound; HTML pages keep the default.
        max_bytes = file_bytes if parsed.netloc == "app.parlamento.pt" else MAX_BYTES
        connection = _PinnedHTTPSConnection(parsed.hostname or "", timeout=TIMEOUT)
        try:
            connection.request(
                "GET",
                parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                # The catalogue accepts the standard Python client header but returns
                # empty 403s for the previous application-specific or missing header.
                headers={
                    "User-Agent": "Python-urllib/3.13",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
            )
            if parsed.netloc == "app.parlamento.pt" and connection.sock is not None:
                # The file is generated before the first byte; wait within the deadline.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ParliamentImportError("Official source exceeded the time limit.")
                connection.sock.settimeout(min(FILE_WAIT, remaining))
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
            if size is not None and (not size.isdecimal() or int(size) > max_bytes):
                raise ParliamentImportError("Official source exceeds the size limit.")
            content = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ParliamentImportError("Official source exceeded the time limit.")
                if connection.sock is not None:
                    connection.sock.settimeout(min(TIMEOUT, remaining))
                chunk = response.read1(min(65536, max_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > max_bytes:
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
    legislature = canonical_legislature(legislature)
    validate_file_legislature(legislature)
    catalogue = CATALOGUES[dataset]
    filename = f"{PREFIXES[dataset]}{file_suffix(legislature)}_json.txt"
    filenames = (
        {filename, f"{PREFIXES[dataset]}Cons_json.txt"} if legislature == "Cons" else {filename}
    )
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
            if parse_qs(urlsplit(href).query).get("fich", [""])[0] in filenames
        }
        if len(downloads) == 1:
            return fetch_url(downloads.pop(), dataset, legislature)
        if downloads or depth:
            raise ParliamentImportError("Official catalogue has no unique JSON download.")
        folders = {
            urljoin(page.url, href)
            for href, label in links.links
            if label == FOLDER_LABELS.get(legislature, f"{legislature} Legislatura")
        }
        if len(folders) != 1:
            raise ParliamentImportError("Official catalogue has no unique legislature folder.")
        page = fetch_url(folders.pop(), dataset, legislature)
    raise ParliamentImportError("Official download was not discovered.")
