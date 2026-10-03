from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from ligacoes.core.government_archive import (
    GOVERNMENTS,
    GovernmentArchiveError,
    apply_snapshot,
    fetch_snapshot,
)


class Command(BaseCommand):
    help = (
        "Valida todas as composições históricas dos Governos Constitucionais I a XX; "
        "--apply grava e publica os cargos documentados."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--government", choices=GOVERNMENTS, help="Limitar a um Governo (gc01 a gc20)."
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true", help="Gravar atomicamente por Governo.")
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        for government in (options["government"],) if options["government"] else GOVERNMENTS:
            try:
                snapshot = fetch_snapshot(government=government)
                offices = len({member.key for member in snapshot.members})
                prime_ministers = sorted(
                    {
                        member.name
                        for member in snapshot.members
                        if member.role == "Primeiro-Ministro"
                    }
                )
                description = (
                    f"Governo={government}; {len(snapshot.pages) - 1} composições datadas; "
                    f"{offices} cargos; Primeiro-Ministro: {', '.join(prime_ministers)}."
                )
                if not options["apply"]:
                    self.stdout.write(f"Validação: {description} Sem alterações na base de dados.")
                    continue
                result = apply_snapshot(snapshot)
                self.stdout.write(
                    f"Aplicação: {description} Novos={result['created']}; "
                    f"alterados={result['changed']}; cessados={result['ceased']}; "
                    f"publicados={result['published']}."
                )
            except (GovernmentArchiveError, ValidationError, DatabaseError) as exc:
                message = (
                    str(exc)
                    if isinstance(exc, GovernmentArchiveError)
                    else (
                        "Falha de validação ou gravação; nenhuma alteração parcial deste Governo foi aplicada."
                    )
                )
                raise CommandError(message) from exc
