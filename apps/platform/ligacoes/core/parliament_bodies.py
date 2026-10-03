"""AR organs, parliamentary groups, delegations and friendship groups for one legislature.

Organ, group and delegation ids are per legislature, so every organisation key includes the
legislature; persons are keyed by DepCadId only. No party data (``ParSigla``/``ParDes``,
lists or coalitions) is ever read: parliamentary groups are the only political grouping.
"""

import hashlib
import json
import re
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from itertools import batched
from typing import cast

from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_observations
from .identity import ar_institution, ar_person, normalise_name, official_entities_bulk
from .models import (
    EnrichmentSource,
    Entity,
    IdentityScheme,
    Relationship,
    SourceIdentity,
    SourceObservation,
    TemporalStatus,
    Term,
    editorial_transaction,
    import_transaction,
)
from .parliament_fetch import (
    FOLDER_LABELS,
    Download,
    ParliamentImportError,
    discover_download,
    download_limits,
    validate_file_legislature,
    validate_url,
)
from .parliament_parse import (
    SERVING,
    JSONObject,
    JSONValue,
    _date,
    _identifier,
    _object,
    _pairs,
    _rows,
    _text,
    canonical_json,
)

ROSTER = "ar_informacao_base"
BODIES = "ar_composicao_orgaos"
ACTIVITY = "ar_atividade_deputados"
DELEGATIONS = "ar_delegacoes_permanentes"
# Catalogue dataset key → parliament_fetch dataset name (for URL revalidation).
FETCH_DATASETS = {
    ROSTER: "roster",
    BODIES: "bodies",
    ACTIVITY: "activity",
    DELEGATIONS: "delegations",
}
MEMBERSHIP = Relationship.Kind.MEMBERSHIP
PUBLIC_OFFICE = Relationship.Kind.PUBLIC_OFFICE
RoleClass = Relationship.RoleClass
LEGISLATURE_LABELS = {
    "Cons": "Assembleia Constituinte",
    "IA": "I Legislatura (IA)",
    "IB": "I Legislatura (IB)",
}
# Permanent delegations are published from the IX Legislatura onwards.
FIRST_DELEGATIONS = 9
# gpId 0 and these acronyms mark independent or non-attached deputies, not a group.
NOT_A_GROUP = frozenset({"Indep", "Ninsc", "DESC"})
ACTIVE_MEMBERSHIP = frozenset({"Ativo", "Activo", "Efetivo"})
COMMITTEE_SECTIONS = (
    ("Comissoes", "CO"),
    ("SubComissoes", "SC"),
    ("GruposTrabalho", "GT"),
)
BODY_SECTIONS = {
    "ComissaoPermanente": "Comissão Permanente",
    "ConferenciaLideres": "Conferência de Líderes",
    "ConselhoAdministracao": "Conselho de Administração",
    "ConferenciaPresidentesComissoes": "Conferência dos Presidentes das Comissões Parlamentares",
}
# Source role wording → role class; first match wins on the normalised role.
ROLE_CLASSES: tuple[tuple[str, str], ...] = (
    (r"\bsuplente\b", RoleClass.SUBSTITUTE),
    (r"^vice (?:presidente|coordenador|coordenadora)\b", RoleClass.DEPUTY_LEADERSHIP),
    # A GP coordinator inside a committee neither leads nor deputises for the committee.
    (r"^(?:vice )?cgp\b", RoleClass.OTHER),
    (r"^vice secretari[oa]\b", RoleClass.MEMBER),
    (r"^presidente\b", RoleClass.LEADERSHIP),
    (r"^coordenadora?\b", RoleClass.DEPUTY_LEADERSHIP),
    (r"^secretari[oa] geral\b", RoleClass.STAFF),
    (
        r"^(?:secretari[oa]|membro|vogal|efetivo|deputad[oa]|representante|lider)\b",
        RoleClass.MEMBER,
    ),
)
_ROMAN = {"M": 1000, "D": 500, "C": 100, "L": 50, "X": 10, "V": 5, "I": 1}
_ROMAN_OUT = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


@dataclass(frozen=True)
class Legislature:
    code: str
    start: date
    end: date | None

    @property
    def label(self) -> str:
        return legislature_label(self.code)


@dataclass(frozen=True)
class Organ:
    """One legislature's instance of an AR body, keyed under the ``parliament`` scheme."""

    key: str
    name: str
    classification: str
    role: str
    # Key of the parent organ; empty for the Assembleia da República itself.
    parent: str
    dataset: str
    source_url: str
    reference: str
    passage: str


@dataclass(frozen=True)
class Claim:
    external_id: str
    # Legislature-scoped parliamentary body key.
    organ: str
    # DepCadId; empty for name-only subjects resolved as source-scoped people.
    cadastro_id: str
    subject_name: str
    subject_reference: str
    kind: str
    role: str
    role_class: str
    start: date | None
    end: date | None
    temporal_status: str
    dataset: str
    source_url: str
    reference: str
    passage: str


@dataclass(frozen=True)
class BodiesSnapshot:
    legislature: Legislature
    as_of: date
    organs: tuple[Organ, ...]
    claims: tuple[Claim, ...]
    names: dict[str, str]
    skipped_intervals: int


def legislature_label(code: str) -> str:
    return LEGISLATURE_LABELS.get(code, f"{code} Legislatura")


def _rank(code: str) -> float:
    if code == "Cons":
        return 0
    if code in FOLDER_LABELS:
        return 1 if code == "IA" else 1.5
    values = [_ROMAN[char] for char in code]
    return sum(
        -value if index + 1 < len(values) and value < values[index + 1] else value
        for index, value in enumerate(values)
    )


def _roman(number: int) -> str:
    result = ""
    for value, symbol in _ROMAN_OUT:
        while number >= value:
            result += symbol
            number -= value
    return result


def earlier_legislatures(code: str) -> tuple[str, ...]:
    """Legislature file codes before ``code``, newest first."""
    rank = _rank(code)
    codes = [_roman(number) for number in range(int(rank) - (rank.is_integer()), 1, -1)]
    codes += [older for older in ("IB", "IA", "Cons") if _rank(older) < rank]
    return tuple(codes)


def later_legislatures(code: str, latest: str) -> tuple[str, ...]:
    """Legislature file codes after ``code`` up to ``latest``, oldest first."""
    later = [newer for newer in ("IA", "IB") if _rank(code) < _rank(newer)]
    first = max(2, int(_rank(code)) + 1)
    return tuple(later + [_roman(number) for number in range(first, int(_rank(latest)) + 1)])


def role_class(role: str) -> str:
    """Shared role classes from the source's wording; unknown wording is ``other``."""
    text = normalise_name(role)
    if not text:
        return ""
    for pattern, value in ROLE_CLASSES:
        if re.search(pattern, text):
            return value
    return RoleClass.OTHER


def temporal_status(
    start: date | None, end: date | None, legislature_end: date | None, as_of: date
) -> str:
    """Ended once the source's end or the legislature has passed; open claims are current."""
    if (end is not None and end < as_of) or (
        legislature_end is not None and legislature_end < as_of
    ):
        return TemporalStatus.ENDED
    if start is not None and start > as_of:
        return TemporalStatus.UNKNOWN
    return TemporalStatus.CURRENT


@editorial_transaction()
def legislature_term(code: str, start: date, end: date | None) -> Term:
    """The single Term per legislature; the source's dates win (an open end gets filled)."""
    term = Term.objects.select_for_update().filter(kind=Term.Kind.LEGISLATURE, code=code).first()
    if term is None:
        term = Term(
            kind=Term.Kind.LEGISLATURE,
            code=code,
            label=legislature_label(code),
            institution=ar_institution(),
            start_date=start,
            end_date=end,
        )
    elif (term.start_date, term.end_date) == (start, end):
        return term
    else:
        term.start_date, term.end_date = start, end
    term.full_clean()
    term.save()
    return term


def _load(download: Download, dataset: str) -> JSONValue:
    if len(download.content) > download_limits(dataset)[0]:
        raise ParliamentImportError("JSON payload exceeds the size limit.")
    try:
        return cast(
            JSONValue, json.loads(download.content.decode("utf-8-sig"), object_pairs_hook=_pairs)
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ParliamentImportError("Invalid official JSON payload.") from exc


def _clean(value: JSONValue, field: str, *, optional: bool = False, limit: int = 240) -> str:
    return " ".join(_text(value, field, optional=optional, limit=limit).split())


def _stamp(value: JSONValue, field: str) -> date | None:
    """AtividadeDeputado timestamps: ``YYYY-MM-DD HH:MM:SS.0``."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)?", value
    ):
        raise ParliamentImportError(f"Invalid date in {field}.")
    return _date(value[:10], field)


def _dmy(value: JSONValue, field: str) -> date | None:
    """Delegation dates: ``DD/MM/YYYY HH:MM:SS``, empty when unknown."""
    if value is None or value == "":
        return None
    match = (
        re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})(?: \d{2}:\d{2}:\d{2})?", value)
        if isinstance(value, str)
        else None
    )
    if match is None:
        raise ParliamentImportError(f"Invalid date in {field}.")
    day, month, year = match.groups()
    return _date(f"{year}-{month}-{day}", field)


def _pt(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _period(start: date | None, end: date | None) -> str:
    if start and end:
        return f"de {_pt(start)} a {_pt(end)}"
    if start:
        return f"desde {_pt(start)}"
    if end:
        return f"até {_pt(end)}"
    return "sem datas indicadas na fonte"


def _number(value: JSONValue, field: str) -> int | None:
    if value is None or value in (0, 0.0, "0", ""):
        return None
    return int(_identifier(value, field))


def _optional_id(value: JSONValue, field: str) -> str:
    """Official ids where 0/null are sentinels (no organ, staff member, non-attached)."""
    if value is None or value in (0, 0.0, "0", ""):
        return ""
    return _identifier(value, field)


def _legislature(root: JSONObject, code: str) -> Legislature:
    detail = _object(root.get("DetalheLegislatura"), "DetalheLegislatura")
    if detail.get("siglaAntiga", detail.get("sigla")) != code:
        raise ParliamentImportError("Roster legislature does not match the requested legislature.")
    start, end = _date(detail.get("dtini"), "dtini"), _date(detail.get("dtfim"), "dtfim")
    if start is None or (end is not None and end < start):
        raise ParliamentImportError("Invalid legislature dates.")
    return Legislature(code, start, end)


def dep_map(download: Download, code: str) -> dict[str, tuple[str, str]]:
    """DepId → (DepCadId, full name) for one InformacaoBase file."""
    root = _object(_load(download, "roster"), "roster")
    _legislature(root, code)
    result: dict[str, tuple[str, str]] = {}
    for row in _rows(root.get("Deputados"), "Deputados"):
        dep = _identifier(row.get("DepId"), "DepId")
        entry = (
            _identifier(row.get("DepCadId"), "DepCadId"),
            _clean(row.get("DepNomeCompleto"), "DepNomeCompleto"),
        )
        if result.setdefault(dep, entry)[0] != entry[0]:
            raise ParliamentImportError("A DepId maps to two cadastro identifiers.")
    return result


def delegation_rows(download: Download, code: str) -> list[JSONObject]:
    """Unique delegations; the official file repeats every object, so deduplicate by Id."""
    unique: dict[str, JSONObject] = {}
    for row in _rows(_load(download, "delegations"), "delegations"):
        key = _identifier(row.get("Id"), "Id")
        if row.get("Legislatura") != code:
            raise ParliamentImportError("Delegation legislature does not match the file.")
        if key in unique and canonical_json(unique[key]) != canonical_json(row):
            raise ParliamentImportError("Repeated delegation id with different content.")
        unique[key] = row
    return [unique[key] for key in sorted(unique, key=int)]


def delegation_dep_ids(download: Download, code: str) -> set[str]:
    return {
        _identifier(member.get("Id"), "Composicao.Id")
        for row in delegation_rows(download, code)
        for member in _rows(row.get("Composicao"), "Composicao", optional=True)
        if member.get("Id") not in (None, "")
    }


def _serving(row: JSONObject, key: str = "DepSituacao") -> list[tuple[str, date, date | None]]:
    """Effective plenary periods: Efetivo* rows with a start and a non-reversed end.

    Overlapping or contiguous rows (e.g. Efetivo Temporário then Efetivo Definitivo on the
    same day) form one continuous period; the statuses are listed in order."""
    periods: list[tuple[str, date, date | None]] = []
    for status in _rows(row.get(key), key, optional=True):
        des = _clean(status.get("sioDes"), "sioDes")
        if des not in SERVING:
            continue
        start = _date(status.get("sioDtInicio"), "sioDtInicio")
        end = _date(status.get("sioDtFim"), "sioDtFim")
        # Missing-start or reversed intervals are neither repaired nor imported.
        if start is not None and (end is None or end >= start):
            periods.append((des, start, end))
    merged: list[tuple[str, date, date | None]] = []
    for des, start, end in sorted(periods, key=lambda period: (period[1], period[2] or date.max)):
        if merged:
            names, first, last = merged[-1]
            if last is None or start <= last + timedelta(days=1):
                if des not in names.split(", "):
                    names = f"{names}, {des}"
                merged[-1] = (names, first, None if last is None or end is None else max(last, end))
                continue
        merged.append((des, start, end))
    return merged


def _normal_title(value: str) -> str:
    return normalise_name(value.replace("\u2013", "-"))


class _Builder:
    def __init__(self, legislature: Legislature, as_of: date, urls: dict[str, str]) -> None:
        self.legislature = legislature
        self.as_of = as_of
        self.urls = urls
        self.organs: dict[str, Organ] = {}
        self.claims: dict[str, Claim] = {}
        self.names: dict[str, str] = {}
        self.fallback_names: dict[str, str] = {}
        self.skipped = 0

    @property
    def code(self) -> str:
        return self.legislature.code

    @property
    def suffix(self) -> str:
        return f"({self.legislature.label})"

    def organ(
        self,
        key: str,
        name: str,
        classification: str,
        role: str,
        *,
        dataset: str,
        reference: str,
    ) -> str:
        full_name = f"{name} {self.suffix}"[:240]
        organ = Organ(
            key=key,
            name=full_name,
            classification=classification,
            role=role,
            parent="",
            dataset=dataset,
            source_url=self.urls[dataset],
            reference=reference,
            passage=f"{full_name}. Tipo: {role}. Integra: Assembleia da República.",
        )
        existing = self.organs.get(key)
        if existing is not None and existing != organ:
            raise ParliamentImportError("Ambiguous official organ identifier.")
        self.organs[key] = organ
        return full_name

    def claim(self, claim: Claim) -> None:
        existing = self.claims.get(claim.external_id)
        if existing is not None and existing != claim:
            raise ParliamentImportError("Ambiguous composition record.")
        self.claims[claim.external_id] = claim

    def valid(self, start: date | None, end: date | None) -> bool:
        if start is not None and end is not None and end < start:
            # Reversed upstream intervals are neither repaired nor imported.
            self.skipped += 1
            return False
        return True

    def clip(self, start: date, end: date | None) -> tuple[date, date | None] | None:
        """Within this legislature: the IA and IB files both span the whole I Legislatura.

        An open end stays open (never filled from the legislature's end)."""
        start = max(start, self.legislature.start)
        if end is not None and self.legislature.end is not None:
            end = min(end, self.legislature.end)
        if end is not None and end < start:
            return None
        if self.legislature.end is not None and start > self.legislature.end:
            return None
        return start, end

    def status(self, start: date | None, end: date | None) -> str:
        return temporal_status(start, end, self.legislature.end, self.as_of)

    def person_claim(
        self,
        *,
        organ: str,
        cadastro_id: str,
        subject_name: str,
        subject_reference: str,
        facet: str,
        kind: str,
        role: str,
        role_class: str,
        start: date | None,
        end: date | None,
        dataset: str,
        reference: str,
        passage: list[str],
    ) -> None:
        person = cadastro_id or subject_reference
        self.claim(
            Claim(
                # The source has no row ids; person, role and period identify a claim.
                external_id=f"{organ}|{person}|{facet}|{role}|{start or ''}|{end or ''}"[:240],
                organ=organ,
                cadastro_id=cadastro_id,
                subject_name="" if cadastro_id else subject_name,
                subject_reference="" if cadastro_id else subject_reference,
                kind=kind,
                role=role,
                role_class=role_class,
                start=start,
                end=end,
                temporal_status=self.status(start, end),
                dataset=dataset,
                source_url=self.urls[dataset],
                reference=reference[:160],
                passage="\n".join(passage),
            ),
        )

    # InformacaoBase: legislature, names and GP memberships while sitting.
    def roster(self, root: JSONObject) -> None:
        deputies = _rows(root.get("Deputados"), "Deputados")
        ends = [
            group.get("gpDtFim")
            for row in deputies
            for group in _rows(row.get("DepGP"), "DepGP", optional=True)
        ]
        placeholder = None
        if self.legislature.end is None and ends:
            # In the open legislature the exporter writes its extraction day as every
            # gpDtFim; a date shared by most rows is that placeholder, not a real end.
            latest = max((end for end in ends if isinstance(end, str)), default=None)
            if latest is not None and ends.count(latest) * 2 > len(ends):
                placeholder = _date(latest, "gpDtFim")
        for row in deputies:
            cadastro = _identifier(row.get("DepCadId"), "DepCadId")
            self.names.setdefault(cadastro, _clean(row.get("DepNomeCompleto"), "DepNomeCompleto"))
            parliamentary = _clean(row.get("DepNomeParlamentar"), "DepNomeParlamentar")
            periods = _serving(row)
            if not periods:
                continue
            for group in _rows(row.get("DepGP"), "DepGP", optional=True):
                gp_id = _optional_id(group.get("gpId"), "gpId")
                sigla = _clean(group.get("gpSigla"), "gpSigla", optional=True)
                if not gp_id or not sigla or sigla in NOT_A_GROUP:
                    continue
                gp_start = _date(group.get("gpDtInicio"), "gpDtInicio")
                gp_end = _date(group.get("gpDtFim"), "gpDtFim")
                if placeholder is not None and gp_end == placeholder:
                    gp_end = None
                if gp_start is None or not self.valid(gp_start, gp_end):
                    continue
                key = f"gp:{self.code}:{gp_id}"
                for status, sit_start, sit_end in periods:
                    # Membership while sitting: the GP interval within each plenary period.
                    bounds = [bound for bound in (gp_end, sit_end) if bound is not None]
                    overlap = self.clip(max(gp_start, sit_start), min(bounds) if bounds else None)
                    if overlap is None:
                        continue
                    start, end = overlap
                    name = self.organ(
                        key,
                        f"Grupo Parlamentar do {sigla}",
                        Entity.Classification.PARLIAMENTARY_GROUP,
                        "Grupo parlamentar",
                        dataset=ROSTER,
                        reference=f"InformacaoBase{self.code} / gpId={gp_id}",
                    )
                    self.person_claim(
                        organ=key,
                        cadastro_id=cadastro,
                        subject_name=parliamentary,
                        subject_reference="",
                        facet="gp",
                        kind=MEMBERSHIP,
                        role="Deputado/a",
                        role_class=RoleClass.MEMBER,
                        start=start,
                        end=end,
                        dataset=ROSTER,
                        reference=f"InformacaoBase{self.code} / DepCadId={cadastro} / gpId={gp_id}",
                        passage=[
                            f"Grupo parlamentar: {name}.",
                            f"Deputado/a: {parliamentary}.",
                            f"Grupo parlamentar na fonte: {_period(gp_start, gp_end)}.",
                            f"Situação do mandato: {status}, {_period(sit_start, sit_end)}.",
                            f"Identificador AR (DepCadId): {cadastro}.",
                        ],
                    )

    # AtividadeDeputado: friendship-group roles and subcommittee/working-group parents.
    def activity(self, root: JSONValue) -> list[tuple[int, str, int]]:
        links: list[tuple[int, str, int]] = []
        for entry in _rows(root, "AtividadeDeputado"):
            deputy = _object(entry.get("Deputado"), "Deputado")
            cadastro = _identifier(deputy.get("DepCadId"), "DepCadId")
            self.fallback_names.setdefault(
                cadastro, _clean(deputy.get("DepNomeCompleto"), "DepNomeCompleto")
            )
            parliamentary = _clean(deputy.get("DepNomeParlamentar"), "DepNomeParlamentar")
            for activity in _rows(entry.get("AtividadeDeputadoList"), "AtividadeDeputadoList"):
                for group in _rows(activity.get("Gpa"), "Gpa", optional=True):
                    if group.get("GplSelLg") != self.code:
                        continue
                    self.friendship(cadastro, parliamentary, group)
                for unit in _rows(activity.get("Scgt"), "Scgt", optional=True):
                    if unit.get("ScmComLg") != self.code:
                        continue
                    child = _number(unit.get("ScmCd"), "ScmCd")
                    parent = _number(unit.get("ScmComCd"), "ScmComCd")
                    if child is not None and parent is not None:
                        links.append(
                            (child, _normal_title(_clean(unit.get("CcmDscom"), "CcmDscom")), parent)
                        )
        return links

    def friendship(self, cadastro: str, parliamentary: str, group: JSONObject) -> None:
        group_id = _identifier(group.get("GplId"), "GplId")
        key = f"gpa:{self.code}:{group_id}"
        name = self.organ(
            key,
            f"Grupo Parlamentar de Amizade {_clean(group.get('GplNo'), 'GplNo')}",
            Entity.Classification.FRIENDSHIP_GROUP,
            "Grupo parlamentar de amizade",
            dataset=ACTIVITY,
            reference=f"AtividadeDeputado{self.code} / Gpa GplId={group_id}",
        )
        role = _clean(group.get("CgaCrg"), "CgaCrg", optional=True)
        start = _stamp(group.get("CgaDtini"), "CgaDtini")
        end = _stamp(group.get("CgaDtfim"), "CgaDtfim")
        if not self.valid(start, end):
            return
        self.person_claim(
            organ=key,
            cadastro_id=cadastro,
            subject_name=parliamentary,
            subject_reference="",
            facet="gpa",
            kind=MEMBERSHIP,
            role=role,
            role_class=role_class(role) or RoleClass.MEMBER,
            start=start,
            end=end,
            dataset=ACTIVITY,
            reference=f"AtividadeDeputado{self.code} / Gpa GplId={group_id} / DepCadId={cadastro}",
            passage=[
                f"Grupo parlamentar de amizade: {name}.",
                f"Deputado/a: {parliamentary}.",
                f"Cargo: {role or 'não indicado'}, {_period(start, end)}.",
                f"Identificador AR (DepCadId): {cadastro}.",
                "Cobertura: o ficheiro Atividade dos Deputados só inclui deputados da "
                "composição mais recente da Assembleia; a ausência de outros deputados "
                "nesta fonte não significa que não tenham integrado o grupo.",
            ],
        )

    # OrgaoComposicao: committees, subcommittees, working groups and governing bodies.
    def bodies(self, root: JSONObject, links: list[tuple[int, str, int]]) -> None:
        committees: dict[int, list[tuple[str, str]]] = {}
        units: list[tuple[str, int | None, str, str]] = []
        for section, expected in COMMITTEE_SECTIONS:
            for organ in _rows(root.get(section), section, optional=True):
                detail = self.detail(organ, expected)
                if detail is None:
                    continue
                organ_id, number, title = detail
                if not title:
                    raise ParliamentImportError("Committee without an official name.")
                if expected == "CO":
                    if re.search(r"inqu[ée]rito", title, re.IGNORECASE):
                        role = "Comissão parlamentar de inquérito"
                    elif re.search(r"eventual", title, re.IGNORECASE):
                        role = "Comissão eventual"
                    else:
                        role = "Comissão permanente"
                else:
                    role = "Subcomissão" if expected == "SC" else "Grupo de trabalho"
                key = f"orgao:{self.code}:{organ_id}"
                name = self.organ(
                    key,
                    title,
                    Entity.Classification.PARLIAMENTARY_COMMITTEE,
                    role,
                    dataset=BODIES,
                    reference=f"OrgaoComposicao{self.code} / {section} idOrgao={organ_id}",
                )
                if number is not None:
                    if expected == "CO":
                        committees.setdefault(number, []).append((key, name))
                    else:
                        units.append((key, number, _normal_title(title), name))
                self.members(
                    key, name, organ, "HistoricoComposicao", f"idOrgao={organ_id}", office=False
                )
        self.parents(committees, units, links)
        for section, fallback in BODY_SECTIONS.items():
            value = root.get(section)
            if value is None:
                continue
            organ = _object(value, section)
            rows_key = (
                "HistoricoComposicaoCPC"
                if organ.get("HistoricoComposicaoCPC") is not None
                else "HistoricoComposicao"
            )
            detail = self.detail(organ, None, rows_key)
            if detail is None:
                continue
            organ_id, _, title = detail
            key = f"orgao:{self.code}:{organ_id}"
            name = self.organ(
                key,
                title or fallback,
                Entity.Classification.PARLIAMENTARY_BODY,
                fallback,
                dataset=BODIES,
                reference=f"OrgaoComposicao{self.code} / {section} idOrgao={organ_id}",
            )
            self.members(key, name, organ, rows_key, f"idOrgao={organ_id}", office=False)
        mesa = _object(root.get("MesaAR"), "MesaAR")
        key = f"mesa:{self.code}"
        name = self.organ(
            key,
            "Mesa da Assembleia da República",
            Entity.Classification.PARLIAMENTARY_BODY,
            "Mesa da Assembleia da República",
            dataset=BODIES,
            reference=f"OrgaoComposicao{self.code} / MesaAR",
        )
        self.members(key, name, mesa, "HistoricoComposicaoMesa", "MesaAR", office=True)

    def detail(
        self, organ: JSONObject, expected: str | None, rows_key: str = "HistoricoComposicao"
    ) -> tuple[str, int | None, str] | None:
        raw = organ.get("DetalheOrgao")
        detail = _object(raw, "DetalheOrgao") if raw is not None else {}
        organ_id = _optional_id(detail.get("idOrgao"), "idOrgao")
        if not organ_id:
            if _rows(organ.get(rows_key), rows_key, optional=True):
                raise ParliamentImportError("Organ members without an official organ id.")
            return None
        if expected is not None and detail.get("siglaOrgao") != expected:
            raise ParliamentImportError("Unexpected organ type in the composition section.")
        title = _clean(detail.get("nomeSigla"), "nomeSigla", optional=True)
        return organ_id, _number(detail.get("numeroOrgao"), "numeroOrgao"), title

    def parents(
        self,
        committees: dict[int, list[tuple[str, str]]],
        units: list[tuple[str, int | None, str, str]],
        links: list[tuple[int, str, int]],
    ) -> None:
        """Parent committees only from AtividadeDeputado ScmComCd; conflicts stay unknown."""
        found: dict[str, set[tuple[str, str, int]]] = {}
        for child_number, title, parent_number in links:
            candidates = [unit for unit in units if unit[1] == child_number]
            if len(candidates) > 1:
                candidates = [unit for unit in candidates if unit[2] == title]
            parents = committees.get(parent_number, [])
            if len(candidates) == 1 and len(parents) == 1:
                found.setdefault(candidates[0][0], set()).add((*parents[0], parent_number))
        for key, options in found.items():
            if len(options) != 1:
                continue
            parent_key, parent_name, parent_number = options.pop()
            organ = self.organs[key]
            self.organs[key] = replace(
                organ,
                parent=parent_key,
                dataset=ACTIVITY,
                source_url=self.urls[ACTIVITY],
                reference=(
                    f"AtividadeDeputado{self.code} / Scgt ScmComCd={parent_number}; "
                    f"OrgaoComposicao{self.code} / {key.rsplit(':', 1)[1]}"
                )[:160],
                passage=f"{organ.name}. Tipo: {organ.role}. Integra: {parent_name}.",
            )

    def members(
        self,
        key: str,
        name: str,
        organ: JSONObject,
        rows_key: str,
        record: str,
        *,
        office: bool,
    ) -> None:
        location = f"OrgaoComposicao{self.code} / {record}"
        for row in _rows(organ.get(rows_key), rows_key, optional=True):
            cadastro = _optional_id(row.get("depCadId"), "depCadId")
            dep = _identifier(row.get("depId"), "depId")
            parliamentary = _clean(row.get("depNomeParlamentar"), "depNomeParlamentar")
            if cadastro:
                self.fallback_names.setdefault(cadastro, parliamentary)
                person = f"depCadId={cadastro}"
                identity_line = f"Identificador AR (DepCadId): {cadastro}."
            else:
                # Staff members (e.g. on the Conselho de Administração) have no DepCadId:
                # verifiable claims publish with source-scoped person identities.
                person = f"depId={dep}"
                identity_line = f"Registo AR sem DepCadId (depId): {dep}."
            # (facet, kind, role, role class, start, end, passage line) per source interval.
            entries: list[tuple[str, str, str, str, date | None, date | None, str]] = []
            for situation in _rows(row.get("depSituacao"), "depSituacao", optional=True):
                des = _clean(situation.get("sioDes"), "sioDes")
                if des not in ACTIVE_MEMBERSHIP:
                    continue
                kind_of = _clean(situation.get("sioTipMem"), "sioTipMem", optional=True)
                start = _date(situation.get("sioDtInicio"), "sioDtInicio")
                end = _date(situation.get("sioDtFim"), "sioDtFim")
                if self.valid(start, end):
                    substitute = kind_of == "Suplente"
                    entries.append(
                        (
                            "situacao",
                            MEMBERSHIP,
                            kind_of,
                            RoleClass.SUBSTITUTE if substitute else RoleClass.MEMBER,
                            start,
                            end,
                            f"Composição: {kind_of or des}, {_period(start, end)}.",
                        )
                    )
            for cargo in _rows(row.get("depCargo"), "depCargo", optional=True):
                role = _clean(cargo.get("carDes"), "carDes")
                start = _date(cargo.get("carDtInicio"), "carDtInicio")
                end = _date(cargo.get("carDtFim"), "carDtFim")
                if self.valid(start, end):
                    entries.append(
                        (
                            "cargo",
                            PUBLIC_OFFICE if office else MEMBERSHIP,
                            role,
                            role_class(role),
                            start,
                            end,
                            f"Cargo: {role}, {_period(start, end)}.",
                        )
                    )
            for facet, kind, role, classification, start, end, line in entries:
                self.person_claim(
                    organ=key,
                    cadastro_id=cadastro,
                    subject_name=parliamentary,
                    subject_reference=f"{location} / depId={dep}",
                    facet=facet,
                    kind=kind,
                    role=role,
                    role_class=classification,
                    start=start,
                    end=end,
                    dataset=BODIES,
                    reference=f"{location} / {person}",
                    passage=[
                        f"Órgão: {name}.",
                        f"Nome parlamentar: {parliamentary}.",
                        line,
                        identity_line,
                    ],
                )

    # DelegacaoPermanente: member ids are DepIds, often from earlier legislatures.
    def delegations(self, rows: list[JSONObject], deputies: dict[str, tuple[str, str]]) -> None:
        for row in rows:
            delegation = _identifier(row.get("Id"), "Id")
            title = _clean(row.get("Nome"), "Nome")
            elected = _dmy(row.get("DataEleicao"), "DataEleicao")
            key = f"delegacao:{self.code}:{delegation}"
            name = self.organ(
                key,
                f"Delegação da Assembleia da República — {title}",
                Entity.Classification.PARLIAMENTARY_DELEGATION,
                "Delegação parlamentar permanente",
                dataset=DELEGATIONS,
                reference=f"DelegacaoPermanente{self.code} / Id={delegation}",
            )
            location = f"DelegacaoPermanente{self.code} / Id={delegation}"
            # A deputy can be listed once per mandate DepId (e.g. an XI and an XII DepId) with
            # the same role and dates: one claim citing every DepId, not two conflicting ones.
            grouped: dict[tuple[str, str, date | None, date | None], tuple[list[str], list[str]]]
            grouped = {}
            for member in _rows(row.get("Composicao"), "Composicao", optional=True):
                published = _clean(member.get("Nome"), "Nome")
                dep = _optional_id(member.get("Id"), "Composicao.Id")
                cadastro = deputies.get(dep, ("", ""))[0] if dep else ""
                role = _clean(member.get("Cargo"), "Cargo", optional=True)
                start = _dmy(member.get("DataInicio"), "DataInicio")
                end = _dmy(member.get("DataFim"), "DataFim")
                if not self.valid(start, end):
                    continue
                if cadastro:
                    person = f"cad:{cadastro}"
                else:
                    person = f"dep:{dep}" if dep else f"nome:{published}"
                deps, names = grouped.setdefault((person, role, start, end), ([], []))
                if dep and dep not in deps:
                    deps.append(dep)
                if published not in names:
                    names.append(published)
            for (person, role, start, end), (deps, names) in grouped.items():
                cadastro = person.removeprefix("cad:") if person.startswith("cad:") else ""
                dep_ref = f"DepId={','.join(deps) or 'sem id'}"
                lines = [
                    f"Delegação: {name}.",
                    f"Nome: {'; '.join(names)}.",
                    f"Cargo: {role or 'não indicado'}, {_period(start, end)}.",
                ]
                if elected is not None:
                    lines.append(f"Data de eleição da delegação: {_pt(elected)}.")
                if cadastro:
                    lines.append(f"Identificador AR (DepCadId): {cadastro}.")
                self.person_claim(
                    organ=key,
                    cadastro_id=cadastro,
                    subject_name="; ".join(names),
                    # Name-only rows without any id are told apart by the published name.
                    subject_reference=(
                        f"{location} / {dep_ref}" + ("" if deps else f" / Nome={names[0]}")
                    )[:240],
                    facet="delegacao",
                    kind=MEMBERSHIP,
                    role=role,
                    role_class=role_class(role) or RoleClass.MEMBER,
                    start=start,
                    end=end,
                    dataset=DELEGATIONS,
                    reference=f"{location} / {dep_ref}"
                    + (f" / DepCadId={cadastro}" if cadastro else ""),
                    passage=lines,
                )

    def snapshot(self) -> BodiesSnapshot:
        names = {**self.fallback_names, **self.names}
        return BodiesSnapshot(
            legislature=self.legislature,
            as_of=self.as_of,
            organs=tuple(self.organs[key] for key in sorted(self.organs)),
            claims=tuple(self.claims[key] for key in sorted(self.claims)),
            names=names,
            skipped_intervals=self.skipped,
        )


def build_snapshot(
    *,
    legislature: str,
    as_of: date,
    roster: Download,
    bodies: Download,
    activity: Download,
    delegations: Download | None = None,
    # (code, InformacaoBase) of other legislatures, to resolve delegation member DepIds.
    other_rosters: tuple[tuple[str, Download], ...] = (),
) -> BodiesSnapshot:
    """Validate every file and build the complete snapshot without database access."""
    validate_file_legislature(legislature)
    root = _object(_load(roster, "roster"), "roster")
    info = _legislature(root, legislature)
    if as_of < info.start:
        raise ParliamentImportError("Requested day is before the legislature.")
    urls = {ROSTER: roster.url, BODIES: bodies.url, ACTIVITY: activity.url}
    if delegations is not None:
        urls[DELEGATIONS] = delegations.url
    builder = _Builder(info, as_of, urls)
    builder.roster(root)
    links = builder.activity(_load(activity, "activity"))
    builder.bodies(_object(_load(bodies, "bodies"), "bodies"), links)
    if delegations is not None:
        deputies = dep_map(roster, legislature)
        for code, download in other_rosters:
            for dep, entry in dep_map(download, code).items():
                if deputies.setdefault(dep, entry)[0] != entry[0]:
                    raise ParliamentImportError("A DepId maps to two cadastro identifiers.")
                builder.fallback_names.setdefault(*entry)
        builder.delegations(delegation_rows(delegations, legislature), deputies)
    return builder.snapshot()


def latest_legislature(activity: Download) -> str:
    """The newest legislature: every AtividadeDeputado file lists that roster's deputies."""
    codes = {
        _object(entry.get("Deputado"), "Deputado").get("LegDes")
        for entry in _rows(_load(activity, "activity"), "AtividadeDeputado")
    }
    if len(codes) != 1:
        raise ParliamentImportError("Activity file does not name a single current legislature.")
    code = codes.pop()
    if not isinstance(code, str):
        raise ParliamentImportError("Invalid current legislature in the activity file.")
    validate_file_legislature(code)
    return code


def fetch_snapshot(*, legislature: str, as_of: date) -> BodiesSnapshot:
    validate_file_legislature(legislature)
    roster = discover_download("roster", legislature)
    bodies = discover_download("bodies", legislature)
    activity = discover_download("activity", legislature)
    delegations = None
    others: list[tuple[str, Download]] = []
    if _rank(legislature) >= FIRST_DELEGATIONS:
        delegations = discover_download("delegations", legislature)
        wanted = delegation_dep_ids(delegations, legislature)
        known = set(dep_map(roster, legislature))
        # Member DepIds come from other legislatures' rosters, mostly nearby ones: fetch
        # the nearest InformacaoBase files first, and only as many as needed.
        if not wanted <= known:
            rank = _rank(legislature)
            codes = sorted(
                earlier_legislatures(legislature)
                + later_legislatures(legislature, latest_legislature(activity)),
                key=lambda code: abs(_rank(code) - rank),
            )
            for code in codes:
                if wanted <= known:
                    break
                download = discover_download("roster", code)
                others.append((code, download))
                known |= set(dep_map(download, code))
    return build_snapshot(
        legislature=legislature,
        as_of=as_of,
        roster=roster,
        bodies=bodies,
        activity=activity,
        delegations=delegations,
        other_rosters=tuple(others),
    )


def _people(snapshot: BodiesSnapshot, claims: list[Claim]) -> dict[str, SourceIdentity]:
    wanted = sorted({claim.cadastro_id for claim in claims if claim.cadastro_id}, key=int)
    known: set[str] = set()
    for chunk in batched(wanted, 5000, strict=False):
        known |= set(
            SourceIdentity.objects.filter(
                source=IdentityScheme.PARLIAMENT, external_id__in=chunk
            ).values_list("external_id", flat=True)
        )
    for cadastro in wanted:
        if cadastro not in known:
            if not snapshot.names.get(cadastro):
                raise ParliamentImportError("A deputy has no published name in the snapshot.")
            ar_person(cadastro, snapshot.names[cadastro])
    result: dict[str, SourceIdentity] = {}
    for chunk in batched(wanted, 5000, strict=False):
        for identity in SourceIdentity.objects.select_related("entity", "reviewed_by").filter(
            source=IdentityScheme.PARLIAMENT, external_id__in=chunk
        ):
            result[identity.external_id] = identity
    return result


def _revised(item: ObservationInput) -> ObservationInput:
    """Revision = digest of every substantive field, so any source change is a new revision."""
    digest = {
        field.name: getattr(getattr(item, field.name), "pk", getattr(item, field.name))
        for field in fields(item)
        if field.name not in {"revision", "retrieved_at"}
    }
    revision = hashlib.sha256(
        json.dumps(digest, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    return replace(item, revision=revision)


@import_transaction()
def apply_snapshot(snapshot: BodiesSnapshot) -> dict[str, int]:
    """Atomic: organ entities, structure claims and person claims for one legislature."""
    info = snapshot.legislature
    for record in (*snapshot.organs, *snapshot.claims):
        validate_url(record.source_url, FETCH_DATASETS[record.dataset], info.code)
    institution = ar_institution()
    term = legislature_term(info.code, info.start, info.end)
    retrieved_at = timezone.now()
    organs = official_entities_bulk(
        IdentityScheme.PARLIAMENT,
        {
            organ.key: (organ.name, Entity.Kind.ORGANISATION, organ.classification)
            for organ in snapshot.organs
        },
    )
    organ_identities = {
        identity.external_id: identity
        for identity in SourceIdentity.objects.select_related("entity").filter(
            source=IdentityScheme.PARLIAMENT, external_id__in=list(organs)
        )
    }
    structure: list[ObservationInput] = []
    for organ in snapshot.organs:
        dataset = DATASETS[organ.dataset]
        item = ObservationInput(
            external_id=organ.key,
            revision="",
            identity=organ_identities[organ.key],
            category=SourceObservation.Category.ORGANISATION_STRUCTURE,
            passage=organ.passage,
            source_url=organ.source_url,
            publisher=dataset.publisher,
            reference=organ.reference,
            title=dataset.title,
            object=organs[organ.parent] if organ.parent else institution,
            kind=Relationship.Kind.PART_OF,
            dataset=organ.dataset,
            role=organ.role,
            term=term,
            temporal_status=temporal_status(None, None, info.end, snapshot.as_of),
            retrieved_at=retrieved_at,
        )
        structure.append(_revised(item))
    # Plenary mandates have one owner: import_parliament's complete roster history.
    claims = list(snapshot.claims)
    people = _people(snapshot, claims)
    observations: list[ObservationInput] = []
    for claim in claims:
        dataset = DATASETS[claim.dataset]
        item = ObservationInput(
            external_id=claim.external_id,
            revision="",
            identity=people[claim.cadastro_id] if claim.cadastro_id else None,
            subject_name=claim.subject_name,
            subject_reference=claim.subject_reference,
            category=SourceObservation.Category.PARLIAMENT_BODY,
            passage=claim.passage,
            source_url=claim.source_url,
            publisher=dataset.publisher,
            reference=claim.reference,
            title=dataset.title,
            effective_start=claim.start,
            effective_end=claim.end,
            object=organs[claim.organ],
            kind=claim.kind,
            dataset=claim.dataset,
            role=claim.role,
            role_class=claim.role_class,
            term=term,
            temporal_status=claim.temporal_status,
            retrieved_at=retrieved_at,
        )
        observations.append(_revised(item))
    result = {"organs": len(snapshot.organs), "claims": len(claims)}
    for prefix, items in (("bodies-structure", structure), ("bodies", observations)):
        for key, value in sync_observations(
            source=EnrichmentSource.PARLIAMENT,
            scope=f"{prefix}:{info.code}",
            observations=tuple(items),
            as_of=snapshot.as_of,
        ).items():
            result[key] = result.get(key, 0) + value
    return result
