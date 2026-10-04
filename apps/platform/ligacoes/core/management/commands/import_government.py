from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.government import (
    GOVERNMENTS,
    GovernmentImportError,
    apply_snapshot,
    fetch_snapshot,
)


class Command(BaseCommand):
    help = (
        "Valida a composição oficial de um Governo (todos os mandatos iniciados até à data); "
        "--apply grava e publica os cargos oficiais e as pastas."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--government",
            choices=GOVERNMENTS,
            default="gc25",
            help="Governo oficial, de gc21 a gc25 (ex.: gc25).",
        )
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply", action="store_true", help="Gravar e publicar cargos oficiais atomicamente."
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(government=options["government"], as_of=options["as_of"])
            portfolios = len({member.portfolio_id for member in snapshot.members})
            if not options["apply"]:
                self.stdout.write(
                    f"Validação: {len(snapshot.members)} mandatos; {portfolios} pastas; "
                    f"Governo={snapshot.government}; data={snapshot.as_of}. "
                    "Sem alterações na base de dados."
                )
                return
            result = apply_snapshot(snapshot)
        except (GovernmentImportError, ValidationError, DatabaseError) as exc:
            if isinstance(exc, GovernmentImportError):
                message = str(exc)
            else:
                message = "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            raise CommandError(message) from exc
        self.stdout.write(
            f"Aplicação: {len(snapshot.members)} mandatos; "
            f"novos={result['created']}; alterados={result['changed']}; "
            f"cessados={result['ceased']}; publicados={result['published']}. "
            f"Pastas: {portfolios}; novas={result['structure_created']}; "
            f"cessadas={result['structure_ceased']}; "
            f"publicadas={result['structure_published']}."
        )
