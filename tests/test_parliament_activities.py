import json
from datetime import date
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pytest

from ligacoes.core.identity import official_entity
from ligacoes.core.models import (
    Entity,
    Event,
    EventParty,
    IdentityScheme,
    SourceObservation,
)
from ligacoes.core.parliament_activities import (
    apply_snapshot,
    build_snapshot,
    download_with_retries,
)
from ligacoes.core.parliament_fetch import (
    FILE_WAIT,
    TIMEOUT,
    Download,
    ParliamentImportError,
    fetch_url,
)
from ligacoes.core.parliament_parse import JSONObject, JSONValue

AS_OF = date(2025, 5, 1)


def file_url(prefix: str, code: str) -> str:
    query = urlencode({"path": "ficticio", "fich": f"{prefix}{code}_json.txt", "Inline": "true"})
    return f"https://app.parlamento.pt/webutils/docs/doc.txt?{query}"


def committee(organ_id: int, name: str) -> JSONObject:
    return {
        "DetalheOrgao": {
            "idOrgao": float(organ_id),
            "nomeSigla": f"{name} ",
            "numeroOrgao": 1.0,
            "siglaLegislatura": "XVI",
            "siglaOrgao": "CO",
        },
        "HistoricoComposicao": [],
        "Reunioes": [],
    }


def bodies() -> Download:
    value: JSONObject = {
        "Comissoes": [
            committee(9001, "Comissão de Orçamento Fictício"),
            committee(9002, "Comissão de Assuntos Fictícios"),
            # Same initials as each other: an acronym matching both stays unmapped.
            committee(9003, "Comissão de Mares e Rios"),
            committee(9004, "Comissão de Montes e Relvas"),
        ]
    }
    return Download(json.dumps(value).encode(), file_url("OrgaoComposicao", "XVI"))


def hearing(number: int, acronym: str, entities: str | None) -> JSONObject:
    return {
        "IDAudicao": float(number),
        "NumeroAudicao": f"{number}-{acronym}-XVI",
        "Assunto": f"Assunto fictício {number}",
        "Data": "2024-06-12",
        "Entidades": entities,
        "Legislatura": "XVI",
        "SessaoLegislativa": "1",
        "Documentos": None,
        "Links": None,
    }


def activities(hearings: list[JSONObject], audiences: list[JSONObject] | None = None) -> Download:
    elected: list[JSONValue] = [
        {"cargo": "Efetivo", "nome": "Deputada Inventada Exemplo (FIC)"},
        {"cargo": "Suplente", "nome": "Pessoa Externa Imaginária"},
    ]
    value: JSONObject = {
        "AtividadesGerais": {
            "Atividades": [
                {
                    "Tipo": "OEX",
                    "Assunto": "Eleição de membros do Conselho Fictício",
                    "OrgaoExterior": "Conselho Fictício de Fiscalização",
                    "DataEntrada": "2024-05-02",
                    "DataAgendamentoDebate": "2024-05-10",
                    "Eleitos": elected,
                    "Publicacao": [
                        {
                            "pubTipo": "DAR II série B",
                            "pubNr": "12",
                            "pubdt": "2024-05-15",
                            "URLDiario": "https://debates.parlamento.pt/catalogo/ficticio/12",
                        }
                    ],
                },
                {
                    "Tipo": "OEX",
                    "Assunto": "Renúncia de membro do Conselho Fictício",
                    "OrgaoExterior": "Conselho Fictício de Fiscalização",
                    "DataEntrada": "2024-06-02",
                    "Eleitos": [{"cargo": None, "nome": "Pessoa Que Renuncia"}],
                    "Publicacao": None,
                },
            ],
            # Report pairs (the acronym is not the name's initials); 8801 is another legislature's id.
            "Relatorios": [
                {"ParecerComissao": [{"Sigla": "CORC", "Id": "9001", "Nome": "Orçamento"}]},
                {"ParecerComissao": [{"Sigla": "CORC", "Id": "8801", "Nome": "Orçamento"}]},
            ],
        },
        "Audicoes": list[JSONValue](hearings),
        "Audiencias": list[JSONValue](audiences or []),
    }
    return Download(json.dumps(value).encode(), file_url("Atividades", "XVI"))


def snapshot(hearings: list[JSONObject], *, as_of: date = AS_OF, **kwargs):
    return build_snapshot(activities(hearings, **kwargs), bodies(), legislature="XVI", as_of=as_of)


def test_committee_acronyms_map_to_this_legislatures_organs():
    result = snapshot(
        [
            hearing(1, "CORC", "Associação Fictícia"),
            # Case differs between legislatures; the stray space occurs upstream.
            hearing(2, "CAf ", "Associação Fictícia"),
            hearing(3, "CMR", "Associação Fictícia"),
        ]
    )

    assert result.committees["corc"] == ("orgao:XVI:9001", "Comissão de Orçamento Fictício")
    assert result.committees["caf"] == ("orgao:XVI:9002", "Comissão de Assuntos Fictícios")
    assert "cmr" not in result.committees
    assert result.unmapped_committees == ("cmr",)


@pytest.mark.django_db
def test_hearings_stay_private_while_attendees_are_unresolved():
    organ = official_entity(
        IdentityScheme.PARLIAMENT,
        "orgao:XVI:9001",
        name="Comissão de Orçamento Fictício",
        kind=Entity.Kind.ORGANISATION,
        classification=Entity.Classification.PARLIAMENTARY_COMMITTEE,
    )
    not_held: JSONObject = {
        "IDAudiencia": 77.0,
        "NumeroAudiencia": "4-CORC-XVI",
        "Assunto": "Pedido fictício",
        "Data": "2024-07-01",
        "Entidades": "Associação Fictícia",
        "Concedida": "Não realizada",
        "Legislatura": "XVI",
    }

    apply_snapshot(
        snapshot(
            [
                hearing(1, "CORC", "Associação Fictícia; Ministra Inventada ;"),
                hearing(2, "CORC", "Peticionários"),
                hearing(3, "CMR", None),
            ],
            audiences=[not_held],
        )
    )

    attended = Event.objects.get(record_id="audicao:1")
    assert attended.status == Event.Status.DRAFT
    assert attended.details == {"number": "1-CORC-XVI", "type": "Audição"}
    assert {(p.role, p.name, p.entity_id) for p in attended.parties.all()} == {
        (EventParty.Role.HOST, "Comissão de Orçamento Fictício", organ.pk),
        (EventParty.Role.ATTENDEE, "Associação Fictícia", None),
        (EventParty.Role.ATTENDEE, "Ministra Inventada", None),
    }
    # Only the anchored committee remains once collective placeholders are dropped.
    petitioners = Event.objects.get(record_id="audicao:2")
    assert petitioners.status == Event.Status.PUBLISHED
    assert list(petitioners.parties.values_list("role", flat=True)) == [EventParty.Role.HOST]
    # An unmapped committee keeps its published acronym and no entity.
    unmapped = Event.objects.get(record_id="audicao:3")
    assert unmapped.status == Event.Status.DRAFT
    assert list(unmapped.parties.values_list("name", "entity")) == [("CMR", None)]
    assert not Event.objects.filter(record_id="audiencia:77").exists()


@pytest.mark.django_db
def test_hearing_absent_from_a_later_file_ceases():
    apply_snapshot(snapshot([hearing(1, "CORC", "A"), hearing(2, "CORC", "B")]))

    apply_snapshot(snapshot([hearing(1, "CORC", "A")], as_of=date(2025, 5, 2)))

    assert Event.objects.get(record_id="audicao:1").status == Event.Status.DRAFT
    assert Event.objects.get(record_id="audicao:2").status == Event.Status.CEASED


@pytest.mark.django_db
def test_external_body_elections_are_private_candidates_without_group_suffix():
    apply_snapshot(snapshot([hearing(1, "CORC", "A")]))

    rows = SourceObservation.objects.filter(dataset="ar_atividades", is_current=True)
    assert sorted(rows.values_list("subject_name", "role")) == [
        ("Deputada Inventada Exemplo", "Efetivo"),
        ("Pessoa Externa Imaginária", "Suplente"),
    ]
    for row in rows:
        assert row.identity is None
        assert row.relationship_id is None
        assert row.object is None
        assert row.object_name == "Conselho Fictício de Fiscalização"
        assert row.effective_start == date(2024, 5, 10)
        assert row.source_url == "https://debates.parlamento.pt/catalogo/ficticio/12"
        assert "FIC)" not in row.passage and "(FIC" not in row.subject_name
        assert row.subject_reference.startswith("oex:XVI:")
    # Resignation notices are not elections.
    assert not rows.filter(subject_name="Pessoa Que Renuncia").exists()


def test_first_period_files_of_the_i_legislatura_carry_code_i():
    composition = Download(
        json.dumps({"Comissoes": [committee(9001, "Comissão de Orçamento Fictício")]})
        .replace('"XVI"', '"I"')
        .encode(),
        file_url("OrgaoComposicao", "IA"),
    )
    audition: JSONValue = hearing(1, "COF", "Associação Fictícia") | {"NumeroAudicao": "1-COF-I"}
    empty: JSONObject = {
        "AtividadesGerais": {"Atividades": [], "Relatorios": []},
        "Audicoes": [audition],
    }
    result = build_snapshot(
        Download(json.dumps(empty).encode(), file_url("Atividades", "IA")),
        composition,
        legislature="IA",
        as_of=AS_OF,
    )

    assert result.committees == {"cof": ("orgao:IA:9001", "Comissão de Orçamento Fictício")}
    # Another legislature's composition file is still rejected.
    with pytest.raises(ParliamentImportError, match="does not match"):
        build_snapshot(
            Download(json.dumps(empty).encode(), file_url("Atividades", "IB")),
            Download(bodies().content, file_url("OrgaoComposicao", "IB")),
            legislature="IB",
            as_of=AS_OF,
        )


def test_generated_file_may_take_longer_than_a_read_before_its_first_byte():
    events: list[tuple[str, float]] = []

    def connection() -> Mock:
        sock = Mock()
        sock.settimeout.side_effect = lambda value: events.append(("timeout", value))
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: default
        response.read1.side_effect = [b"{}", b""]

        def getresponse() -> Mock:
            events.append(("headers", 0))
            return response

        return Mock(sock=sock, getresponse=getresponse)

    with patch(
        "ligacoes.core.parliament_fetch._PinnedHTTPSConnection",
        side_effect=lambda *args, **kwargs: connection(),
    ):
        fetch_url(file_url("Atividades", "XIII"), "activities", "XIII")
        kind, header_wait = events[events.index(("headers", 0)) - 1]
        assert kind == "timeout" and TIMEOUT < header_wait <= FILE_WAIT
        # Body chunks keep the short per-read timeout.
        assert events[-1] == ("timeout", TIMEOUT)
        events.clear()
        fetch_url(
            "https://www.parlamento.pt/Cidadania/Paginas/DAatividades.aspx", "activities", "XIII"
        )
        assert events[0] == ("headers", 0)


def test_only_network_failures_of_a_download_are_retried():
    timeout = ParliamentImportError("Could not retrieve the official source safely.")
    timeout.__cause__ = TimeoutError("The read operation timed out")
    download = Download(b"{}", file_url("Atividades", "XIII"))
    waits: list[float] = []
    target = "ligacoes.core.parliament_activities.discover_download"

    with patch(target, side_effect=[timeout, timeout, download]):
        assert download_with_retries("activities", "XIII", sleep=waits.append) is download
    assert waits == [10.0, 20.0]

    invalid = ParliamentImportError("Official catalogue has no unique JSON download.")
    with (
        patch(target, side_effect=[invalid, download]),
        pytest.raises(ParliamentImportError, match="no unique"),
    ):
        download_with_retries("activities", "XIII", sleep=waits.append)
    with (
        patch(target, side_effect=[timeout, timeout, timeout]),
        pytest.raises(ParliamentImportError, match="safely"),
    ):
        download_with_retries("activities", "XIII", sleep=lambda _: None)
