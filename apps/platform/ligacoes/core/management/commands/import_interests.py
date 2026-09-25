from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from ligacoes.core.interests import InterestsImportError, apply_snapshot, fetch_snapshot


class Command(BaseCommand):
    help = (
        "Valida os interesses públicos EpT de um titular com correspondência revista. "
        "Exige autorização da fonte; simulação por omissão, sem publicação."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--holder-id",
            required=True,
            help="Identificador EpT com correspondência editorial revista para uma pessoa local.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Guarda candidatos privados para revisão; não resolve organizações nem publica.",
        )
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Valida o âmbito completo do titular sem escrever na base de dados (omissão).",
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(holder_id=options["holder_id"])
            if not options["apply"]:
                self.stdout.write(
                    f"Simulação: {snapshot.declaration_count} declarações no âmbito do titular; "
                    f"{len(snapshot.observations)} candidatos públicos selecionados; "
                    f"{snapshot.restricted_sections} secções indisponíveis. "
                    "Sem escritas. A ausência de candidatos não significa ausência de interesses."
                )
                return
            result = apply_snapshot(snapshot)
        except PermissionDenied as exc:
            raise CommandError(
                "A consulta EpT exige autorização registada, ativa e válida para interesses declarados."
            ) from exc
        except InterestsImportError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit holder names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Importação não aplicada: confirme a correspondência de titular revista e "
                "as condições de validação editorial."
            ) from exc
        self.stdout.write(
            f"Candidatos privados: novos={result['created']}, alterados={result['changed']}, "
            f"retirados={result['ceased']}. Sem publicação automática; "
            "a organização, o tipo e as datas exigem revisão editorial."
        )
