from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.igf_subsidies import (
    CATEGORY_LABELS,
    Client,
    IgfError,
    NotPublished,
    apply_file,
    scope,
    summarise,
    years,
)


class Command(BaseCommand):
    help = (
        "Valida as listas IGF de subvenções, benefícios públicos e doações (Lei n.º 64/2013); "
        "--apply grava os apoios a pessoas coletivas como eventos (um âmbito completo por "
        "ficheiro). Pessoas singulares são excluídas. Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--year", required=True, help="Ano (2020 em diante) ou «all».")
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar atomicamente cada ficheiro; publica apoios entre NIPC identificados.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        as_of: date = options["as_of"]
        try:
            selected = years(options["year"], as_of=as_of)
            client = Client()
            for year in selected:
                try:
                    resources = client.resources(year)
                except NotPublished:
                    if options["year"] == "all" and year == selected[-1]:
                        self.stdout.write(f"IGF {year}: ainda não publicado.")
                        continue
                    raise
                for resource in resources:
                    content = client.content(resource)
                    category = CATEGORY_LABELS[resource.category]
                    label = f"IGF {scope(year, resource.category)} ({category})"
                    if not options["apply"]:
                        s = summarise(content)
                        self.stdout.write(
                            f"{label}: linhas={s['rows']}; pessoas coletivas={s['kept']}; "
                            f"pessoas singulares excluídas={s['dropped_persons']}; concedente sem "
                            f"NIPC={s['grantor_unanchored']}; montante inválido="
                            f"{s['invalid_amount']}; sem data={s['no_date']}; organizações="
                            f"{s['organisations']}."
                        )
                        continue
                    r = apply_file(content, year=year, category=resource.category, as_of=as_of)
                    del content
                    self.stdout.write(
                        f"{label}: pessoas coletivas={r['kept']}; pessoas singulares excluídas="
                        f"{r['dropped_persons']}; novos={r['created']}; alterados={r['changed']}; "
                        f"inalterados={r['unchanged']}; ausentes={r['ceased']}; publicados="
                        f"{r['published']}; rascunho={r['draft']}."
                    )
        except IgfError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; o ficheiro em curso não foi aplicado."
            ) from exc
        if not options["apply"]:
            self.stdout.write("Simulação: sem escritas.")
