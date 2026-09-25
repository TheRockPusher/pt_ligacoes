import math
import signal
import time
from types import FrameType

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from ligacoes.core.import_jobs import run_next_import


def _stop(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


class Command(BaseCommand):
    help = "Executa a fila de importações parlamentares, sem publicação automática."
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Processa no máximo um pedido.")
        parser.add_argument(
            "--poll-interval", type=float, default=5.0, help="Intervalo em segundos."
        )

    def handle(self, *args, **options) -> None:
        interval = options["poll_interval"]
        if not math.isfinite(interval) or interval <= 0:
            raise CommandError("O intervalo deve ser um número positivo e finito.")
        try:
            executor = MigrationExecutor(connection)
            executor.loader.check_consistent_history(connection)
            pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        except Exception:
            raise CommandError("Não foi possível verificar o esquema da base de dados.") from None
        if pending:
            raise CommandError(
                "Existem migrações pendentes. Migre primeiro o serviço web e reinicie o executor."
            )
        previous_handler = signal.signal(signal.SIGTERM, _stop)
        try:
            while True:
                try:
                    run = run_next_import()
                except Exception:
                    raise CommandError(
                        "O executor parou por segurança; os pedidos não serão repetidos automaticamente."
                    ) from None
                if run is not None:
                    self.stdout.write(f"{run.pk}: {run.get_status_display()}")
                if options["once"]:
                    if run is None:
                        self.stdout.write("Não há pedidos disponíveis na fila.")
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("Executor de importações terminado.")
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            connection.close()
