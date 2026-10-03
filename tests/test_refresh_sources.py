from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from ligacoes.core import government, government_archive, parliament_fetch, parliament_gifts
from ligacoes.core.eu_funds import PROGRAMMES
from ligacoes.core.management.commands.refresh_sources import FAMILIES, MIN_INTERVAL, steps
from ligacoes.core.models import RefreshState

COMMAND = "ligacoes.core.management.commands.refresh_sources"
pytestmark = pytest.mark.django_db


def invoke(*arguments):
    output, errors = StringIO(), StringIO()
    call_command("refresh_sources", *arguments, stdout=output, stderr=errors)
    return output.getvalue(), errors.getvalue()


def test_every_source_runs_in_dependency_order_and_full_scopes():
    with patch(f"{COMMAND}.call_command") as importer:
        invoke("--apply")
    calls = importer.call_args_list
    families = [call.args[0].removeprefix("import_") for call in calls]
    assert list(dict.fromkeys(families)) == list(FAMILIES)
    assert [call.args[2] for call in calls if call.args[0] == "import_parliament"] == list(
        parliament_fetch.LEGISLATURES
    )
    assert [call.args[2] for call in calls if call.args[0] == "import_government"] == list(
        government.GOVERNMENTS
    )
    assert [call.args[2] for call in calls if call.args[0] == "import_government_archive"] == list(
        government_archive.GOVERNMENTS
    )
    assert [call.args[2] for call in calls if call.args[0] == "import_eu_funds"] == list(PROGRAMMES)
    assert [call.args[2] for call in calls if call.args[0] == "import_eu_contacts"] == [
        "register",
        "ec-meetings",
    ]
    assert [call.args[2] for call in calls if call.args[0] == "import_parliament_gifts"] == [
        code for code in parliament_fetch.LEGISLATURES if code in parliament_gifts.LEGISLATURES
    ]
    assert [call.args[2] for call in calls if call.args[0] == "import_parliament_interests"] == [
        "XI",
        "XII",
        "XIII",
        "XIV",
        "XV",
    ]
    assert all(call.args[-1] == "--apply" for call in calls)
    assert any(call.args[:2] == ("import_interests", "--all") for call in calls)
    assert any(
        call.args[:2] == ("import_igf_subsidies", "--year") and call.args[2] == "all"
        for call in calls
    )


def test_failure_is_isolated_and_command_returns_failure_status():
    def fail_first(command, *args, **kwargs):
        if command == "import_ept_offices":
            raise CommandError("fictional source unavailable")

    output, errors = StringIO(), StringIO()
    with (
        patch(f"{COMMAND}.call_command", side_effect=fail_first) as importer,
        pytest.raises(CommandError) as failure,
    ):
        call_command(
            "refresh_sources",
            "--only",
            "ept_offices",
            "gleif",
            stdout=output,
            stderr=errors,
        )
    assert failure.value.returncode == 1
    assert [call.args[0] for call in importer.call_args_list] == [
        "import_ept_offices",
        "import_gleif",
    ]
    assert "ept_offices: failed" in errors.getvalue()
    assert "gleif: ok" in output.getvalue()


def test_recent_heavy_step_is_skipped_and_expired_success_runs():
    RefreshState.objects.create(step="sioe", last_success=timezone.now())
    with patch(f"{COMMAND}.call_command") as importer:
        output, _ = invoke("--apply")
    assert "sioe: skipped (interval)" in output
    assert "import_sioe" not in [call.args[0] for call in importer.call_args_list]
    RefreshState.objects.filter(step="sioe").update(
        last_success=timezone.now() - MIN_INTERVAL - timedelta(seconds=1)
    )
    with patch(f"{COMMAND}.call_command") as importer:
        invoke("--apply")
    assert "import_sioe" in [call.args[0] for call in importer.call_args_list]
    assert RefreshState.objects.get(step="sioe").last_success > timezone.now() - timedelta(
        minutes=1
    )


def test_only_ignores_interval_and_skip_wins_within_selected_family():
    RefreshState.objects.create(step="sioe", last_success=timezone.now())
    with patch(f"{COMMAND}.call_command") as importer:
        invoke("--only", "sioe", "eu_contacts", "--skip", "eu_contacts:ec-meetings")
    assert [call.args[0] for call in importer.call_args_list] == [
        "import_sioe",
        "import_eu_contacts",
    ]
    assert importer.call_args_list[1].args == ("import_eu_contacts", "--dataset", "register")
    assert all("--apply" not in call.args for call in importer.call_args_list)


def test_initial_expands_base_years():
    year = timezone.localdate().year
    with patch(f"{COMMAND}.call_command") as importer:
        invoke("--only", "base_contracts", "--initial")
    assert [int(call.args[2]) for call in importer.call_args_list] == list(range(2012, year + 1))
    with patch(f"{COMMAND}.call_command") as importer:
        invoke("--only", "base_contracts")
    assert [int(call.args[2]) for call in importer.call_args_list] == [year - 1, year]


def test_dry_run_and_failed_apply_do_not_advance_intervals():
    with patch(f"{COMMAND}.call_command"):
        invoke("--only", "sioe")
    assert not RefreshState.objects.exists()
    with (
        patch(f"{COMMAND}.call_command", side_effect=CommandError("fictional failure")),
        pytest.raises(CommandError),
    ):
        invoke("--only", "sioe", "--apply")
    assert not RefreshState.objects.exists()


def test_unknown_selection_fails_before_any_import():
    with (
        patch(f"{COMMAND}.call_command") as importer,
        pytest.raises(CommandError, match="Passos desconhecidos"),
    ):
        invoke("--only", "nonexistent")
    importer.assert_not_called()


def test_persistent_sioe_cache_argument():
    plan = list(steps(initial=False, year=2026, cache_dir=Path("/persistent/sioe")))
    assert next(step for step in plan if step.family == "sioe").arguments == (
        "--cache-dir",
        "/persistent/sioe",
    )
