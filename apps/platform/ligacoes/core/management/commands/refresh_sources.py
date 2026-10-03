"""Refresh official sources in dependency order, with independent snapshot scopes."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import gettempdir
from time import monotonic, sleep

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from ligacoes.core.models import RefreshState

FAMILIES = (
    "parliament",
    "parliament_bodies",
    "government",
    "government_archive",
    "government_nominations",
    "ept_offices",
    "interests",
    "gleif",
    "wikidata_crosswalk",
    "sioe",
    "base_contracts",
    "igf_subsidies",
    "eu_funds",
    "etf_boards",
    "european_parliament",
    "eu_contacts",
    "parliament_interests",
    "parliament_activities",
    "parliament_gifts",
    "link_identities",
)
MIN_INTERVAL = timedelta(days=7)
AR_PAUSE_SECONDS = 3
AR_DEPENDENTS = frozenset(
    {
        "government",
        "government_archive",
        "government_nominations",
        "ept_offices",
        "interests",
        "european_parliament",
    }
)


@dataclass(frozen=True)
class Step:
    family: str
    arguments: tuple[str, ...] = ()
    scope: str = ""
    interval: timedelta = timedelta(0)

    @property
    def name(self) -> str:
        return f"{self.family}:{self.scope}" if self.scope else self.family


def steps(*, initial: bool, year: int, cache_dir: Path) -> Iterator[Step]:
    # Imported only when executing, not when discovering commands or rendering --help.
    from ligacoes.core import government, government_archive, parliament_fetch, parliament_gifts
    from ligacoes.core.eu_funds import PROGRAMMES

    current = parliament_fetch.LEGISLATURES[-1]
    for family in ("parliament", "parliament_bodies"):
        for code in parliament_fetch.LEGISLATURES:
            interval = timedelta(0) if code == current else MIN_INTERVAL
            yield Step(family, ("--legislature", code), code, interval)
    for family, governments in (
        ("government", government.GOVERNMENTS),
        ("government_archive", government_archive.GOVERNMENTS),
        ("government_nominations", government.GOVERNMENTS),
    ):
        for code in governments:
            yield Step(family, ("--government", code), code)
    yield Step("ept_offices")
    yield Step("interests", ("--all",))
    yield Step("gleif")
    yield Step("wikidata_crosswalk")
    yield Step("sioe", ("--cache-dir", str(cache_dir)), interval=MIN_INTERVAL)
    for value in range(2012 if initial else year - 1, year + 1):
        yield Step("base_contracts", ("--year", str(value)), str(value), MIN_INTERVAL)
    yield Step("igf_subsidies", ("--year", "all"))
    for programme in PROGRAMMES:
        yield Step("eu_funds", ("--programme", programme), programme, MIN_INTERVAL)
    yield Step("etf_boards")
    yield Step("european_parliament")
    for dataset in ("register", "ec-meetings"):
        yield Step("eu_contacts", ("--dataset", dataset), dataset)
    # Older interest links return publisher 'not found' pages; XVI+ use EpT instead.
    for code in parliament_fetch.LEGISLATURES:
        if code in {"XI", "XII", "XIII", "XIV", "XV"}:
            yield Step("parliament_interests", ("--legislature", code), code, MIN_INTERVAL)
    for code in parliament_fetch.LEGISLATURES:
        interval = timedelta(0) if code == current else MIN_INTERVAL
        yield Step("parliament_activities", ("--legislature", code), code, interval)
    for code in parliament_fetch.LEGISLATURES:
        if code in parliament_gifts.LEGISLATURES:
            interval = timedelta(0) if code == current else MIN_INTERVAL
            yield Step("parliament_gifts", ("--legislature", code), code, interval)
    yield Step("link_identities")


class Command(BaseCommand):
    help = "Atualizar todas as fontes oficiais por ordem de dependência; sem --apply não grava."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--apply", action="store_true", help="Gravar os snapshots completos.")
        selector_help = (
            "Passos: "
            + ", ".join(FAMILIES)
            + ". Aceita também um âmbito específico, por exemplo parliament:XVI."
        )
        parser.add_argument(
            "--only",
            nargs="+",
            action="extend",
            default=[],
            metavar="STEP",
            help="Executar apenas estes passos, ignorando intervalos mínimos. " + selector_help,
        )
        parser.add_argument(
            "--skip",
            nargs="+",
            action="extend",
            default=[],
            metavar="STEP",
            help="Omitir estes passos. " + selector_help,
        )
        parser.add_argument(
            "--initial",
            action="store_true",
            help=(
                "Importar BASE desde 2012 e ignorar intervalos mínimos; normalmente BASE "
                "importa o ano atual e o anterior."
            ),
        )
        parser.add_argument(
            "--cache-dir",
            type=Path,
            default=Path(gettempdir()) / "ligacoes-refresh" / "sioe",
            help="Pasta persistente de respostas minimizadas SIOE (fora do Git).",
        )

    def handle(self, *args, **options) -> None:
        plan = list(
            steps(
                initial=options["initial"],
                year=timezone.localdate().year,
                cache_dir=options["cache_dir"],
            )
        )
        valid = set(FAMILIES) | {step.name for step in plan}
        unknown = (set(options["only"]) | set(options["skip"])) - valid
        if unknown:
            raise CommandError("Passos desconhecidos: " + ", ".join(sorted(unknown)))
        failed = []
        failed_families = set()
        last_was_ar = False
        for step in plan:
            selected = {step.family, step.name}
            if options["only"] and not selected.intersection(options["only"]):
                continue
            if selected.intersection(options["skip"]):
                continue
            if ("parliament" in failed_families and step.family in AR_DEPENDENTS) or (
                "government" in failed_families and step.family in {"ept_offices", "interests"}
            ):
                self.stdout.write(f"{step.name}: skipped: prerequisite failed")
                continue
            started = monotonic()
            try:
                # A dry-run never advances the successful-apply clock.
                if step.interval and not options["only"] and not options["initial"]:
                    state = RefreshState.objects.filter(step=step.name).first()
                    if state and timezone.now() - state.last_success < step.interval:
                        self.stdout.write(f"{step.name}: skipped (interval) 0.0s")
                        continue
                is_ar = step.family.startswith("parliament")
                if is_ar and last_was_ar:
                    sleep(AR_PAUSE_SECONDS)
                last_was_ar = is_ar
                arguments = (*step.arguments, "--apply") if options["apply"] else step.arguments
                call_command(
                    "link_identities"
                    if step.family == "link_identities"
                    else f"import_{step.family}",
                    *arguments,
                    stdout=self.stdout,
                    stderr=self.stderr,
                )
                if options["apply"] and step.interval:
                    RefreshState.objects.update_or_create(
                        step=step.name,
                        defaults={"last_success": timezone.now()},
                    )
            except Exception as exc:
                failed.append(step.name)
                failed_families.add(step.family)
                self.stderr.write(
                    f"{step.name}: failed ({type(exc).__name__}: {exc}) "
                    f"{monotonic() - started:.1f}s"
                )
            else:
                self.stdout.write(f"{step.name}: ok {monotonic() - started:.1f}s")
        if failed:
            raise CommandError("Falharam passos: " + ", ".join(failed))
