import json
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from django.core.management import call_command
from django.utils.text import slugify

from ligacoes.core.government import (
    ORIGIN,
    GovernmentMember,
    GovernmentSnapshot,
    JSONObject,
)
from ligacoes.core.government import apply_snapshot as apply_composition
from ligacoes.core.government_nominations import (
    NominationsImportError,
    NominationsSnapshot,
    apply_snapshot,
    dr_document,
    fetch_snapshot,
    office_key,
    parse_index,
    parse_page,
    validate_nominations_url,
)
from ligacoes.core.models import Entity, Relationship, SourceIdentity, SourceObservation, Term

DAY = date(2026, 9, 25)
START = date(2025, 6, 5)
PAY = "PAY_CANARY 9 999,99 €"
DR = "https://diariodarepublica.pt/dr/detalhe/despacho/1234-2025-900000001"
DRE = "https://dre.pt/dre/detalhe/despacho/77-b-2024-900000002"
MINISTER = "Gabinete do Ministro das Finanças Fictícias"
SECRETARY = "Gabinete da Secretária de Estado da Habitação Fictícia"
PRIME_MINISTER = "Gabinete do Primeiro-Ministro"
HEADER = (
    "<thead><tr><th>Função</th><th>Nome</th><th>Rendimento bruto</th>"
    "<th>Rendimento líquido</th><th>Data de nomeação</th><th>Publicação em DRE</th></tr></thead>"
)


def guid(number: int) -> str:
    return str(UUID(int=number))


def table(heading: str | None, rows: list[list[str]], *, tag: str = "h2") -> str:
    body = "".join(
        "<tr>" + "".join(f'<td colspan="1" rowspan="l">{cell}</td>' for cell in row) + "</tr>"
        for row in rows
    )
    title = f"<{tag}>{heading}</{tag}><p>&nbsp;</p>" if heading else ""
    return f'{title}<figure class="table"><table>{HEADER}<tbody>{body}</tbody></table></figure>'


def link(href: str, text: str = "Despacho") -> str:
    return f'<a href="{href}" target="_blank">{text}</a>'


def finance_content() -> str:
    return (
        '<div class="ck-content"><p>O rendimento bruto engloba a remuneração. PAY_CANARY</p>'
        # An empty heading after the real one does not replace it.
        + f"<h4>{MINISTER}</h4><h4>&nbsp;</h4>"
        + table(
            None,
            [
                ["Chefe do Gabinete", "Pessoa Exemplo Um (1)", PAY, PAY, "02/07/2025", link(DR)],
                [
                    "Adjunta",
                    "Pessoa Exemplo Dois",
                    PAY,
                    PAY,
                    "2025-07-03",
                    link("https://dre.pt/web/guest/pesquisa/-/search/1/details/normal?q=Exemplo"),
                ],
                [
                    "Técnica Especilaista",
                    "Pessoa Exemplo Três*",
                    PAY,
                    PAY,
                    "\u00b42025-07-04",
                    link("https://Despacho n.º 1/2025"),
                ],
                ["Motorista", "Pessoa Exemplo Quatro", PAY, PAY, "02/07/2025", link(DR)],
                ["", "", "", "", "", ""],
                [
                    "Tecnico Especialista",
                    "Pessoa Exemplo Cinco",
                    PAY,
                    PAY,
                    "01/01/2024",
                    link(DRE + "?_ts=1700000000000 "),
                ],
            ],
        )
        + "<p>(1) Opta pelo vencimento de origem. PAY_CANARY</p>"
        + table(
            SECRETARY,
            [["Adjunto", "Pessoa Exemplo Seis", PAY, PAY, "", "Aguarda publicação"]],
        )
        + "</div>"
    )


def prime_minister_content() -> str:
    return table(
        PRIME_MINISTER,
        [["Chefe de Gabinete", "Pessoa Exemplo Sete", PAY, PAY, "06/06/2025", link(DR)]],
        tag="h4",
    )


PAGES = (
    (11, "financas-ficticias", "Ministro das Finanças Fictícias"),
    (12, "primeiro-ministro", "Primeiro-Ministro"),
    (13, "legislacao-aplicavel", "Legislação aplicável"),
)


def index_html(pages: tuple[tuple[int, str, str], ...] = PAGES) -> bytes:
    menu = [
        {
            "Id": guid(number),
            "Href": f"/pt/gc25/governo/nomeacoes/{slug}",
            "NavigationTitle": {"value": title},
            "NavigationSubtitle": {"value": f"Nomeações — {title}"},
        }
        for number, slug, title in pages
    ]
    payload = {
        "buildId": "build-fictional",
        "props": {
            "pageProps": {
                "layoutData": {
                    "sitecore": {
                        "context": {
                            "governmentContext": {
                                "governmentId": guid(1),
                                "governmentName": "Governo Fictício",
                                "startDate": "20250605T000000Z",
                                "endDate": None,
                            }
                        },
                        "route": {
                            "templateName": "Appointments Page",
                            "placeholders": {
                                "main": [
                                    {"componentName": "Menu", "fields": menu},
                                    {
                                        "componentName": "Menu",
                                        "fields": [{"Href": "/pt/gc25/governo/composicao"}],
                                    },
                                ]
                            },
                        },
                    }
                }
            }
        },
    }
    app = ORIGIN + "/_next/static/chunks/pages/_app-12345678.js?dpl=dpl_fictional123"
    return (
        f'<script src="{app}"></script>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    ).encode()


def subpage(number: int, content: str, template: str = "Appointment Page") -> JSONObject:
    return {
        "pageProps": {
            "layoutData": {
                "sitecore": {
                    "route": {
                        "itemId": guid(number),
                        "templateName": template,
                        "fields": {"Content": {"value": content}},
                    },
                    "context": {"governmentContext": {"governmentId": guid(1)}},
                }
            }
        }
    }


SUBPAGES = {
    "financas-ficticias": subpage(11, finance_content()),
    "primeiro-ministro": subpage(12, prime_minister_content()),
    "legislacao-aplicavel": subpage(13, "<p>Legislação aplicável fictícia.</p>", "Page"),
}


def parsed(slug: str = "financas-ficticias", content: str | None = None):
    index = parse_index(index_html(), government="gc25")
    number, _, title = next(entry for entry in PAGES if entry[1] == slug)
    payload = SUBPAGES[slug] if content is None else subpage(number, content)
    return parse_page(payload, index, page_id=guid(number), slug=slug, title=title)


def offline_snapshot(*, as_of: date = DAY) -> NominationsSnapshot:
    def offline_wire(_self, url: str) -> bytes:
        assert validate_nominations_url(url, "gc25")
        if url == ORIGIN + "/gc25/governo/nomeacoes":
            return index_html()
        slug = url.split("/nomeacoes/")[1].split(".json")[0]
        return json.dumps(SUBPAGES[slug]).encode()

    with (
        patch("ligacoes.core.government_nominations._Fetcher.fetch", offline_wire),
        patch("ligacoes.core.government_nominations.timezone.localdate", return_value=DAY),
    ):
        return fetch_snapshot(government="gc25", as_of=as_of)


def composition() -> None:
    """Fictional gc25 composition: two secretariats share one title."""

    def member(number: int, template: str, role: str, portfolio: str, path: str):
        return GovernmentMember(
            official_id=guid(number + 100),
            appointment_id=guid(number),
            name=f"Pessoa Fictícia {number}",
            role=role,
            portfolio_id=guid(number + 200),
            portfolio=portfolio,
            area_id=guid(1),
            area="Governo Fictício",
            start_date=START,
            end_date=None,
            source_url=ORIGIN + path,
            template=template,
        )

    members = (
        member(100, "prime-minister", "Primeiro-Ministro", "Primeiro-Ministro", "/gc25/pm"),
        member(101, "minister", "Ministro", "Finanças Fictícias", "/gc25/financas"),
        member(102, "secretary-of-state", "Secretária de Estado", "Habitação Fictícia", "/gc25/a"),
        member(103, "secretary-of-state", "Secretário de Estado", "Habitação Fictícia", "/gc25/b"),
    )
    apply_composition(
        GovernmentSnapshot("gc25", guid(1), "Governo Fictício", START, None, DAY, members)
    )


def test_only_documented_staff_roles_are_kept_and_pay_is_never_read():
    page = parsed()
    assert page is not None
    assert page.url == ORIGIN + "/pt/gc25/governo/nomeacoes/financas-ficticias"
    assert page.headings == (MINISTER, SECRETARY)
    assert [(row.heading, row.role, row.name, row.appointed_on) for row in page.rows] == [
        (MINISTER, "Chefe de Gabinete", "Pessoa Exemplo Um", date(2025, 7, 2)),
        (MINISTER, "Adjunta", "Pessoa Exemplo Dois", date(2025, 7, 3)),
        (MINISTER, "Técnica Especialista", "Pessoa Exemplo Três", date(2025, 7, 4)),
        (MINISTER, "Técnico Especialista", "Pessoa Exemplo Cinco", date(2024, 1, 1)),
        (SECRETARY, "Adjunto", "Pessoa Exemplo Seis", None),
    ]
    # The driver and the fully blank row are not retained.
    assert page.dropped == 2
    assert "PAY_CANARY" not in repr(page)
    assert "9 999" not in repr(page)


def test_dates_outside_the_term_are_kept_but_flagged():
    page = parsed()
    assert page is not None
    flagged = {row.name: row.outside_term for row in page.rows}
    assert flagged == {
        "Pessoa Exemplo Um": False,
        "Pessoa Exemplo Dois": False,
        "Pessoa Exemplo Três": False,
        "Pessoa Exemplo Cinco": True,
        "Pessoa Exemplo Seis": False,
    }


@pytest.mark.parametrize(
    "href,expected",
    [
        (DR, ("900000001", DR)),
        (DRE + "?_ts=1700000000000 ", ("900000002", DRE)),
        (
            "https://diariodarepublica.pt/dr/detalhe/declaracao-retificacao/9-2025-900000003",
            ("900000003", None),
        ),
        ("https://www.dre.pt/dre/analise-juridica/despacho/5-2020-900000004", ("900000004", None)),
        ("https://diariodarepublica.pt/dr/detalhe/doc/6-2025-2-900000005", ("900000005", None)),
        ("https://dre.pt/web/guest/pesquisa/-/search/1/details/normal?q=Nome+Exemplo", None),
        ("https://dre.pt/application/file/a/123456", None),
        ("https://files.diariodarepublica.pt/2s/2025/08/1/0001.pdf", None),
        ("https://Despacho n.º 1/2025", None),
        ("ttps://diariodarepublica.pt/dr/detalhe/despacho/1-2025-900000006", None),
        (DR + "," + DRE, None),
        ("http://diariodarepublica.pt/dr/detalhe/despacho/1-2025-900000007", None),
        (DR + "?q=Nome", None),
        ("", None),
    ],
)
def test_dr_document_ids_only_from_record_pages(href: str, expected: tuple[str, str | None] | None):
    found = dr_document(href)
    if expected is None:
        assert found is None
    else:
        assert found is not None
        assert found[0] == expected[0]
        if expected[1] is not None:
            assert found[1] == expected[1]


@pytest.mark.parametrize(
    "content",
    [
        # Unknown column layout.
        table(MINISTER, [["Adjunto", "Pessoa Exemplo", PAY, PAY, "01/07/2025", ""]]).replace(
            "Nome", "Observações"
        ),
        # A table no heading names.
        table(None, [["Adjunto", "Pessoa Exemplo", PAY, PAY, "01/07/2025", ""]]),
        # A row outside the six-column contract.
        table(MINISTER, [["Adjunto", "Pessoa Exemplo", "01/07/2025"]]),
    ],
)
def test_unknown_nomination_layouts_fail_closed(content: str):
    with pytest.raises(NominationsImportError):
        parsed(content=content)


def test_informational_subpages_are_skipped():
    assert parsed("legislacao-aplicavel") is None


def test_headings_map_to_portfolio_labels_keeping_adjunto_distinct():
    assert office_key(PRIME_MINISTER) == office_key("Primeiro-Ministro Primeiro-Ministro") == "pm"
    assert office_key(MINISTER) == office_key("Ministro Finanças Fictícias")
    assert office_key("Ministra das Finanças Fictícias") == office_key(MINISTER)
    assert office_key("Gabinete do Secretário de Estado Adjunto e da Justiça") == office_key(
        "Secretário de Estado Adjunto e da Justiça"
    )
    assert office_key("Gabinete do Secretário de Estado Adjunto e da Justiça") != office_key(
        "Secretário de Estado Justiça"
    )
    assert office_key(MINISTER) != office_key("Secretário de Estado Finanças Fictícias")
    assert office_key("Assessoria técnica") is None


@pytest.mark.parametrize(
    "url,allowed",
    [
        (ORIGIN + "/gc25/governo/nomeacoes", True),
        (
            ORIGIN + "/_next/data/build-x/gc25/governo/nomeacoes/financas.json"
            "?dpl=dpl_fictional123",
            True,
        ),
        (ORIGIN + "/gc24/governo/nomeacoes", False),
        (ORIGIN + "/gc25/governo/nomeacoes?url=https://example.org", False),
        (ORIGIN + "/_next/data/build-x/gc25/governo/composicao.json", False),
        (ORIGIN + "/_next/data/build-x/gc25/governo/nomeacoes/../x.json", False),
        ("http://portugal.gov.pt/gc25/governo/nomeacoes", False),
        ("https://portugal.gov.pt:443/gc25/governo/nomeacoes", False),
        ("https://portugal.gov.pt.example.org/gc25/governo/nomeacoes", False),
    ],
)
def test_nominations_routes_are_allowlisted(url: str, allowed: bool):
    assert validate_nominations_url(url, "gc25") is allowed


@pytest.mark.django_db
def test_apply_publishes_name_only_staff_with_anchored_gabinetes():
    composition()
    result = apply_snapshot(offline_snapshot())
    assert (result["created"], result["published"], result["gabinetes"]) == (6, 6, 3)
    staff = SourceObservation.objects.filter(category="office_holding")
    assert staff.count() == 6
    assert not staff.filter(identity=None).exists()
    assert not staff.filter(relationship=None).exists()
    assert not staff.filter(subject_name="").exists()
    assert not staff.filter(object=None).exists()
    assert not staff.filter(role="").exists()
    assert not staff.exclude(relationship__status="published").exists()
    assert not staff.exclude(identity__source="scoped_name").exists()
    assert set(
        staff.values_list(
            "kind", "role_class", "temporal_status", "dataset", "source", "effective_end"
        )
    ) == {("public_office", "staff", "unknown", "gov_nomeacoes", "government", None)}
    assert set(staff.values_list("scope", flat=True)) == {
        "nominations:gc25:financas-ficticias",
        "nominations:gc25:primeiro-ministro",
    }
    chief = staff.get(subject_name="Pessoa Exemplo Um")
    assert (chief.role, chief.effective_start, chief.subject_reference) == (
        "Chefe de Gabinete",
        date(2025, 7, 2),
        "dr:900000001",
    )
    assert DR in chief.reference
    term = Term.objects.get(kind="government", code="gc25")
    assert chief.term == term
    unresolved = staff.get(subject_name="Pessoa Exemplo Dois")
    assert unresolved.subject_reference.startswith("nomeacao:")
    assert "dre.pt/web" not in unresolved.reference + unresolved.passage
    misfiled = staff.get(subject_name="Pessoa Exemplo Cinco")
    assert misfiled.term is None
    assert misfiled.effective_start == date(2024, 1, 1)
    assert "fora da vigência" in misfiled.passage
    gabinete = SourceIdentity.objects.get(
        source="government", external_id=f"gabinete:gc25:{slugify(MINISTER)}"
    ).entity
    assert (gabinete.name, gabinete.kind, gabinete.classification) == (
        MINISTER,
        "organisation",
        "government_office",
    )
    assert chief.object == gabinete
    stored = json.dumps(
        [
            list(SourceObservation.objects.values()),
            list(Entity.objects.values()),
            list(Relationship.objects.values()),
        ],
        default=str,
        ensure_ascii=False,
    )
    assert "PAY_CANARY" not in stored
    assert "9 999" not in stored


@pytest.mark.django_db
def test_staff_identity_scope_is_the_government_not_the_subpage():
    snapshot = offline_snapshot()
    first, second = snapshot.pages
    name = first.rows[0].name
    second = replace(second, rows=(replace(second.rows[0], name=name),))
    apply_snapshot(replace(snapshot, pages=(first, second)))
    observations = SourceObservation.objects.filter(category="office_holding", subject_name=name)
    assert observations.count() == 2
    assert observations.values("identity__entity_id").distinct().count() == 1


@pytest.mark.django_db
def test_gabinete_links_only_to_a_single_matching_portfolio():
    composition()
    result = apply_snapshot(offline_snapshot())
    assert (result["linked"], result["ambiguous"], result["unmatched"]) == (2, 1, 0)
    links = Relationship.objects.filter(kind="part_of", subject__classification="government_office")
    assert links.count() == 2
    assert not links.exclude(status="published").exists()
    minister = SourceIdentity.objects.get(external_id=f"gabinete:gc25:{slugify(MINISTER)}")
    portfolio = SourceIdentity.objects.get(external_id=f"portfolio:{guid(301)}").entity
    assert links.get(subject=minister.entity).object == portfolio
    secretary = SourceIdentity.objects.get(external_id=f"gabinete:gc25:{slugify(SECRETARY)}")
    assert not Relationship.objects.filter(subject=secretary.entity, kind="part_of").exists()


@pytest.mark.django_db
def test_without_composition_gabinetes_stay_unlinked():
    result = apply_snapshot(offline_snapshot())
    assert (result["linked"], result["unmatched"]) == (0, 3)
    assert not Relationship.objects.filter(kind="part_of").exists()
    assert Relationship.objects.filter(kind="public_office", status="published").count() == 6


@pytest.mark.django_db
def test_a_subpage_absent_from_a_later_run_ceases_its_staff_offices():
    snapshot = offline_snapshot()
    apply_snapshot(snapshot)
    later = replace(snapshot, pages=snapshot.pages[:1], as_of=DAY + timedelta(days=1))
    result = apply_snapshot(later)
    assert result["ceased"] == 1
    pm_rows = SourceObservation.objects.filter(scope="nominations:gc25:primeiro-ministro")
    assert not pm_rows.filter(is_current=True).exists()
    assert (
        SourceObservation.objects.filter(
            scope="nominations:gc25:financas-ficticias", is_current=True
        ).count()
        == 5
    )


@pytest.mark.django_db
def test_cli_dry_run_writes_nothing_and_apply_publishes_staff(capsys):
    def offline_wire(_self, url: str) -> bytes:
        assert validate_nominations_url(url, "gc25")
        if url == ORIGIN + "/gc25/governo/nomeacoes":
            return index_html()
        return json.dumps(SUBPAGES[url.split("/nomeacoes/")[1].split(".json")[0]]).encode()

    with (
        patch("ligacoes.core.government_nominations._Fetcher.fetch", offline_wire),
        patch("ligacoes.core.government_nominations.timezone.localdate", return_value=DAY),
    ):
        call_command("import_government_nominations", as_of=DAY)
        assert not SourceObservation.objects.exists()
        assert not Entity.objects.exists()
        call_command("import_government_nominations", "--apply", as_of=DAY)
    assert SourceObservation.objects.filter(category="office_holding").count() == 6
    assert Relationship.objects.filter(kind="public_office", status="published").count() == 6
    output = capsys.readouterr().out
    assert "Pessoa Exemplo" not in output
    assert "PAY_CANARY" not in output
