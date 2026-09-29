from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.parliament_fetch import ParliamentImportError
from ligacoes.core.parliament_interests import apply_snapshot, fetch_snapshot


class Command(BaseCommand):
    help = (
        "Valida o registo de interesses histórico da Assembleia da República para uma "
        "legislatura. Simulação por omissão; --apply guarda só candidatos privados."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--legislature",
            required=True,
            help="Código da legislatura no ficheiro oficial (por exemplo XV, IA, IB ou Cons).",
        )
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Guarda candidatos privados para revisão; não cria organizações nem publica.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Valida sem escrever (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(legislature=options["legislature"], as_of=options["as_of"])
            excluded = ", ".join(f"{reason}={count}" for reason, count in snapshot.excluded)
            summary = (
                f"legislatura={snapshot.legislature}; deputados com declarações="
                f"{len(snapshot.declarants)}; linhas retidas={snapshot.row_count}; "
                f"linhas excluídas: {excluded or 'nenhuma'}"
            )
            if not options["apply"]:
                self.stdout.write(f"Simulação: {summary}. Sem escritas.")
                return
            result = apply_snapshot(snapshot)
        except (ParliamentImportError, ValidationError, DatabaseError) as exc:
            # Never emit declarant names, payloads, opaque download paths or database details.
            if isinstance(exc, ParliamentImportError):
                message = str(exc)
            else:
                message = "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            raise CommandError(message) from exc
        self.stdout.write(
            f"Aplicação: {summary}. Candidatos privados: novos={result['created']}, "
            f"alterados={result['changed']}, retirados={result['ceased']}. "
            "Sem publicação automática; organização, tipo e datas exigem revisão editorial."
        )
