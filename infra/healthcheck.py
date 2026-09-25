"""Probe web readiness or private worker liveness without exposing failure details."""

import http.client
import json
import os
import re
from pathlib import Path


def worker_is_alive() -> bool:
    try:
        command = Path("/proc/1/cmdline").read_bytes().split(b"\0")
        if len(command) < 3:
            return False
        os.kill(1, 0)
        return (
            re.fullmatch(rb"python(?:3(?:\.[0-9]+)?)?", command[0].rsplit(b"/", 1)[-1]) is not None
            and command[1] in {b"apps/platform/manage.py", b"/app/apps/platform/manage.py"}
            and command[2] == b"run_import_worker"
        )
    except OSError:
        return False


def main() -> None:
    if os.environ.get("APP_PROCESS") == "import-worker":
        if not worker_is_alive():
            raise SystemExit(1)
        return
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
