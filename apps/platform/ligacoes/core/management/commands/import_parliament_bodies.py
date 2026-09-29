from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.parliament_bodies import apply_snapshot, fetch_snapshot
from ligacoes.core.parliament_fetch import ParliamentImportError


class Command(BaseCommand):
    help = (
        "Validate AR organ, parliamentary-group, delegation and friendship-group composition "
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
            "--apply", action="store_true", help="Atomically save and auto-publish anchored claims."
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validate without database writes (default)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(legislature=options["legislature"], as_of=options["as_of"])
            summary = (
                f"legislature={snapshot.legislature.code}, as_of={snapshot.as_of}, "
                f"organs={len(snapshot.organs)}, claims={len(snapshot.claims)}, "
                f"plenary_mandates={len(snapshot.mandates)}, "
                f"skipped_intervals={snapshot.skipped_intervals}"
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
        self.stdout.write(
            f"Applied: {summary}; applied_claims={result['claims']}, "
            f"plenary_mandates_left_to_roster={result['mandates_skipped']}, "
            f"created={result['created']}, changed={result['changed']}, "
            f"ceased={result['ceased']}, published={result['published']}."
        )
