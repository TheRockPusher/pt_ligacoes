from datetime import date
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ligacoes.core.etf_boards import INDEX_URL, SITE, parse_pages, role_class
from ligacoes.core.identity import official_entity
from ligacoes.core.models import Entity, Relationship, SourceObservation

DAY = date(2026, 9, 1)
LATER = date(2026, 9, 15)
COMPANY = "Empresa Fictícia de Testes, S.A."
PDF_URL = f"{SITE}/media/doc/ficticia%20modelo-governo.pdf"
# Layout-mode columns: Cargo | Órgãos Sociais | Eleição | Mandato.
NAME, ELECTION, MANDATE = 26, 72, 100


def line(cargo: str = "", name: str = "", election: str = "", mandate: str = "") -> str:
    text = cargo.ljust(NAME) + name
    text = text.ljust(ELECTION) + election
    return (text.ljust(MANDATE) + mandate).rstrip()


def organ(title: str) -> str:
    return " " * 30 + title


HEADER = line("   Cargo", "      Órgãos Sociais", "Eleição", "Mandato")
REMUNERATION = "\n".join(
    [
        "Estatuto remuneratório fixado",
        "Presidente: Rui Fictício Modelo Teste \u2013 Vencimento mensal de 5.000,00 euros",
        line("   Cargo", "Nome", "Remuneração anual"),
        line("Vogal Executivo", "Nome Fictício Remunerado", "60.000,00 €"),
        "Viatura de serviço atribuída a Rui Fictício Modelo Teste.",
    ]
)


def row_pages(*, resigned_member: bool = True) -> tuple[str, ...]:
    """Row-ordered layout text: merged election cells, wrapped cargos and names."""
    current = [
        "Modelo de Governo",
        "MANDATO 2024-2026",
        "",
        HEADER,
        "",
        organ("Mesa da Assembleia Geral"),
        line("Presidente", "Ana Fictícia Exemplo", "30/09/2024", "2024-2026"),
        line("Secretário", "-", "", "2024-2026"),
        "",
        organ("Conselho de Administração"),
        line(
            "Presidente Executivo", "Rui Fictício Modelo Teste", "Deliberação Unânime", "2024-2026"
        ),
        line("Vogal Não Executivo", "Marta Fictícia Duarte de", "por escrito de", "2024-2026"),
        line("(e Presidente da", "Sousa", "29/08/2024"),
        line("Comissão de Auditoria)"),
    ]
    if resigned_member:
        current.append(line("Vogal Executivo", "Paulo Inventado Nunes (a)", "", "2024-2026"))
    current += [
        "",
        # Vertically centred cells: the first name sits one line above its cargo.
        organ("Conselho Fiscal"),
        line("", "Carla Fictícia Rocha"),
        line("Presidente", "Duarte Inventado Lima", "", "2024-2026"),
        line("Vogal", "Eva Fictícia Matos", "", "2024-2026"),
        line("Vogal Suplente", "", "", "2024-2026"),
        "",
        organ("Revisor Oficial de Contas"),
        line("Efetivo", "Fictícia & Associados, SROC, Lda."),
        "",
    ]
    if resigned_member:
        current.append("(a) Renúncia com efeitos a 15/03/2025.")
    previous = [
        "",
        "MANDATO 2021-2023 \u2013 até 31 de agosto de 2024",
        "",
        HEADER,
        organ("Conselho de Administração"),
        line("Presidente", "Velho Fictício Antunes", "", "2021-2023"),
    ]
    return "\n".join(current + previous), REMUNERATION


COLUMN_PAGES = (
    "\n".join(
        [
            "MANDATO 2024-2026",
            "Cargo",
            "",
            "Órgãos Sociais",
            "",
            "Eleição",
            "",
            "Mandato",
            "Presidente",
            "Vogal Executivo (1)",
            "Presidente",
            "Suplente",
            "Conselho de Administração (*)",
            "Joana Fictícia Almeida",
            "Tiago Inventado Reis",
            "Conselho Fiscal (*)",
            "Sofia Exemplo Castro",
            "Bruno Fictício Pires",
            "2024/2026",
            "2024/2026",
            "2021/2023",
            "2021/2023",
            "Efetivo",
            "Revisor Oficial de Contas",
            "Sociedade Fictícia de Revisores, SROC, Lda",
            "(*) Nomeados por DUE",
        ]
    ),
)


def summary(pages: tuple[str, ...]) -> list[tuple[str, str, str, tuple[int, int] | None]]:
    return [(row.organ, row.cargo, row.name, row.years) for row in parse_pages(pages).rows]


def test_row_ordered_tables_pair_cells_per_organ_and_stop_at_remuneration():
    document = parse_pages(row_pages())
    assert summary(row_pages()) == [
        ("Mesa da Assembleia Geral", "Presidente", "Ana Fictícia Exemplo", (2024, 2026)),
        (
            "Conselho de Administração",
            "Presidente Executivo",
            "Rui Fictício Modelo Teste",
            (2024, 2026),
        ),
        (
            "Conselho de Administração",
            "Vogal Não Executivo (e Presidente da Comissão de Auditoria)",
            "Marta Fictícia Duarte de Sousa",
            (2024, 2026),
        ),
        ("Conselho de Administração", "Vogal Executivo", "Paulo Inventado Nunes", (2024, 2026)),
        ("Conselho Fiscal", "Presidente", "Carla Fictícia Rocha", (2024, 2026)),
        ("Conselho Fiscal", "Vogal", "Duarte Inventado Lima", (2024, 2026)),
        ("Conselho Fiscal", "Vogal Suplente", "Eva Fictícia Matos", (2024, 2026)),
        ("Conselho de Administração", "Presidente", "Velho Fictício Antunes", (2021, 2023)),
    ]
    # The auditing firm and the vacant seat pair with their cargos but are not members.
    assert (document.firms, document.vacant, document.unparseable) == (1, 1, 0)
    rows = {row.name: row for row in document.rows}
    assert rows["Ana Fictícia Exemplo"].appointed_on == date(2024, 9, 30)
    assert rows["Rui Fictício Modelo Teste"].act == "Deliberação Unânime por escrito de 29/08/2024"
    assert rows["Paulo Inventado Nunes"].resigned_on == date(2025, 3, 15)
    assert rows["Paulo Inventado Nunes"].ended
    assert not rows["Rui Fictício Modelo Teste"].ended
    # A mandate the document marks as past ("até …") and a later one supersede it.
    assert rows["Velho Fictício Antunes"].ended


def test_column_ordered_extraction_is_distributed_in_order_across_organs():
    document = parse_pages(COLUMN_PAGES)
    assert summary(COLUMN_PAGES) == [
        ("Conselho de Administração", "Presidente", "Joana Fictícia Almeida", (2024, 2026)),
        ("Conselho de Administração", "Vogal Executivo", "Tiago Inventado Reis", (2024, 2026)),
        ("Conselho Fiscal", "Presidente", "Sofia Exemplo Castro", (2021, 2023)),
        ("Conselho Fiscal", "Suplente", "Bruno Fictício Pires", (2021, 2023)),
    ]
    assert document.firms == 1
    assert {row.act for row in document.rows} == {"DUE"}


def test_table_whose_cells_do_not_pair_is_skipped_and_counted():
    pages = (
        "\n".join(
            [
                HEADER,
                organ("Conselho de Administração"),
                line("Presidente", "Nome Fictício Isolado", "", "2024-2026"),
                line("Vogal", "", "", "2024-2026"),
                line("Vogal", "", "", "2024-2026"),
            ]
        ),
    )
    document = parse_pages(pages)
    assert (document.rows, document.tables, document.unparseable) == ((), 1, 1)


@pytest.mark.parametrize(
    ("cargo", "expected"),
    [
        ("Presidente não Executivo", Relationship.RoleClass.LEADERSHIP),
        ("Vice-Presidente Executivo", Relationship.RoleClass.DEPUTY_LEADERSHIP),
        ("Vogal Executivo", Relationship.RoleClass.MEMBER),
        ("Administrador", Relationship.RoleClass.MEMBER),
        ("Vogal Suplente", Relationship.RoleClass.SUBSTITUTE),
        ("Assessor", Relationship.RoleClass.OTHER),
    ],
)
def test_role_class_follows_the_cargo_wording(cargo, expected):
    assert role_class(cargo) == expected


def index_html(slugs: tuple[str, ...]) -> bytes:
    rows = {
        "empresa-ficticia": COMPANY,
        "fundo-ficticio": "Fundo Fictício",
    }
    table = "".join(
        f'<tr><td><a href="/{slug}" target="_blank">{rows[slug]}</a></td>'
        "<td>Empresas Públicas não Financeiras</td><td>Outros</td></tr>"
        for slug in slugs
    )
    return (
        '<html><body><a href="/avisos-legais">Avisos legais</a>'
        f"<table><tbody>{table}</tbody></table></body></html>"
    ).encode()


COMPANY_PAGE = (
    "<p><strong>Documentos Associados</strong></p>"
    '<a target="_blank" href="/media/doc/ficticia-principios-bom-governo.pdf">'
    "Príncipios de Bom Governo</a><br>"
    '<a target="_blank" href="/media/doc/ficticia modelo-governo.pdf">'
    "Modelo de governo/Membros dos órgãos sociais </a>"
).encode()
# A governance link off the allowed routes is never followed.
FUND_PAGE = (
    '<a href="https://example.org/modelo.pdf">Modelo de governo/Membros dos órgãos sociais</a>'
).encode()


class FakeEtf:
    """The ETF site over fictional pages; PDFs stand for the extracted texts below."""

    def __init__(self, *, slugs: tuple[str, ...], pages: tuple[str, ...]) -> None:
        self.slugs = slugs
        self.pages = pages

    def __call__(self, url: str, **request: object) -> bytes:
        allowed = request["allowed"]
        assert callable(allowed) and allowed(url)
        documents = {
            INDEX_URL: index_html(self.slugs),
            f"{SITE}/empresa-ficticia": COMPANY_PAGE,
            f"{SITE}/fundo-ficticio": FUND_PAGE,
            PDF_URL: b"%PDF-ficticio",
        }
        return documents[url]

    def text(self, content: bytes, *, layout: bool) -> tuple[str, ...]:
        assert content == b"%PDF-ficticio"
        return self.pages


def run(fake: FakeEtf, *, as_of: date = DAY, apply: bool = True, limit: int = 0) -> str:
    output = StringIO()
    arguments = ["--apply"] if apply else []
    if limit:
        arguments += ["--limit", str(limit)]
    with (
        patch("ligacoes.core.etf_boards.download", side_effect=fake),
        patch("ligacoes.core.etf_boards.pdf_text", side_effect=fake.text),
        patch("ligacoes.core.etf_boards.PAUSE", 0),
    ):
        call_command("import_etf_boards", *arguments, as_of=as_of, stdout=output)
    return output.getvalue()


def member(name: str) -> SourceObservation:
    return SourceObservation.objects.get(source="etf", subject_name=name, is_current=True)


BOTH = ("empresa-ficticia", "fundo-ficticio")


@pytest.mark.django_db
def test_board_members_publish_with_scoped_people_and_year_precision():
    output = run(FakeEtf(slugs=BOTH, pages=row_pages()), apply=False)
    assert "Sem escritas" in output
    assert "1 ligações fora das rotas autorizadas" in output
    assert not SourceObservation.objects.exists()

    run(FakeEtf(slugs=BOTH, pages=row_pages()))
    candidates = SourceObservation.objects.filter(source="etf")
    assert candidates.count() == 8
    assert Relationship.objects.filter(status="published").count() == 8
    assert Entity.objects.filter(kind="person").count() == 8
    for candidate in candidates:
        assert candidate.identity is not None
        assert candidate.relationship is not None
        assert candidate.evidence is not None
        assert candidate.identity.source == "scoped_name"
        assert candidate.object is not None
        assert candidate.relationship.status == "published"
        assert candidate.evidence.is_public and candidate.evidence.source.is_public
        assert candidate.evidence.excerpt == candidate.passage
        assert candidate.relationship.object == candidate.object
        assert candidate.scope == "etf:empresa-ficticia"
        assert candidate.subject_reference.startswith("etf:empresa-ficticia:")
        assert (candidate.category, candidate.kind, candidate.dataset) == (
            "office_holding",
            "directorship",
            "etf_orgaos_sociais",
        )
        assert (candidate.object_name, candidate.source_url) == (COMPANY, PDF_URL)
        # Remuneration, cars and the auditing firm never reach a candidate.
        assert "euros" not in candidate.passage and "Viatura" not in candidate.passage
    assert not candidates.filter(subject_name__contains="Remunerado").exists()
    assert not candidates.filter(subject_name__contains="SROC").exists()

    president = member("Rui Fictício Modelo Teste")
    assert (president.role, president.role_class) == (
        "Presidente Executivo — Conselho de Administração",
        "leadership",
    )
    assert (president.effective_start, president.start_precision) == (date(2024, 1, 1), "year")
    assert (president.effective_end, president.end_precision) == (date(2026, 12, 31), "year")
    assert president.temporal_status == "unknown"
    assert "Designação: Deliberação Unânime por escrito de 29/08/2024." in president.passage
    assert president.reference == (
        "ETF empresa-ficticia / p. 1 / Conselho de Administração / linha 1"
    )

    resigned = member("Paulo Inventado Nunes")
    assert (resigned.effective_end, resigned.end_precision, resigned.temporal_status) == (
        date(2025, 3, 15),
        "day",
        "ended",
    )
    previous = member("Velho Fictício Antunes")
    assert (previous.effective_end, previous.temporal_status) == (date(2023, 12, 31), "ended")
    assert member("Eva Fictícia Matos").role_class == "substitute"


@pytest.mark.django_db
def test_removed_rows_and_companies_cease_but_a_limited_run_ceases_nothing():
    run(FakeEtf(slugs=BOTH, pages=row_pages()))
    resigned = member("Paulo Inventado Nunes")

    run(FakeEtf(slugs=BOTH, pages=row_pages(resigned_member=False)), as_of=LATER)
    resigned.refresh_from_db()
    assert not resigned.is_current
    assert member("Rui Fictício Modelo Teste").as_of == LATER

    # A limited run reads only the first listed company and treats nothing as absent.
    output = run(FakeEtf(slugs=("fundo-ficticio", *BOTH[:1]), pages=()), as_of=LATER, limit=1)
    assert "recolha parcial" in output
    assert SourceObservation.objects.filter(source="etf", is_current=True).count() == 7

    run(FakeEtf(slugs=("fundo-ficticio",), pages=()), as_of=LATER)
    assert not SourceObservation.objects.filter(source="etf", is_current=True).exists()


@pytest.mark.django_db
def test_board_role_reuses_an_identifier_anchored_company():
    company = official_entity(
        "nipc", "601234561", name=COMPANY, kind="company", classification="state_company"
    )
    run(FakeEtf(slugs=BOTH, pages=row_pages()))
    observation = member("Rui Fictício Modelo Teste")
    assert observation.relationship is not None
    assert observation.identity is not None
    assert observation.object == company
    assert observation.relationship.object == company
    assert observation.relationship.subject == observation.identity.entity
    assert observation.identity.source == "scoped_name"
    assert observation.relationship.status == "published"
