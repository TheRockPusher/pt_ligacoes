from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from ligacoes.core.ept_offices import EptOfficesError
from ligacoes.core.interests import (
    InterestsImportError,
    apply_snapshot,
    fetch_holder_listing,
    fetch_snapshot,
)


class Command(BaseCommand):
    help = "Importa os interesses públicos EpT de todos os titulares ou de um titular; simulação por omissão."
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--holder-id", help="Identificador EpT para diagnóstico de um titular.")
        scope.add_argument(
            "--all", action="store_true", help="Todos os titulares da lista pública."
        )
        parser.add_argument(
            "--limit", type=int, help="Máximo de titulares, por identificador crescente."
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Guarda e publica os interesses públicos verificáveis com evidência.",
        )
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Valida o âmbito completo do titular sem escrever na base de dados (omissão).",
        )

    def handle(self, *args, **options) -> None:
        if options["limit"] is not None and (options["limit"] < 1 or not options["all"]):
            raise CommandError("--limit exige --all e um número positivo.")
        try:
            holders = fetch_holder_listing()
        except (InterestsImportError, EptOfficesError) as exc:
            raise CommandError(str(exc)) from exc
        if options["holder_id"]:
            identifier = str(options["holder_id"])
            if identifier not in holders:
                raise CommandError("O titular não consta da lista pública completa EpT.")
            identifiers = [identifier]
        else:
            identifiers = sorted(holders, key=int)
            if options["limit"] is not None:
                identifiers = identifiers[: options["limit"]]
        failures = 0
        completed = 0
        for identifier in identifiers:
            name, rows = holders[identifier]
            try:
                snapshot = fetch_snapshot(
                    holder_id=identifier,
                    holder_name=name,
                    office_rows=rows,
                )
                if options["apply"]:
                    result = apply_snapshot(snapshot)
                    self.stdout.write(
                        f"Titular {identifier}: novos={result['created']}, "
                        f"alterados={result['changed']}, retirados={result['ceased']}."
                    )
                else:
                    self.stdout.write(
                        f"Simulação; titular {identifier}: {snapshot.declaration_count} declarações; "
                        f"{len(snapshot.observations)} observações; "
                        f"{snapshot.restricted_sections} secções indisponíveis. Sem escritas."
                    )
                completed += 1
            except (InterestsImportError, EptOfficesError, ValidationError, DatabaseError) as exc:
                failures += 1
                message = (
                    f"Titular {identifier}: âmbito não aplicado; consulta ou validação falhou."
                )
                if not options["all"]:
                    raise CommandError(message) from exc
                # No source passage, holder name or database diagnostic is logged.
                self.stderr.write(message)
        self.stdout.write(f"Titulares concluídos={completed}; falhas={failures}.")
        if failures:
            raise CommandError("Importação parcial: os âmbitos com falha não foram aplicados.")
