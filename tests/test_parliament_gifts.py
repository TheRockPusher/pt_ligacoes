from datetime import date
from decimal import Decimal
from html import escape
from urllib.parse import parse_qs

import pytest

from ligacoes.core.models import Event, EventParty, SourceIdentity
from ligacoes.core.official_http import OfficialHTTPError
from ligacoes.core.parliament_gifts import (
    Deputy,
    GiftsImportError,
    GiftsSnapshot,
    _Collector,
    allowed,
    apply_snapshot,
    enumerate_deputies,
    fetch_snapshot,
    parse_day,
    parse_register,
)

PREFIX = "ctl00$ctl51$g_ficticio$ctl00$"
LIST = "https://www.parlamento.pt/RegistoDeslocacoesHospitalidades/Paginas/ROfertasDHList.aspx"
DEPUTY = Deputy("900001", "Deputada Fictícia")

GIFT = {
    "Descricao": "Livro fictício ",
    "Valor": "1.250,50 €",
    "Ofertante": "Embaixada Imaginária",
    "Data": "05/03/2024",
    "Decisao": "Integração no património da A.R.",
}
TRAVEL = {
    "Descricao": "Viagem fictícia",
    "Local": "Lisboa-Porto",
    "Ofertante": "-",
    "Representacao": "AR",
    "Data": "12/04/2024",
    "Duracao": "2 dias",
}


def register(sections: dict[str, list[dict[str, str]]]) -> bytes:
    spans = ['<span id="ctl00_ctl51_g_x_ctl00_onome"> - Deputada Fictícia</span>']
    for section, rows in sections.items():
        for index, row in enumerate(rows):
            for field, value in row.items():
                spans.append(
                    f'<span id="ctl00_ctl51_g_x_ctl00_{section}_ctl{index:02d}_lbl{field}" '
                    f'class="TextoRegular">{escape(value)}</span>'
                )
    return f"<html><body>{''.join(spans)}</body></html>".encode()


def gifts_snapshot(sections: dict[str, list[dict[str, str]]], as_of: date) -> GiftsSnapshot:
    return GiftsSnapshot("XVII", as_of, (parse_register(register(sections), DEPUTY, "XVII"),))


def test_register_dates_are_day_month_year():
    assert parse_day(" 05/03/2024 ") == date(2024, 3, 5)
    assert parse_day("-") is None
    for invalid in ("2024-03-05", "31/02/2024", "5/3/2024"):
        with pytest.raises(GiftsImportError):
            parse_day(invalid)


def test_declared_value_is_an_amount_only_when_it_is_a_plain_number():
    odd = dict(GIFT, Valor="cerca de 50 €")
    page = parse_register(register({"rptOfertas": [GIFT, odd]}), DEPUTY, "XVII")

    plain, text = sorted(page.entries, key=lambda entry: entry.amount is None)
    assert plain.amount == Decimal("1250.50")
    assert "valor" not in plain.details
    assert text.amount is None
    assert text.details["valor"] == "cerca de 50 €"
    assert plain.details["destino_final"] == "Integração no património da A.R."


@pytest.mark.django_db
def test_unresolved_provider_keeps_gift_private_and_travel_without_provider_publishes():
    apply_snapshot(
        gifts_snapshot({"rptOfertas": [GIFT], "rptHDeslocacoes": [TRAVEL]}, date(2025, 1, 1))
    )

    deputy = SourceIdentity.objects.get(source="parliament", external_id="900001").entity
    gift = Event.objects.get(kind=Event.Kind.GIFT)
    assert gift.status == Event.Status.DRAFT
    assert gift.title == "Livro fictício"
    assert gift.date == date(2024, 3, 5)
    assert (gift.amount, gift.amount_label) == (Decimal("1250.50"), "Valor declarado")
    assert gift.record_url.endswith("/RDH.aspx?BID=900001&lg=XVII")
    assert {(p.role, p.name, p.entity_id) for p in gift.parties.all()} == {
        (EventParty.Role.RECIPIENT, "Deputada Fictícia", deputy.pk),
        (EventParty.Role.PROVIDER, "Embaixada Imaginária", None),
    }
    travel = Event.objects.get(kind=Event.Kind.TRAVEL)
    assert travel.status == Event.Status.PUBLISHED
    assert travel.details == {"local": "Lisboa-Porto", "duracao": "2 dias", "representacao": "AR"}
    assert list(travel.parties.values_list("entity", flat=True)) == [deputy.pk]


@pytest.mark.django_db
def test_entry_removed_from_the_register_ceases():
    apply_snapshot(
        gifts_snapshot({"rptOfertas": [GIFT], "rptHDeslocacoes": [TRAVEL]}, date(2025, 1, 1))
    )

    apply_snapshot(gifts_snapshot({"rptHDeslocacoes": [TRAVEL]}, date(2025, 1, 2)))

    assert Event.objects.get(kind=Event.Kind.GIFT).status == Event.Status.CEASED
    assert Event.objects.get(kind=Event.Kind.TRAVEL).status == Event.Status.PUBLISHED


def list_page(legislature: str, ids: range, total: int, next_page: str | None) -> bytes:
    rows = "".join(
        f'<a href="/DeputadoGP/Paginas/Biografia.aspx?BID={cadastro}">Pessoa {cadastro}</a>'
        f'<span id="x_lblGP">FIC</span>'
        f'<a href="/RegistoDeslocacoesHospitalidades/Paginas/RDH.aspx?BID={cadastro}'
        f'&amp;lg={legislature}">[ver...]</a>'
        for cadastro in ids
    )
    pager = (
        f'<a href="javascript:__doPostBack(&#39;{PREFIX}dpgResultsByDate$ctl00$ctl01'
        f'&#39;,&#39;&#39;)">{next_page}</a>'
        if next_page
        else ""
    )
    return (
        '<form><input type="hidden" name="__VIEWSTATE" value="estado"/>'
        f'<input type="text" name="{PREFIX}txtNome" value=""/>'
        f'<select name="{PREFIX}ddlLegislatura"><option value="XVII">XVII</option></select>'
        f'<input type="submit" name="{PREFIX}btnDepActual" value="Pesquisar"/>'
        f'<span id="x_lblResultsByDate">Deputados na {legislature} Legislatura, nome . '
        f"[{total} registo(s)]</span>{rows}{pager}</form>"
    ).encode()


def test_deputies_are_enumerated_across_postback_pages():
    posts: list[dict[str, list[str]]] = []

    def fetch(url: str, **kwargs) -> bytes:
        assert url == LIST and kwargs["allowed"](url)
        if kwargs.get("method") != "POST":
            return list_page("XVII", range(1, 11), 10, None)
        form = parse_qs(kwargs["body"].decode(), keep_blank_values=True)
        posts.append(form)
        if form["__EVENTTARGET"] == [""]:
            return list_page("XV", range(100, 120), 21, "2")
        return list_page("XV", range(120, 121), 21, None)

    deputies = enumerate_deputies(_Collector(fetch, sleep=lambda _: None), "XV")

    assert [deputy.cadastro_id for deputy in deputies] == [str(n) for n in range(100, 121)]
    assert posts[0][f"{PREFIX}btnDepActual"] == ["Pesquisar"]
    assert posts[1]["__EVENTTARGET"] == [f"{PREFIX}dpgResultsByDate$ctl00$ctl01"]
    assert f"{PREFIX}btnDepActual" not in posts[1]
    assert all(form[f"{PREFIX}ddlLegislatura"] == ["XV"] for form in posts)


def test_incomplete_enumeration_is_rejected():
    def fetch(url: str, **kwargs) -> bytes:
        return list_page("XV", range(100, 110), 25, None)

    with pytest.raises(GiftsImportError):
        enumerate_deputies(_Collector(fetch, sleep=lambda _: None), "XV")


def test_overloaded_server_answers_are_retried_until_a_valid_page():
    # Observed under load: dropped connections and a 200 page without the results label.
    answers: list[bytes | Exception] = [
        list_page("XVII", range(1, 11), 10, None),
        OfficialHTTPError("A fonte oficial devolveu HTTP 503."),
        b"<html><body>Sorry, something went wrong</body></html>",
        list_page("XV", range(100, 102), 2, None),
        OfficialHTTPError("Não foi possível obter a fonte oficial em segurança."),
        register({"rptHDeslocacoes": [TRAVEL]}),
        register({}),
    ]
    waits: list[float] = []

    def fetch(url: str, **kwargs) -> bytes:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    snapshot = fetch_snapshot(
        legislature="XV", as_of=date(2025, 1, 1), collector=_Collector(fetch, sleep=waits.append)
    )

    assert [len(page.entries) for page in snapshot.pages] == [1, 0]
    assert not answers
    # Normal spacing between requests; growing back-off only before retries.
    assert waits == [1.0, 5.0, 10.0, 1.0, 5.0, 1.0]


def test_persistent_failure_names_the_step_without_payload():
    def fetch(url: str, **kwargs) -> bytes:
        raise OfficialHTTPError("A fonte oficial devolveu HTTP 503.")

    with pytest.raises(GiftsImportError, match=r"list form; 4 attempts; .*HTTP 503"):
        enumerate_deputies(_Collector(fetch, sleep=lambda _: None), "XV")


def test_only_the_list_and_register_routes_are_fetched():
    base = "https://www.parlamento.pt/RegistoDeslocacoesHospitalidades/Paginas/"
    assert allowed(LIST)
    assert allowed(f"{base}RDH.aspx?BID=900001&lg=XVII")
    for url in (
        f"{base}RDH.aspx?BID=900001&lg=XIII",
        f"{base}RDH.aspx?BID=900001&lg=XVII&x=1",
        f"{base}Outra.aspx",
        f"{LIST}?x=1",
        f"http://www.parlamento.pt{LIST[len('https://www.parlamento.pt') :]}",
        "https://example.org/RegistoDeslocacoesHospitalidades/Paginas/RDH.aspx?BID=1&lg=XV",
    ):
        assert not allowed(url)
