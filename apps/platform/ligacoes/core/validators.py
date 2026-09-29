import ipaddress
import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


def validate_source_url(value):
    """Validate link destinations only: no DNS lookup or server-side fetching."""
    URLValidator(schemes=["http", "https"])(value)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Endereço de fonte inválido.") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or not host
        or "\\" in value
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or host == "localhost"
        or host.endswith((".localhost", ".localdomain", ".local", ".internal", ".test", ".invalid"))
        or (port is not None and port not in (80, 443))
    ):
        raise ValidationError("A fonte deve ser uma ligação pública HTTP(S), sem credenciais.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValidationError("A fonte deve ter um domínio público.") from None
    else:
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise ValidationError("Não são permitidos endereços de rede privada ou reservada.")


NIPC_LEGAL_PREFIXES = ("5", "6", "71", "72", "77", "79", "90", "91", "98", "99")


def valid_nipc(value: str) -> bool:
    """A legal-person NIPC; natural-person, estate and sole-trader prefixes are refused."""
    if not re.fullmatch(r"[0-9]{9}", value) or not value.startswith(NIPC_LEGAL_PREFIXES):
        return False
    total = sum(
        int(digit) * weight for digit, weight in zip(value[:8], range(9, 1, -1), strict=True)
    )
    check = 11 - total % 11
    return int(value[8]) == (0 if check >= 10 else check)
