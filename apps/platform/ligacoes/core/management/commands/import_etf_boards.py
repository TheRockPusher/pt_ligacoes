from argparse import ArgumentTypeError
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.etf_boards import EtfError, apply_snapshot, fetch_snapshot, summarise


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
        "Lê os documentos «Modelo de governo/Membros dos órgãos sociais» das empresas públicas "
        "publicados pela ETF; --apply publica os cargos verificáveis com evidência. "
        "Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        parser.add_argument(
            "--limit",
            type=positive,
            default=None,
            help="Ler só as primeiras N empresas da lista; não cessa empresas não lidas.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar e publicar atomicamente os cargos verificáveis.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(as_of=options["as_of"], limit=options["limit"])
            counts = summarise(snapshot)
            summary = (
                f"{counts['companies']} de {counts['listed']} empresas lidas; "
                f"{counts['documents']} documentos de governo; "
                f"{counts['rejected_links']} ligações fora das rotas autorizadas; "
                f"{counts['unreadable']} documentos ilegíveis; "
                f"{counts['without_table']} documentos sem tabela legível; "
                f"{counts['tables']} tabelas de órgãos ({counts['unparseable']} ignoradas por "
                f"não serem legíveis); {counts['members']} membros "
                f"({counts['ended']} com mandato terminado); "
                f"{counts['firms']} sociedades de revisores excluídas; "
                f"{counts['vacant']} lugares vagos; {counts['duplicates']} linhas repetidas"
            )
            if not options["apply"]:
                self.stdout.write(
                    f"Simulação: {summary}; data={snapshot.as_of}"
                    f"{'' if snapshot.complete else '; recolha parcial'}. Sem escritas."
                )
                return
            result = apply_snapshot(snapshot)
        except EtfError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit names, payloads or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            ) from exc
        self.stdout.write(
            f"Aplicação: {summary}; data={snapshot.as_of}"
            f"{'' if snapshot.complete else '; recolha parcial (nada cessado)'}; "
            f"observações novas={result.get('created', 0)}; "
            f"alterados={result.get('changed', 0)}; cessados={result.get('ceased', 0)}."
        )
