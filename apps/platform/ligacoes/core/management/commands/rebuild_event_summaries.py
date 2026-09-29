from django.core.management.base import BaseCommand, CommandParser
from django.db import connection

from ligacoes.core.event_summaries import rebuild_event_summaries
from ligacoes.core.models import EventEntitySummary, EventPairSummary, import_transaction


class Command(BaseCommand):
    help = (
        "Reconstrói de raiz os resumos derivados de eventos públicos (por entidade e por par "
        "de entidades) a partir de public_events(); toda ou só de um conjunto de dados. "
        "Usar após restaurar uma cópia da base de dados."
    )
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dataset", help="Reconstruir só este conjunto de dados.")

    def handle(self, *args, **options) -> None:
        dataset: str | None = options["dataset"]
        with import_transaction():
            rebuild_event_summaries(dataset=dataset)
            # One planner pass over the fresh rows keeps the first public reads fast.
            with connection.cursor() as cursor:
                for model in (EventEntitySummary, EventPairSummary):
                    cursor.execute(f"ANALYZE {connection.ops.quote_name(model._meta.db_table)}")
        self.stdout.write(
            f"Resumos reconstruídos: entidades={EventEntitySummary.objects.count()}; "
            f"pares={EventPairSummary.objects.count()}."
        )
