from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.parliament_activities import apply_snapshot, fetch_snapshot
from ligacoes.core.parliament_fetch import ParliamentImportError


class Command(BaseCommand):
    help = (
        "Validate AR committee hearings/audiences and external-body elections (Atividades) "
        "for one legislature; dry-run unless --apply is supplied."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--legislature", required=True, help="Official file code, e.g. XVI, IA, IB or Cons."
        )
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Atomically save hearing events and publish verifiable external-body elections.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validate without database writes (default)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(legislature=options["legislature"], as_of=options["as_of"])
            summary = (
                f"legislature={snapshot.legislature}, as_of={snapshot.as_of}, "
                f"hearings={len(snapshot.hearings)}, committees={len(snapshot.committees)}, "
                f"unmapped_committees={len(snapshot.unmapped_committees)}, "
                f"elected={len(snapshot.elections)}, "
                f"skipped_placeholders={snapshot.skipped_placeholders}, "
                f"skipped_not_held={snapshot.skipped_not_held}, "
                f"skipped_resignations={snapshot.skipped_resignations}, "
                f"skipped_without_parties={snapshot.skipped_without_parties}, "
                f"invalid_dates={snapshot.invalid_dates}"
            )
            if not options["apply"]:
                self.stdout.write(f"Dry run: {summary}. No database writes.")
                return
            result = apply_snapshot(snapshot)
        except (ParliamentImportError, ValidationError, DatabaseError) as exc:
            # Do not log payloads, names, opaque download paths or database credentials.
            if isinstance(exc, ParliamentImportError):
                message = str(exc)
            else:
                message = "Import validation/database failure; the snapshot was rolled back."
            raise CommandError(message) from exc
        counts = ", ".join(f"{key}={value}" for key, value in sorted(result.items()))
        self.stdout.write(f"Applied: {summary}; {counts}.")
