from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.government import GOVERNMENTS, GovernmentImportError
from ligacoes.core.government_nominations import apply_snapshot, fetch_snapshot


class Command(BaseCommand):
    help = (
        "Valida as nomeações dos gabinetes de um Governo (chefes de gabinete, adjuntos e "
        "técnicos especialistas); --apply publica os gabinetes e as nomeações verificáveis. "
        "Importe primeiro a composição do mesmo Governo para ligar gabinetes às pastas."
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
            "--apply", action="store_true", help="Gravar e publicar nomeações atomicamente."
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(government=options["government"], as_of=options["as_of"])
            rows = sum(len(page.rows) for page in snapshot.pages)
            dropped = sum(page.dropped for page in snapshot.pages)
            incomplete = sum(page.incomplete for page in snapshot.pages)
            outside = sum(row.outside_term for page in snapshot.pages for row in page.rows)
            summary = (
                f"{len(snapshot.pages)} páginas; {rows} nomeações retidas; "
                f"{dropped} de outras funções ignoradas; {incomplete} sem nome utilizável; "
                f"{outside} com data fora da vigência"
            )
            if not options["apply"]:
                self.stdout.write(
                    f"Validação: {summary}; Governo={snapshot.government}; "
                    f"data={snapshot.as_of}. Sem alterações na base de dados."
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
            f"Aplicação: {summary}. Nomeações: novas={result['created']}; "
            f"alteradas={result['changed']}; cessadas={result['ceased']}; "
            f"publicadas={result['published']}. "
            f"Gabinetes: {result['gabinetes']}; ligados a pastas={result['linked']}; "
            f"ambíguos={result['ambiguous']}; sem pasta={result['unmatched']}; "
            f"ligações publicadas={result['structure_published']}."
        )
