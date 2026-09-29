from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from ligacoes.core.wikidata import (
    WikidataError,
    apply_snapshot,
    fetch_snapshot,
    plan_snapshot,
    summary,
)


class Command(BaseCommand):
    help = (
        "Consulta o Wikidata (fonte secundária) e cruza QID com identificadores oficiais já "
        "ligados; --apply grava pistas QID não revistas e sugestões de identidade pendentes. "
        "Nunca cria entidades nem afirmações. Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar atomicamente pistas QID e sugestões de identidade pendentes.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot()
            if options["apply"]:
                counts = apply_snapshot(snapshot)
                mode, trailer = "Aplicação", "Nenhuma entidade ou afirmação criada."
            else:
                counts = summary(snapshot, plan_snapshot(snapshot))
                mode, trailer = "Simulação", "Sem escritas."
        except WikidataError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit identifiers, payloads or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            ) from exc
        self.stdout.write(
            f"{mode}: {counts['rows']} valores em {counts['items']} itens Wikidata; "
            f"descartados={counts['dropped']}; pistas QID novas={counts['hints']}; "
            f"inalteradas={counts['unchanged']}; conflitos={counts['conflicts']}; "
            f"sem identificador oficial ligado={counts['unmatched']}; sugestões de identidade "
            f"novas={counts['suggestions_new']}; já pendentes={counts['suggestions_existing']}. "
            f"{trailer}"
        )
