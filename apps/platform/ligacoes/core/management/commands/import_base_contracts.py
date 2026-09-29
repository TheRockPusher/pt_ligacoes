import gc
from argparse import ArgumentTypeError
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.base_contracts import (
    FIRST_YEAR,
    BaseContractsError,
    YearScan,
    apply_year,
    fetch_archive,
    fetch_resources,
    known_organisations,
    scan,
)


def year_option(value: str) -> int | None:
    """A contract year, or ``all`` (None) for every year file published."""
    if value == "all":
        return None
    try:
        year = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("tem de ser um ano (AAAA) ou 'all'") from exc
    if year < FIRST_YEAR or year > 2100:
        raise ArgumentTypeError(f"o Portal BASE publica contratos desde {FIRST_YEAR}")
    return year


def _summary(year_scan: YearScan) -> str:
    counts = year_scan.counts
    return (
        f"{year_scan.year}: {counts['rows']} linhas; {counts['contracts']} contratos únicos; "
        f"{counts['duplicates']} repetições idênticas; {counts['conflicts']} repetições "
        f"divergentes (mantida a primeira); {counts['invalid']} registos inválidos; "
        f"{counts['skipped']} contratos sem pessoa coletiva identificada; "
        f"{counts['dropped_parties']} intervenientes excluídos (pessoas singulares, "
        f"estrangeiros ou NIF inválido); adjudicantes={counts['parties_buyer']}; "
        f"adjudicatários={counts['parties_supplier']}; concorrentes={counts['parties_bidder']}; "
        f"sem preço contratual={counts['without_amount']}; "
        f"organizações (NIPC)={counts['organisations']}"
    )


class Command(BaseCommand):
    help = (
        "Valida os contratos públicos do Portal BASE (IMPIC, dados.gov.pt), um ficheiro anual "
        "de cada vez; --apply grava os contratos como eventos entre organizações identificadas "
        "por NIPC. Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--year",
            type=year_option,
            required=True,
            help="Ano do ficheiro (AAAA) ou 'all' para todos os anos publicados.",
        )
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar cada ano atomicamente; cessa contratos ausentes do ficheiro do ano.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            resources = fetch_resources()
            wanted: int | None = options["year"]
            if wanted is not None and wanted not in resources:
                raise CommandError(f"O ficheiro de {wanted} não está publicado no dados.gov.pt.")
            years = sorted(resources) if wanted is None else [wanted]
            for year in years:
                # One file at a time: the archive and scan are released before the next.
                archive = fetch_archive(resources[year])
                year_scan = scan(archive, year)
                summary = _summary(year_scan)
                if not options["apply"]:
                    known = known_organisations(list(year_scan.organisations))
                    self.stdout.write(
                        f"Simulação {summary}; já identificadas={known}; "
                        f"data={options['as_of']}. Sem escritas."
                    )
                else:
                    result = apply_year(archive, year_scan, as_of=options["as_of"])
                    self.stdout.write(
                        f"Aplicação {summary}; organizações criadas="
                        f"{result['organisations_created']}; novos={result['created']}; "
                        f"alterados={result['changed']}; inalterados={result['unchanged']}; "
                        f"cessados={result['ceased']}; publicados={result['published']}; "
                        f"rascunhos={result['draft']}."
                    )
                del archive, year_scan
                gc.collect()
        except BaseContractsError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; o ano em curso não foi aplicado."
            ) from exc
