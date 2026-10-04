from django.core.management.base import BaseCommand

from ligacoes.core.identity import reconcile_identities


class Command(BaseCommand):
    help = "Reconcilia identidades corroboradas; sem --apply apenas mostra o plano."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Aplicar as correspondências auditadas."
        )

    def handle(self, *args, **options):
        applying = options["apply"]
        matches = reconcile_identities(apply=applying)
        for match in matches:
            self.stdout.write(f"{match.from_entity.slug} -> {match.to_entity.slug}: {match.basis}")
        action = "aplicadas" if applying else "propostas (sem alterações)"
        self.stdout.write(f"Correspondências {action}: {len(matches)}")
