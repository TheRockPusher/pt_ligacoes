from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.ept_offices import EptOfficesError, apply_snapshot, fetch_snapshot


class Command(BaseCommand):
    help = (
        "Valida a lista pública de titulares da EpT; --apply grava cargos ligados por "
        "identificadores oficiais. Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar atomicamente; publica os cargos de titulares e entidades identificados.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(as_of=options["as_of"])
            if not options["apply"]:
                self.stdout.write(
                    f"Simulação: {snapshot.total} registos; {len(snapshot.offices)} cargos; "
                    f"{len(snapshot.crosswalk)} registos da AR/Governo só para correspondência; "
                    f"{snapshot.skipped} excluídos (órgãos partidários e candidaturas); "
                    f"data={snapshot.as_of}. Sem escritas."
                )
                return
            result = apply_snapshot(snapshot)
        except EptOfficesError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit holder names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            ) from exc
        self.stdout.write(
            f"Aplicação: {len(snapshot.offices)} cargos; novos={result['created']}; "
            f"alterados={result['changed']}; cessados={result['ceased']}; "
            f"publicados={result['published']}; "
            f"{len(snapshot.holders)} titulares na listagem completa."
        )
