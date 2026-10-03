from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.parliament_fetch import (
    LEGISLATURES,
    ParliamentImportError,
    canonical_legislature,
)
from ligacoes.core.parliament_import import apply_snapshot, fetch_snapshot


class Command(BaseCommand):
    help = "Import complete official AR legislature histories; dry-run unless --apply is supplied."
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--legislature", default="XVII", type=canonical_legislature, choices=LEGISLATURES
        )
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply", action="store_true", help="Atomically publish evidenced mandates."
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validate without database writes (default)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(
                legislature=options["legislature"],
                as_of=options["as_of"],
            )
            if not options["apply"]:
                self.stdout.write(
                    f"Dry run: people={len(snapshot.members)}, mandates={snapshot.mandate_count}; "
                    f"legislature={snapshot.legislature}, as_of={snapshot.as_of}. No database writes."
                )
                return
            result = apply_snapshot(snapshot)
        except (ParliamentImportError, ValidationError, DatabaseError) as exc:
            # Do not log payloads, names, opaque download paths or database credentials.
            if isinstance(exc, ParliamentImportError):
                message = str(exc)
            else:
                message = (
                    f"Import validation/database failure ({type(exc).__name__}); "
                    "the snapshot was rolled back."
                )
            raise CommandError(message) from exc
        self.stdout.write(
            f"Applied: mandates={result.serving}, new_members={result.created_members}, "
            f"new_source_revisions={result.created_records}, ceased={result.ceased_members}. "
            "Official mandates are published automatically."
        )
