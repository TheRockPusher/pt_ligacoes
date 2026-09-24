"""Probe the real readiness route without exposing failure details or following redirects."""

import http.client
import json
import os


def main() -> None:
    host = os.environ["ALLOWED_HOSTS"].split(",", 1)[0].strip()
    connection = http.client.HTTPConnection(
        "127.0.0.1", int(os.environ.get("PORT", "8000")), timeout=4
    )
    try:
        connection.request("GET", "/healthz/", headers={"Host": host, "X-Forwarded-Proto": "https"})
        response = connection.getresponse()
        if response.status != 200 or not isinstance(json.loads(response.read(1024)), dict):
            raise SystemExit(1)
    except (OSError, ValueError, http.client.HTTPException):
        raise SystemExit(1) from None
    finally:
        connection.close()


if __name__ == "__main__":
    main()
