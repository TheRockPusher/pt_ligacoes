from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.government import GovernmentImportError, apply_snapshot, fetch_snapshot


class Command(BaseCommand):
    help = "Valida a composição oficial do Governo; só --apply grava rascunhos privados."
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--government", default="gc25", help="Governo oficial, por exemplo gc25."
        )
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply", action="store_true", help="Gravar rascunhos privados atomicamente."
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(government=options["government"], as_of=options["as_of"])
            if not options["apply"]:
                self.stdout.write(
                    f"Validação: {len(snapshot.members)} mandatos; Governo={snapshot.government}; "
                    f"data={snapshot.as_of}. Sem alterações na base de dados."
                )
                return
            result = apply_snapshot(snapshot)
        except (GovernmentImportError, PermissionDenied, ValidationError, DatabaseError) as exc:
            if isinstance(exc, GovernmentImportError):
                message = str(exc)
            elif isinstance(exc, PermissionDenied):
                message = "Recolha bloqueada: falta aprovação ativa para esta fonte e finalidade."
            else:
                message = "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            raise CommandError(message) from exc
        self.stdout.write(
            f"Aplicação: {len(snapshot.members)} mandatos; "
            f"novos={result['created']}; alterados={result['changed']}; "
            f"cessados={result['ceased']}; rascunhos={result['drafts']}. Sem publicação automática."
        )
