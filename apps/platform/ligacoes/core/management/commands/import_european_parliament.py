from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.european_parliament import (
    EuropeanParliamentError,
    apply_snapshot,
    fetch_snapshot,
)


class Command(BaseCommand):
    help = (
        "Valida os deputados portugueses ao Parlamento Europeu (API de dados abertos do PE, "
        "todas as legislaturas); --apply grava mandatos, grupos políticos, comissões e "
        "delegações. Filiação partidária nacional nunca é lida. Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar atomicamente; publica as relações de deputados identificados.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(as_of=options["as_of"])
            counts = snapshot.counts()
            skipped = snapshot.skipped
            summary = (
                f"{counts['meps']} deputados/as; {counts['terms']} legislaturas; "
                f"{counts['bodies']} órgãos; {counts['mandates']} mandatos; "
                f"{counts['groups']} pertenças a grupos políticos; "
                f"{counts['committees']} a comissões; {counts['delegations']} a delegações; "
                f"{skipped.get('national_party', 0)} pertenças a partidos nacionais excluídas; "
                f"{skipped.get('other_body', 0) + skipped.get('no_organisation', 0)} funções "
                f"noutros órgãos não importadas; "
                f"{skipped.get('invalid_dates', 0)} pertenças com datas inválidas excluídas"
            )
            if not options["apply"]:
                self.stdout.write(f"Simulação: {summary}; data={snapshot.as_of}. Sem escritas.")
                return
            result = apply_snapshot(snapshot)
        except EuropeanParliamentError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            ) from exc
        self.stdout.write(
            f"Aplicação: {summary}; pessoas criadas={result.get('persons_created', 0)}; "
            f"a aguardar revisão de identidade={result.get('pending_review', 0)}; "
            f"órgãos criados={result.get('bodies_created', 0)}; "
            f"novos={result.get('created', 0)}; alterados={result.get('changed', 0)}; "
            f"cessados={result.get('ceased', 0)}; publicados={result.get('published', 0)}."
        )
