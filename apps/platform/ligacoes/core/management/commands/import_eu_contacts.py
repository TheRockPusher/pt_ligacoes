from argparse import ArgumentTypeError
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from django.utils import timezone

from ligacoes.core.eu_contacts import (
    EC_MEETINGS,
    EP_MEETINGS,
    REGISTER,
    EuContactsError,
    apply_snapshot,
    fetch_snapshot,
    parse_month,
)

DATASETS = {
    "register": frozenset({REGISTER}),
    "ep-meetings": frozenset({REGISTER, EP_MEETINGS}),
    "ec-meetings": frozenset({REGISTER, EC_MEETINGS}),
    "all": frozenset({REGISTER, EP_MEETINGS, EC_MEETINGS}),
}


def month(value: str) -> tuple[int, int]:
    try:
        return parse_month(value)
    except ValueError as exc:
        raise ArgumentTypeError("use AAAA-MM") from exc


class Command(BaseCommand):
    help = (
        "Valida organizações do Registo de Transparência da UE e reuniões de eurodeputados "
        "portugueses e de gabinetes de comissários portugueses; --apply grava. Simulação por "
        "omissão. Com --dataset all, um bloqueio de acesso às reuniões PE é comunicado e "
        "esse dataset é ignorado, preservando os seus dados existentes."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dataset", choices=sorted(DATASETS), default="all")
        parser.add_argument("--as-of", type=date.fromisoformat, default=timezone.localdate())
        parser.add_argument(
            "--from",
            dest="first_month",
            type=month,
            default=None,
            help="Primeiro mês (AAAA-MM) das reuniões do Parlamento Europeu; meses anteriores "
            "ficam intactos.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true", help="Gravar atomicamente.")
        mode.add_argument(
            "--dry-run", action="store_true", help="Validar sem gravar (predefinição)."
        )

    def handle(self, *args, **options) -> None:
        try:
            snapshot = fetch_snapshot(
                datasets=DATASETS[options["dataset"]],
                as_of=options["as_of"],
                first_month=options["first_month"],
                skip_blocked_ep=options["dataset"] == "all",
            )
            for warning in snapshot.warnings:
                self.stderr.write(warning)
            portuguese = sum(1 for r in snapshot.registrants.values() if r.portuguese)
            ep = sum(len(group) for group in snapshot.ep_months.values())
            ec = sum(len(group) for group in snapshot.ec_files.values())
            dropped = sum(snapshot.dropped.values())
            summary = (
                f"{len(snapshot.registrants)} registos de transparência ({portuguese} com sede "
                f"em Portugal); {ep} reuniões PE em {len(snapshot.ep_months)} meses; "
                f"{ec} reuniões CE; {dropped} linhas excluídas (outros deputados, testes, "
                "datas futuras, duplicados)"
            )
            if not options["apply"]:
                self.stdout.write(f"Simulação: {summary}; data={snapshot.as_of}. Sem escritas.")
                return
            result = apply_snapshot(snapshot)
        except EuContactsError as exc:
            raise CommandError(str(exc)) from exc
        except (ValidationError, DatabaseError) as exc:
            # Never emit names, payloads, identifiers or database diagnostics.
            raise CommandError(
                "Falha de validação ou gravação; nenhuma alteração parcial foi aplicada."
            ) from exc
        self.stdout.write(
            f"Aplicação: {summary}; organizações={result.get('organisations', 0)}; "
            f"criadas={result.get('organisations_created', 0)}; "
            f"a aguardar revisão={result.get('organisations_pending_review', 0)}; "
            f"participantes pessoais excluídos="
            f"{result.get('self_employed_dropped', 0) + result.get('possible_persons_dropped', 0)}; "
            f"reuniões sem organização={result.get('without_organisation', 0)}; "
            f"novos={result.get('created', 0)}; alterados={result.get('changed', 0)}; "
            f"cessados={result.get('ceased', 0)}; publicados={result.get('published', 0)}; "
            f"rascunhos={result.get('draft', 0)}."
        )
