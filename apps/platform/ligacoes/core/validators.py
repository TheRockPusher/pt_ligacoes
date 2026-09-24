import ipaddress
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
