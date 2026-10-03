"""Registry of the official datasets behind every imported claim and event.

Importers take `Source` titles, publishers and URLs from `DATASETS`, so one
dataset always appears under one name. The public sources page lists the same
entries, including datasets that are planned (`upcoming`) or used only as
identifier hints (`secondary`). Reuse terms are stated as published by each
body; the project is non-profit, so non-commercial clauses are met.

`IDENTIFIER_SCHEMES` holds display metadata for `IdentityScheme` values: a
label, an optional public record URL template and whether the identifier may
be shown publicly. Internal ids (Government portal, EpT) are never shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

type DatasetStatus = Literal["imported", "upcoming", "secondary"]
type DatasetPhase = Literal["existing", "A", "B", "C", "D", "E"]


@dataclass(frozen=True)
class Dataset:
    key: str
    title: str
    publisher: str
    url: str
    licence: str
    licence_url: str
    description: str
    identifiers: tuple[str, ...]
    status: DatasetStatus
    phase: DatasetPhase


@dataclass(frozen=True)
class SchemeInfo:
    label: str
    url_template: str | None
    public: bool


AR = "Assembleia da República"
AR_LICENCE = (
    "Reutilização livre por qualquer pessoa ou instituição, "
    "devendo ser mencionada a fonte (Assembleia da República)."
)
AR_LICENCE_URL = "https://www.parlamento.pt/Cidadania/paginas/dadosabertos.aspx"
GOV = "Governo da República Portuguesa"
GOV_LICENCE = "Sem licença de reutilização publicada; informação de publicação legal obrigatória."
EPT = "Entidade para a Transparência"
EPT_LICENCE = (
    "Sem licença de reutilização publicada; só o registo de interesses é de acesso "
    "público (Lei n.º 52/2019, art. 17.º), não sendo reproduzidos rendimentos nem património."
)
EPT_URL = "https://entidadetransparencia.pt/"
DADOS_PD = "Domínio público (dados.gov.pt: «Outra (Domínio Público)»)."
NOT_SPECIFIED = "Licença não especificada no dados.gov.pt; dados de publicação obrigatória."
EP = "Parlamento Europeu"
EP_LICENCE = (
    "Reutilização autorizada para fins comerciais ou não comerciais, "
    "desde que reproduzida integralmente e citada a fonte."
)
EP_LICENCE_URL = "https://www.europarl.europa.eu/legal-notice/"
EC_REUSE = "Aviso de reutilização da Comissão Europeia (Decisão 2011/833/UE), com citação da fonte."


def _ar_page(name: str) -> str:
    return f"https://www.parlamento.pt/Cidadania/Paginas/{name}.aspx"


_DATASETS: tuple[Dataset, ...] = (
    # Existing sources (URLs as used by parliament_fetch, government and interests).
    Dataset(
        key="ar_informacao_base",
        title="Assembleia da República — Informação de Base",
        publisher=AR,
        url=_ar_page("DAInformacaoBase"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Deputados de cada legislatura e respetivos mandatos e grupos parlamentares. "
            "Liga cada deputado à Assembleia da República e ao seu grupo parlamentar."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="existing",
    ),
    Dataset(
        key="ar_registo_biografico",
        title="Assembleia da República — Registo Biográfico",
        publisher=AR,
        url=_ar_page("DARegistoBiografico"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Cargos e funções declarados nas biografias oficiais dos deputados. "
            "Publica ligações entre deputados e organizações quando a fonte identifica o cargo e a entidade."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="existing",
    ),
    Dataset(
        key="gov_composicao",
        title="Governo — Composição",
        publisher=GOV,
        url="https://portugal.gov.pt/gc25/governo/composicao",
        licence=GOV_LICENCE,
        licence_url="",
        description=(
            "Membros do Governo e respetivas pastas, com datas de posse e cessação. "
            "Liga cada membro à área governativa que tutela."
        ),
        identifiers=("government",),
        status="imported",
        phase="existing",
    ),
    Dataset(
        key="gov_arquivo_historico",
        title="Governo — Arquivo histórico",
        publisher=GOV,
        url=(
            "https://www.historico.portugal.gov.pt/pt/o-governo/"
            "arquivo-historico/governos-constitucionais.aspx"
        ),
        licence=GOV_LICENCE,
        licence_url="",
        description=(
            "Composições dos Governos Constitucionais I a XX e todas as versões datadas "
            "publicadas no arquivo oficial. Liga membros aos Governos, sem inferir "
            "datas individuais de posse ou cessação."
        ),
        identifiers=("government",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="ept_declaracoes",
        title="Entidade para a Transparência — Declarações únicas (registo de interesses)",
        publisher=EPT,
        url=EPT_URL,
        licence=EPT_LICENCE,
        licence_url="",
        description=(
            "Atividades, cargos e participações sociais declarados no registo de interesses. "
            "Publica ligações declaradas pelo titular, sem verificação independente; identifica organizações pelo NIPC quando existe."
        ),
        identifiers=("ept", "nipc"),
        status="imported",
        phase="existing",
    ),
    # Phase A.
    Dataset(
        key="ar_composicao_orgaos",
        title="Assembleia da República — Composição de Órgãos",
        publisher=AR,
        url=_ar_page("DAComposicaoOrgaos"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Composição datada de comissões, subcomissões, grupos de trabalho, Mesa e "
            "Conferência de Líderes. Liga deputados aos órgãos parlamentares e respetivos cargos."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="ar_atividade_deputados",
        title="Assembleia da República — Atividade dos Deputados",
        publisher=AR,
        url=_ar_page("DAatividadeDeputado"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Participação de cada deputado em grupos de amizade e delegações parlamentares. "
            "Fornece as datas de pertença que complementam a composição dos órgãos."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="ar_delegacoes_permanentes",
        title="Assembleia da República — Delegações Permanentes",
        publisher=AR,
        url=_ar_page("DADelegacoesPermanentes"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Delegações da Assembleia a assembleias parlamentares internacionais "
            "(APCE, AP OSCE, AP NATO, UIP, entre outras). Liga deputados às delegações de que são membros."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="ar_grupos_amizade",
        title="Assembleia da República — Grupos Parlamentares de Amizade",
        publisher=AR,
        url=_ar_page("DAGPA"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Grupos parlamentares de amizade e respetiva data de criação. "
            "Liga deputados aos grupos de amizade que integram ou presidem."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="ar_registo_interesses",
        title="Assembleia da República — Registo de Interesses (arquivo, legislaturas anteriores)",
        publisher=AR,
        url=_ar_page("DARegistoBiografico"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Registos de interesses dos deputados anteriores à Entidade para a Transparência. "
            "Publica ligações declaradas pelos deputados, sem verificação independente; dados do cônjuge e dados privados são excluídos."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="ept_titulares",
        title="Entidade para a Transparência — Titulares por entidade e cargo",
        publisher=EPT,
        url=EPT_URL,
        licence=EPT_LICENCE,
        licence_url="",
        description=(
            "Lista pública de titulares de cargos por entidade, órgão e cargo, com datas. "
            "Liga titulares às entidades públicas onde exercem funções."
        ),
        identifiers=("ept",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="gov_nomeacoes",
        title="Governo — Nomeações dos gabinetes",
        publisher=GOV,
        url="https://portugal.gov.pt/gc25/governo/nomeacoes",
        licence=GOV_LICENCE,
        licence_url="",
        description=(
            "Pessoal dos gabinetes dos membros do Governo, com função, data de nomeação e despacho. "
            "Liga chefes de gabinete, adjuntos e técnicos especialistas ao gabinete; remunerações não são importadas."
        ),
        identifiers=("government",),
        status="imported",
        phase="A",
    ),
    Dataset(
        key="dr_diario_republica",
        title="Diário da República",
        publisher="Imprensa Nacional-Casa da Moeda",
        url="https://diariodarepublica.pt/",
        licence=(
            "Textos oficiais não protegidos por direito de autor (CDADC, art. 8.º); "
            "acesso universal e gratuito (Decreto-Lei n.º 83/2016)."
        ),
        licence_url="",
        description=(
            "Atos de nomeação e exoneração publicados em Diário da República. "
            "Serve de prova documental das datas de início e fim de funções."
        ),
        identifiers=(),
        status="imported",
        phase="A",
    ),
    # Phase B.
    Dataset(
        key="sioe",
        title="SIOE+ — Sistema de Informação da Organização do Estado",
        publisher="Direção-Geral da Administração e do Emprego Público (DGAEP)",
        url="https://www.sioe.dgaep.gov.pt/",
        licence="Sem licença publicada; acesso livre e gratuito por lei (Lei n.º 104/2019, art. 11.º).",
        licence_url="",
        description=(
            "Entidades do setor público com código SIOE, NIPC, tutela e órgãos de direção datados. "
            "Liga membros de órgãos de direção às entidades e estas à respetiva tutela."
        ),
        identifiers=("sioe", "nipc"),
        status="imported",
        phase="B",
    ),
    Dataset(
        key="gleif_lei",
        title="GLEIF — Identificadores de Entidades Jurídicas (LEI)",
        publisher="Global Legal Entity Identifier Foundation (GLEIF)",
        url="https://search.gleif.org/",
        licence="CC0 (domínio público).",
        licence_url="https://www.gleif.org/en/meta/lei-data-terms-of-use",
        description=(
            "Códigos LEI de entidades com sede em Portugal e o respetivo NIPC no Registo Comercial. "
            "Liga sociedades às suas empresas-mãe diretas e finais."
        ),
        identifiers=("lei", "nipc"),
        status="imported",
        phase="B",
    ),
    Dataset(
        key="wikidata",
        title="Wikidata",
        publisher="Comunidade Wikimedia",
        url="https://www.wikidata.org/",
        licence="CC0 (domínio público).",
        licence_url="https://www.wikidata.org/wiki/Wikidata:Licensing",
        description=(
            "Fonte secundária, não oficial: usada apenas como pista de identificadores "
            "(QID) confirmada por identificadores oficiais. Nenhuma ligação é publicada só com base nela."
        ),
        identifiers=("wikidata", "parliament", "ep", "lei", "eu_tr"),
        status="secondary",
        phase="B",
    ),
    # Phase C.
    Dataset(
        key="base_contratos",
        title="Portal BASE — Contratos públicos",
        publisher="IMPIC — Instituto dos Mercados Públicos, do Imobiliário e da Construção",
        url="https://dados.gov.pt/pt/datasets/contratos-publicos-portal-base-impic-contratos-de-2012-a-2026/",
        licence=DADOS_PD,
        licence_url="",
        description=(
            "Contratos públicos celebrados desde 2012, com entidades adjudicantes e adjudicatários. "
            "Liga entidades públicas a empresas pelo NIPC; pessoas singulares não são importadas."
        ),
        identifiers=("nipc",),
        status="imported",
        phase="C",
    ),
    Dataset(
        key="base_entidades",
        title="Portal BASE — Entidades",
        publisher="IMPIC — Instituto dos Mercados Públicos, do Imobiliário e da Construção",
        url="https://dados.gov.pt/pt/organizations/impic-i-p-instituto-dos-mercados-publicos-do/",
        licence=DADOS_PD,
        licence_url="",
        description=(
            "Entidades que participam em contratos públicos, com NIPC e designação. "
            "Serve para identificar organizações; pessoas singulares não são importadas."
        ),
        identifiers=("nipc",),
        status="imported",
        phase="C",
    ),
    Dataset(
        key="igf_subvencoes",
        title="IGF — Listas de subvenções e benefícios públicos e de doações",
        publisher="Inspeção-Geral de Finanças (IGF)",
        url="https://igf.gov.pt/subvencoes-publicas",
        licence=DADOS_PD,
        licence_url="",
        description=(
            "Subvenções e benefícios públicos concedidos (Lei n.º 64/2013), com montante e data de decisão. "
            "Liga entidades concedentes a beneficiários coletivos pelo NIPC; pessoas singulares são descartadas."
        ),
        identifiers=("nipc",),
        status="imported",
        phase="C",
    ),
    Dataset(
        key="pt2020_beneficiarios",  # gitleaks:allow - dataset key, not a secret.
        title="Portugal 2020 — Beneficiários",
        publisher="Agência para o Desenvolvimento e Coesão (AD&C)",
        url="https://dados.gov.pt/pt/datasets/datasets-do-portugal-2020/",
        licence="CC BY 4.0, com citação da fonte.",
        licence_url="https://creativecommons.org/licenses/by/4.0/",
        description=(
            "Operações cofinanciadas pelo Portugal 2020 e respetivas entidades beneficiárias. "
            "Liga beneficiários coletivos aos fundos europeus pelo NIPC; pessoas singulares são descartadas."
        ),
        identifiers=("nipc",),
        status="imported",
        phase="C",
    ),
    Dataset(
        key="pt2030_operacoes",
        title="Portugal 2030 — Lista de operações",
        publisher="Agência para o Desenvolvimento e Coesão (AD&C)",
        url="https://dados.gov.pt/pt/datasets/datasets-pt2030-03-lista-de-operacoes-pt2030/",
        licence=NOT_SPECIFIED,
        licence_url="",
        description=(
            "Operações aprovadas no Portugal 2030, com montantes, fundo e datas. "
            "Liga beneficiários coletivos aos fundos europeus pelo NIPC; pessoas singulares são descartadas."
        ),
        identifiers=("nipc",),
        status="imported",
        phase="C",
    ),
    Dataset(
        key="prr_entidades",
        title="PRR — Entidades",
        publisher="Estrutura de Missão Recuperar Portugal",
        url="https://dados.gov.pt/pt/datasets/dataset-estrutura-de-missao-prr-entidades-1/",
        licence=NOT_SPECIFIED,
        licence_url="",
        description=(
            "Entidades envolvidas em projetos do Plano de Recuperação e Resiliência e respetivo papel. "
            "Liga beneficiários, intermediários e fornecedores coletivos pelo NIPC; entradas pseudonimizadas são descartadas."
        ),
        identifiers=("nipc",),
        status="imported",
        phase="C",
    ),
    Dataset(
        key="etf_orgaos_sociais",
        title="Entidade do Tesouro e Finanças — Órgãos sociais das empresas públicas",
        publisher="Entidade do Tesouro e Finanças (ETF)",
        url="https://www.etf.gov.pt/informacao-sobre-as-empresas",
        licence=(
            "Utilização gratuita para uso pessoal ou público sem fins lucrativos nem ofensivos, "
            "com referência à fonte."
        ),
        licence_url="https://www.etf.gov.pt/avisos-legais",
        description=(
            "Membros dos órgãos sociais das empresas do setor empresarial do Estado, por mandato. "
            "Liga administradores e membros do conselho fiscal às empresas públicas; remunerações não são importadas."
        ),
        identifiers=("nipc",),
        status="imported",
        phase="C",
    ),
    # Phase D.
    Dataset(
        key="ep_deputados",
        title="Parlamento Europeu — Deputados (dados abertos)",
        publisher=EP,
        url="https://data.europarl.europa.eu/api/v2/meps?country-of-representation=PT",
        licence=EP_LICENCE,
        licence_url=EP_LICENCE_URL,
        description=(
            "Deputados portugueses ao Parlamento Europeu e pertenças datadas a grupos políticos, "
            "comissões e delegações. Filiação partidária nacional não é importada."
        ),
        identifiers=("ep",),
        status="imported",
        phase="D",
    ),
    # Phase E.
    Dataset(
        key="eu_registo_transparencia",
        title="Registo de Transparência da UE",
        publisher="Comissão Europeia e Parlamento Europeu",
        url="https://data.europa.eu/data/datasets/transparency-register",
        licence=EC_REUSE,
        licence_url="https://eur-lex.europa.eu/eli/dec/2011/833/oj",
        description=(
            "Organizações registadas como representantes de interesses junto das instituições europeias. "
            "Identifica as organizações presentes nas reuniões do Parlamento e da Comissão."
        ),
        identifiers=("eu_tr",),
        status="imported",
        phase="E",
    ),
    Dataset(
        key="ep_reunioes",
        title="Parlamento Europeu — Reuniões dos deputados com representantes de interesses",
        publisher=EP,
        url="https://www.europarl.europa.eu/meps/en/search-meetings",
        licence=EP_LICENCE,
        licence_url=EP_LICENCE_URL,
        description=(
            "Reuniões publicadas pelos deputados ao Parlamento Europeu (Código de Conduta, art. 7.º). "
            "Liga deputados portugueses às organizações com que se reuniram."
        ),
        identifiers=("ep", "eu_tr"),
        status="imported",
        phase="E",
    ),
    Dataset(
        key="ce_reunioes",
        title="Comissão Europeia — Reuniões com representantes de interesses",
        publisher="Comissão Europeia",
        url="https://data.europa.eu/data/datasets/european-commission-meetings-with-interest-representatives",
        licence="CC BY 4.0, com citação da fonte.",
        licence_url="https://creativecommons.org/licenses/by/4.0/",
        description=(
            "Reuniões de comissários e respetivos gabinetes com representantes de interesses. "
            "Liga comissários portugueses às organizações com que se reuniram."
        ),
        identifiers=("ec", "eu_tr"),
        status="imported",
        phase="E",
    ),
    Dataset(
        key="ar_atividades",
        title="Assembleia da República — Atividades",
        publisher=AR,
        url=_ar_page("DAatividades"),
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Audições e audiências das comissões parlamentares e eleições pela Assembleia para órgãos externos. "
            "Liga comissões às entidades ouvidas e pessoas eleitas aos órgãos externos."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="E",
    ),
    Dataset(
        key="ar_ofertas_hospitalidades",
        title="Assembleia da República — Registo de ofertas, deslocações e hospitalidades",
        publisher=AR,
        url="https://www.parlamento.pt/RegistoDeslocacoesHospitalidades/Paginas/ROfertasDHList.aspx",
        licence=AR_LICENCE,
        licence_url=AR_LICENCE_URL,
        description=(
            "Ofertas, deslocações e hospitalidades registadas pelos deputados (Lei n.º 52/2019). "
            "Liga deputados às entidades ofertantes."
        ),
        identifiers=("parliament",),
        status="imported",
        phase="E",
    ),
    # Upcoming, not imported.
    Dataset(
        key="rtri",
        title="Registo de Transparência da Representação de Interesses (RTRI)",
        publisher=AR,
        url=(
            "https://www.parlamento.pt/Paginas/2026/agosto/"
            "Informacao-sobre-inicio-funcionamento-do-Registo-de-Transparencia-Representacao-Interesses-RTRI.aspx"
        ),
        licence="Dados abertos por lei (Lei n.º 5-A/2026, art. 4.º, n.º 4); termos de reutilização por publicar.",
        licence_url="",
        description=(
            "Registo público de representantes de interesses, em funcionamento pleno a partir de 1 de janeiro de 2027. "
            "Ligará representantes aos clientes e às reuniões com entidades públicas."
        ),
        identifiers=(),
        status="upcoming",
        phase="E",
    ),
    Dataset(
        key="gov_audiencias",
        title="Governo — Audiências concedidas pelos membros do Governo",
        publisher=GOV,
        url="https://files.diariodarepublica.pt/1s/2026/08/15600/0004200044.pdf",
        licence="Publicação trimestral obrigatória (RCM n.º 167/2026); termos de reutilização por publicar.",
        licence_url="",
        description=(
            "Listas trimestrais de audiências de ministros e gabinetes a representantes de interesses. "
            "Ligará membros do Governo às entidades recebidas."
        ),
        identifiers=("government",),
        status="upcoming",
        phase="E",
    ),
)

DATASETS: dict[str, Dataset] = {dataset.key: dataset for dataset in _DATASETS}

# Parliament ids: only plain numeric DepCadId values are deputies with a public
# biography page; organ ids (`orgao:`, `gp:`, `gpa:`, `delegacao:`,
# `institution:`) have no stable public page and are never linked.
IDENTIFIER_SCHEMES: dict[str, SchemeInfo] = {
    "parliament": SchemeInfo(
        "Assembleia da República",
        "https://www.parlamento.pt/DeputadoGP/Paginas/Biografia.aspx?BID={id}",
        True,
    ),
    "government": SchemeInfo("Governo", None, False),
    "ept": SchemeInfo("Entidade para a Transparência", None, False),
    "ep": SchemeInfo("Parlamento Europeu", "https://www.europarl.europa.eu/meps/pt/{id}", True),
    "nipc": SchemeInfo("NIPC", None, True),
    "sioe": SchemeInfo("SIOE+", None, True),
    "lei": SchemeInfo("LEI (GLEIF)", "https://search.gleif.org/#/record/{id}", True),
    "eu_tr": SchemeInfo(
        "Registo de Transparência da UE",
        "https://transparency-register.europa.eu/searchregister-or-update/organisation-detail_en?id={id}",
        True,
    ),
    "ec": SchemeInfo("Comissão Europeia", None, False),
    "wikidata": SchemeInfo("Wikidata", "https://www.wikidata.org/wiki/{id}", True),
}

_URL_VALUE = {
    "parliament": re.compile(r"[0-9]+"),
    "ep": re.compile(r"[0-9]+"),
    "lei": re.compile(r"[A-Z0-9]{20}"),
    "eu_tr": re.compile(r"[0-9]{6,15}-[0-9]{2}"),
    "wikidata": re.compile(r"Q[1-9][0-9]*"),
}


def identifier_url(scheme: str, value: str) -> str | None:
    """Public record URL for an identifier, or None when it has none or is not public."""
    info = IDENTIFIER_SCHEMES.get(scheme)
    if info is None or not info.public or info.url_template is None:
        return None
    pattern = _URL_VALUE.get(scheme)
    if pattern is None or pattern.fullmatch(value) is None:
        return None
    return info.url_template.format(id=value)
