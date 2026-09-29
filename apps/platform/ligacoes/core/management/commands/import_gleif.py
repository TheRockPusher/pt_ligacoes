from argparse import ArgumentTypeError
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.gleif import GleifError, apply_snapshot, fetch_snapshot


def positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("tem de ser um inteiro positivo") from exc
    if number < 1:
        raise ArgumentTypeError("tem de ser um inteiro positivo")
    return number


class Command(BaseCommand):
    help = (
        "Valida os registos LEI (GLEIF) de entidades do Registo Comercial português; --apply "
        "grava identificadores LEI/NIPC e empresas-mãe de consolidação. Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        parser.add_argument(
            "--limit",
            type=positive,
            default=None,
            help="Ler só os primeiros N registos; não cessa relações de registos não lidos.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar atomicamente; publica as relações de consolidação ancoradas.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(as_of=options["as_of"], limit=options["limit"])
            summary = (
                f"{len(snapshot.records)} entidades com NIPC de pessoa coletiva; "
                f"{snapshot.skipped} registos excluídos (pessoas singulares, empresários em nome "
                f"individual, NIPC inválido, registos duplicados ou anulados); "
                f"{len(snapshot.links)} relações de consolidação; "
                f"{len(snapshot.parents)} empresas-mãe fora do conjunto português; "
                f"{snapshot.unresolved_links} relações sem registo LEI utilizável"
            )
            if not options["apply"]:
                self.stdout.write(
                    f"Simulação: {summary}; data={snapshot.as_of}"
                    f"{'' if snapshot.complete else '; recolha parcial'}. Sem escritas."
                )
                return
            result = apply_snapshot(snapshot)
        except GleifError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            ) from exc
        self.stdout.write(
            f"Aplicação: {summary}; organizações criadas={result.get('organisations_created', 0)}; "
            f"LEI acrescentados a entidades existentes={result.get('lei_added', 0)}; "
            f"NIPC acrescentados={result.get('nipc_added', 0)}; "
            f"conflitos LEI/NIPC por rever={result.get('conflicts', 0)}; "
            f"empresas-mãe criadas={result.get('parents_created', 0)}; "
            f"novos={result.get('created', 0)}; alterados={result.get('changed', 0)}; "
            f"cessados={result.get('ceased', 0)}; publicados={result.get('published', 0)}."
        )
