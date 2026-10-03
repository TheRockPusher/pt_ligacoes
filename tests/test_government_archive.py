from dataclasses import replace
from datetime import date
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ligacoes.core.government_archive import (
    GOVERNMENTS,
    INDEX_URL,
    ORIGIN,
    ROOT,
    GovernmentArchiveError,
    GovernmentArchiveSnapshot,
    allowed_url,
    apply_snapshot,
    composition_url,
    fetch_snapshot,
    parse_page,
)
from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Evidence,
    ParliamentStatusInterval,
    Relationship,
    Source,
    SourceIdentity,
    SourceObservation,
    SourceSyncState,
    Term,
)
from ligacoes.core.services import withdraw_relationship

START = date(2000, 1, 3)
LATER = date(2000, 2, 3)
END = date(2001, 1, 3)
DAY = date(2026, 9, 1)


def fixture(*, government="gc06", day=None, empty=False, minister="Bela Exemplo"):
    base = composition_url(government)
    links = "".join(f'<a href="{base}?date={value}">{value}</a>' for value in (START, LATER))
    cards = (
        ""
        if empty
        else f"""
    <div class="firstHistory">
      <cite><img alt="PHOTO_NAME_CANARY" src="/private-photo.jpg" /></cite>
      <span>Ana Fictícia</span><h3 class="mainForecolor">Primeiro-Ministro</h3>
      <a>Expandir</a><ul><li><cite></cite><span>Duarte Exemplo</span>
      <h3 class="mainForecolor">Secretário de Estado Adjunto do Primeiro-Ministro</h3>
      </li></ul>
    </div>
    <ul class="historico"><li><cite></cite><span>{minister}</span>
      <h3 class="mainForecolor">Ministra de Estado</h3>
      <h3 class="mainForecolor">Ministra da Imaginação</h3>
      <span class="subTitle">Imaginação</span><a>Expandir</a>
      <ul><li><cite></cite><span>Carlos Fictício</span>
      <h3 class="mainForecolor">Secretário de Estado da Imaginação</h3></li></ul>
    </li></ul>"""
    )
    return f'<div id="archive_governmentPeople">{cards}</div><aside>{links}</aside>'.encode()


def snapshot():
    members = []
    for day in (START, LATER):
        url = f"{composition_url('gc06')}?date={day}"
        members.extend(parse_page(fixture(day=day), government="gc06", source_url=url).members)
    return GovernmentArchiveSnapshot(
        "gc06",
        "VI Governo Fictício",
        START,
        END,
        tuple(members),
        tuple(f"{composition_url('gc06')}?date={day}" for day in (START, LATER)),
        DAY,
    )


def test_parser_keeps_visible_names_all_roles_and_nested_portfolios_only():
    page = parse_page(fixture(), government="gc06", source_url=composition_url("gc06"))
    assert len(page.members) == 5
    assert page.dates == (START, LATER)
    assert {(member.name, member.role) for member in page.members} >= {
        ("Ana Fictícia", "Primeiro-Ministro"),
        ("Bela Exemplo", "Ministra de Estado"),
        ("Bela Exemplo", "Ministra da Imaginação"),
    }
    secretary = next(member for member in page.members if member.name == "Carlos Fictício")
    assert secretary.portfolio == "Imaginação"
    pm_secretary = next(member for member in page.members if member.name == "Duarte Exemplo")
    assert pm_secretary.portfolio == "Primeiro-Ministro"
    assert "CANARY" not in repr(page)
    assert all(member.observed_on is None for member in page.members)


def test_empty_advertised_states_are_valid_but_missing_composition_markup_fails():
    base = composition_url("gc06")
    assert not parse_page(fixture(empty=True), government="gc06", source_url=base).members
    assert not parse_page(
        fixture(empty=True), government="gc06", source_url=base + f"?date={LATER}"
    ).members
    with pytest.raises(GovernmentArchiveError):
        parse_page(b"<h1>Unavailable</h1>", government="gc06", source_url=base)


def test_identical_secretary_cards_under_two_ministers_are_one_assertion():
    html = fixture()
    duplicate = (
        '<ul class="historico"><li><span class="subTitle">Imaginação</span>'
        "<ul><li><cite></cite><span>Carlos Fictício</span>"
        '<h3 class="mainForecolor">Secretário de Estado da Imaginação</h3>'
        "</li></ul></li></ul>"
    ).encode()
    html = html.replace(b"</div><aside>", duplicate + b"</div><aside>")
    page = parse_page(html, government="gc06", source_url=composition_url("gc06"))
    assert len(page.members) == 5


@pytest.mark.parametrize(
    "url",
    [
        "http://www.historico.portugal.gov.pt" + ROOT + "/gc06/composicao.aspx",
        ORIGIN + ROOT + "/gc6/composicao.aspx",
        ORIGIN + ROOT + "/gc21/composicao.aspx",
        ORIGIN + ROOT + "/gc06/composicao.aspx?date=2000-02-30",
        ORIGIN + ROOT + "/gc06/composicao.aspx?date=2000-01-03&date=2000-02-03",
        ORIGIN + ROOT + "/gc06/composicao.aspx?date=2000-01-03&extra=yes",
        "https://evil.example" + ROOT + "/gc06/composicao.aspx",
        ORIGIN + "/ImageGen.ashx?image=photo",
    ],
)
def test_http_allowlist_refuses_unadvertised_routes(url):
    assert not allowed_url(url)


def test_government_codes_are_zero_padded_and_complete():
    assert tuple(f"gc{number:02d}" for number in range(1, 21)) == GOVERNMENTS
    assert allowed_url(INDEX_URL)
    assert allowed_url(composition_url("gc06") + f"?date={START}")


@pytest.mark.django_db
def test_fetch_collects_every_advertised_state_sequentially_and_command_dry_run_is_read_only(
    capsys,
):
    index = f'<li><span>VI Governo Fictício</span><a href="{ROOT}/gc06.aspx">Ver</a></li>'.encode()
    responses = {
        INDEX_URL: index,
        composition_url("gc06"): fixture(empty=True),
        composition_url("gc06") + f"?date={START}": fixture(),
        composition_url("gc06") + f"?date={LATER}": fixture(),
        composition_url("gc07"): fixture(government="gc07")
        .replace(str(START).encode(), str(END).encode())
        .replace(str(LATER).encode(), str(END).encode()),
    }
    visited = []

    def fetch(url, **kwargs):
        visited.append(url)
        assert kwargs["allowed"](url)
        return responses[url]

    with patch("ligacoes.core.government_archive.download", side_effect=fetch):
        parsed = fetch_snapshot(government="gc06", as_of=DAY)
        assert parsed.start_date == START and parsed.end_date == END
        assert len(parsed.members) == 10
        assert visited == list(responses)
        call_command("import_government_archive", government="gc06")
    assert "Ana Fictícia" in capsys.readouterr().out
    assert not Entity.objects.exists()
    assert not SourceObservation.objects.exists()
    assert not SourceSyncState.objects.exists()


@pytest.mark.django_db
def test_apply_publishes_scoped_people_unknown_boundaries_all_dated_evidence_and_is_idempotent():
    namesake = Entity.objects.create(
        name="Ana Fictícia", kind="person", slug="unrelated", is_public=True
    )
    result = apply_snapshot(snapshot())
    assert result["published"] == 5
    pm = Relationship.objects.get(role="Primeiro-Ministro")
    assert pm.subject_id != namesake.pk
    assert pm.status == "published"
    assert pm.start_date is None and pm.end_date is None
    assert pm.role_class == "leadership"
    assert pm.temporal_status == "ended"
    assert pm.term is not None
    assert pm.term.code == "gc06"
    assert pm.term.start_date == START
    assert pm.term.end_date == END.replace(day=2)
    assert pm.object.classification == "government"
    assert SourceIdentity.objects.filter(entity=pm.subject, source="scoped_name").exists()
    evidence = Evidence.objects.filter(relationship=pm)
    assert evidence.count() == 2
    assert set(evidence.values_list("source__url", flat=True)) == {
        composition_url("gc06") + f"?date={day}" for day in (START, LATER)
    }
    assert not evidence.filter(is_public=False).exists()
    assert not Source.objects.filter(is_public=False).exists()
    assert Source.objects.filter(dataset="gov_arquivo_historico").exists()
    again = apply_snapshot(snapshot())
    assert again["created"] == again["changed"] == again["published"] == 0
    assert Relationship.objects.count() == 5
    assert Evidence.objects.count() == 10


@pytest.mark.django_db
@pytest.mark.parametrize("first_seen", [START, LATER])
def test_ar_suspension_corroborates_first_evidenced_presence(first_seen):
    deputy = official_entity(
        "parliament", "fictional-17", name="Ana Fictícia", kind="person", classification=""
    )
    ParliamentStatusInterval.objects.create(
        cadastro_id="fictional-17",
        entity=deputy,
        legislature="FICT",
        status="Suspenso",
        start=first_seen,
        end=LATER,
    )
    original = snapshot()
    observed = replace(
        original,
        members=tuple(
            member
            for member in original.members
            if member.name != "Ana Fictícia"
            or (member.observed_on is not None and member.observed_on >= first_seen)
        ),
    )
    apply_snapshot(observed)
    relationship = Relationship.objects.get(role="Primeiro-Ministro")
    assert relationship.subject_id == deputy.pk
    assert relationship.start_date is None and relationship.end_date is None


@pytest.mark.django_db
def test_rejection_persists_and_missing_office_ceases(django_user_model):
    original = snapshot()
    apply_snapshot(original)
    pm = Relationship.objects.get(role="Primeiro-Ministro")
    reviewer = django_user_model.objects.create_user(
        username="archive-reviewer", is_superuser=True, is_staff=True
    )
    withdraw_relationship(pm, reviewer)
    apply_snapshot(original)
    pm.refresh_from_db()
    assert pm.status == "rejected"
    reduced = replace(
        original,
        members=tuple(member for member in original.members if member.name != "Carlos Fictício"),
    )
    result = apply_snapshot(reduced)
    assert result["ceased"] == 1
    assert not Relationship.objects.filter(
        subject__name="Carlos Fictício", status="published"
    ).exists()


@pytest.mark.django_db
def test_failure_during_evidence_collection_rolls_back_whole_government():
    with (
        patch(
            "ligacoes.core.government_archive.Evidence.objects.get_or_create",
            side_effect=GovernmentArchiveError("fixture failure"),
        ),
        pytest.raises(GovernmentArchiveError),
    ):
        apply_snapshot(snapshot())
    assert not Term.objects.exists()
    assert not Entity.objects.exists()
    assert not Relationship.objects.exists()
