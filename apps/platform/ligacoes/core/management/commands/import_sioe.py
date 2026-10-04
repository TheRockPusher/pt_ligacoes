from argparse import ArgumentTypeError
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.sioe import SioeError, apply_snapshot, fetch_snapshot, summarise

PROGRESS_EVERY = 500


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("indique um número inteiro positivo") from exc
    if number < 1:
        raise ArgumentTypeError("indique um número inteiro positivo")
    return number


class Command(BaseCommand):
    help = (
        "Valida o SIOE+ (DGAEP): organizações do setor público, tutela, agregação, "
        "sucessões e dirigentes; --apply grava organizações identificadas, estrutura "
        "publicada e cargos verificáveis de dirigentes. Simulação por omissão."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Gravar atomicamente; publica a estrutura entre organizações identificadas.",
        )
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )
        parser.add_argument(
            "--cache-dir",
            type=Path,
            help=(
                "Pasta local para guardar as respostas (já minimizadas) e retomar uma recolha "
                "longa; uma subpasta por data de referência."
            ),
        )
        parser.add_argument(
            "--limit",
            type=_positive,
            help=(
                "Recolher o histórico de apenas N entidades (ensaio); uma recolha parcial "
                "nunca dá âmbitos como ausentes."
            ),
        )

    def _progress(self, done: int, total: int) -> None:
        if done == total or done % PROGRESS_EVERY == 0:
            self.stdout.write(f"Histórico SIOE+: {done}/{total} entidades.")

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(
                as_of=options["as_of"],
                cache_dir=options["cache_dir"],
                limit=options["limit"],
                progress=self._progress,
            )
            summary = summarise(snapshot)
            self.stdout.write(
                f"SIOE+: {summary['organisations']} organizações ({summary['ghosts']} fictícias "
                f"excluídas); existentes={summary['existing']}; por NIPC={summary['via_nipc']}; "
                f"novas={summary['new']}; NIPC a associar={summary['nipc_attach']}; "
                f"históricos={summary['histories']}; áreas governativas={summary['ministries']} "
                f"(regionais={summary['regional_ministries']}, sem Governo importado="
                f"{summary['unlinked_ministries']}); tutelas={summary['tutelas']} "
                f"(antigas sem Governo={summary['legacy_tutelas']}); agregações="
                f"{summary['parents']}; sucessões={summary['successions']}; dirigentes="
                f"{summary['members']} (excluídos={summary['dropped_members']}); "
                f"data={snapshot.as_of}."
            )
            if not options["apply"]:
                self.stdout.write("Simulação: sem escritas.")
                return
            result = apply_snapshot(snapshot)
        except SioeError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            ) from exc
        scope = "completa" if snapshot.complete else "parcial (sem ausências)"
        self.stdout.write(
            f"Aplicação {scope}: organizações novas={result['new']}; por NIPC="
            f"{result['via_nipc']}; áreas governativas={result['ministries']} (sem Governo="
            f"{result['unlinked_ministries']}); ligações por resolver="
            f"{result['unresolved_links']}; estrutura novas={result.get('structure_created', 0)}"
            f", alteradas={result.get('structure_changed', 0)}, cessadas="
            f"{result.get('structure_ceased', 0)}, publicadas="
            f"{result.get('structure_published', 0)}; dirigentes novas="
            f"{result.get('boards_created', 0)}, alteradas={result.get('boards_changed', 0)}, "
            f"cessadas={result.get('boards_ceased', 0)}."
        )
