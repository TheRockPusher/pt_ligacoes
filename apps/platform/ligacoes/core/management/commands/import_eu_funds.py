from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.eu_funds import PROGRAMMES, EuFundsError, run


class Command(BaseCommand):
    help = (
        "Valida as operações de fundos europeus (Portugal 2020, Portugal 2030, PRR) publicadas "
        "no dados.gov.pt; --apply grava-as como eventos sobre as entidades beneficiárias, "
        "intermediárias e fornecedoras com NIPC. Cada programa é gravado atomicamente. "
        "Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--programme", choices=[*PROGRAMMES, "all"], default="all", help="Programa a importar."
        )
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar atomicamente cada programa; publica os eventos ancorados.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        keys = list(PROGRAMMES) if options["programme"] == "all" else [options["programme"]]
        for key in keys:
            programme = PROGRAMMES[key]
            try:
                result = run(programme, apply=options["apply"], as_of=options["as_of"])
            except EuFundsError as exc:
                raise CommandError(f"{programme.label}: {exc}") from exc
            except (ValidationError, DatabaseError) as exc:
                # Never emit names, payloads, identifiers or database diagnostics.
                raise CommandError(
                    f"{programme.label}: falha de validação ou gravação; nenhuma alteração "
                    "parcial deste programa foi aplicada."
                ) from exc
            summary = (
                f"{programme.label}: {result.get('operations', 0)} operações; "
                f"{result.get('events', 0)} eventos com entidade coletiva; "
                f"{result.get('skipped_without_legal_party', 0)} operações sem entidade coletiva; "
                f"{result.get('parties_dropped', 0)} entradas de pessoas singulares, "
                "pseudonimizadas ou sem NIPC válido descartadas; "
                f"{result.get('titles_withheld', 0)} títulos substituídos pelo código; "
                f"{result.get('orphan_party_rows', 0)} entidades sem operação; "
                f"{result.get('duplicates', 0)} operações repetidas; "
                f"{result.get('invalid_amounts', 0)} montantes e "
                f"{result.get('invalid_dates', 0)} datas inválidos; "
                f"{result.get('end_dates_dropped', 0)} datas de fim anteriores ao início omitidas"
            )
            if not options["apply"]:
                self.stdout.write(
                    f"Simulação: {summary}; {result.get('organisations', 0)} organizações; "
                    f"data={options['as_of']}. Sem escritas."
                )
                continue
            self.stdout.write(
                f"Aplicação: {summary}; organizações criadas="
                f"{result.get('organisations_created', 0)}; novos={result.get('created', 0)}; "
                f"alterados={result.get('changed', 0)}; inalterados={result.get('unchanged', 0)}; "
                f"cessados={result.get('ceased', 0)}; publicados={result.get('published', 0)}; "
                f"rascunhos={result.get('draft', 0)}."
            )
