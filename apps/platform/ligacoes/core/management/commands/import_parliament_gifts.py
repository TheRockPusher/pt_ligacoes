from collections import Counter
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.parliament_gifts import (
    LEGISLATURES,
    GiftsImportError,
    apply_snapshot,
    fetch_snapshot,
)


class Command(BaseCommand):
    help = (
        "Validate the AR register of gifts, travel and hospitality for one legislature; "
        "dry-run unless --apply is supplied."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--legislature", required=True, choices=LEGISLATURES)
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Atomically save the register; events with an unresolved provider stay drafts.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validate without database writes (default)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(legislature=options["legislature"], as_of=options["as_of"])
            kinds = Counter(entry.kind for page in snapshot.pages for entry in page.entries)
            summary = (
                f"legislature={snapshot.legislature}, as_of={snapshot.as_of}, "
                f"deputies={len(snapshot.pages)}, "
                f"with_entries={sum(1 for page in snapshot.pages if page.entries)}, "
                + ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
            )
            if not options["apply"]:
                self.stdout.write(f"Dry run: {summary}. No database writes.")
                return
            result = apply_snapshot(snapshot)
        except (GiftsImportError, ValidationError, DatabaseError) as exc:
            # Do not log payloads, names or database credentials.
            if isinstance(exc, GiftsImportError):
                message = str(exc)
            else:
                message = "Import validation/database failure; the snapshot was rolled back."
            raise CommandError(message) from exc
        counts = ", ".join(f"{key}={value}" for key, value in sorted(result.items()))
        self.stdout.write(f"Applied: {summary}; {counts}.")
