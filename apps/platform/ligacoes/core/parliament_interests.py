"""Historic AR registo de interesses (RegistoInteresses<Leg>) as declared interests.

Schema versions: V1 (XI), V2 (XII, XIII), V3 (XIV) and V5 (XIV, XV); earlier files
are empty shells. Only activity, social-position, company, support and service rows
are read, through an explicit field allowlist. Spouse, marital and personal fields,
fiscal/staff numbers, remuneration flags, holding values and "other situations" are
never read or retained. Rows marked as the spouse's, party offices and any retained
text holding a 9-digit number are skipped. Organisations use published names only
(no NIPC); verifiable declared interests publish automatically with source passages.
"""

import calendar
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from django.utils import timezone

from .catalogue import DATASETS
from .enrichment import ObservationInput, sync_scoped_snapshot
from .identity import ar_person
from .models import (
    DatePrecision,
    EnrichmentSource,
    IdentityScheme,
    Relationship,
    SourceIdentity,
    SourceObservation,
    TemporalStatus,
    Term,
    import_transaction,
)
from .parliament_fetch import Download, ParliamentImportError, discover_download
from .parliament_parse import (
    JSONObject,
    JSONValue,
    _identifier,
    _json,
    _object,
    _rows,
    canonical_json,
)

DATASET = "ar_registo_interesses"
VERSIONS = (
    "RegistoInteressesV1",
    "RegistoInteressesV2",
    "RegistoInteressesV3",
    "RegistoInteressesV5",
)
# At most three retained cells enter a passage, which must stay within 10 000 characters.
TEXT_LIMIT = 2000
ROLE_LIMIT = 240
OBJECT_LIMIT = 300

ACTIVITY = Relationship.Kind.PROFESSIONAL_ACTIVITY
DIRECTORSHIP = Relationship.Kind.DIRECTORSHIP
SHAREHOLDING = Relationship.Kind.SHAREHOLDING

# Normalised (accent-free, lower-case, punctuation as spaces) "nothing to declare" cells.
NIL = frozenset(
    {
        "",
        "nada a declarar",
        "nada declarar",
        "nada a registar",
        "nada consta",
        "nada",
        "nao aplicavel",
        "nao se aplica",
        "na",
        "n a",
        "nenhum",
        "nenhuma",
        "nao",
        "sem",
        "inexistente",
        "nao tem",
        "nao existe",
        "nao existem",
        "nao ha",
    }
)
SPOUSE = re.compile(r"\b(?:conjuge|marido|esposa|esposo|uniao de facto|unid[oa] de facto)\b")
PARTY = re.compile(
    r"\bpartid|\bjuventude (?:socialista|popular|social democrata|comunista)\b"
    r"|\bcomissao politica\b|\btrabalhadores social democratas\b|\bbloco de esquerda\b"
    r"|\biniciativa liberal\b|\b(?:ps|psd|cds|cds pp|pcp|pev|jsd|jcp|js)\b"
)
PARTY_NAMES = frozenset({"chega", "livre", "pan", "be", "il", "jpp", "ch", "l"})
# Parliamentary groups are the only political grouping kept (contract §1.3).
GROUP = re.compile(r"\bgrupo parlamentar\b|\bgp\b")
# The existing guard, also catching grouped digits ("123 456 789").
IDENTIFIER = re.compile(r"(?<!\d)(?:\d[ .]?){8}\d(?!\d)")
MONTHS = {
    "janeiro": 1,
    "jan": 1,
    "fevereiro": 2,
    "fev": 2,
    "marco": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "maio": 5,
    "mai": 5,
    "junho": 6,
    "jun": 6,
    "julho": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "setembro": 9,
    "set": 9,
    "outubro": 10,
    "out": 10,
    "novembro": 11,
    "nov": 11,
    "dezembro": 12,
    "dez": 12,
}
LEGISLATURE = re.compile(r"Cons|IA|IB|[IVXLCDM]{1,12}")

type DateParts = tuple[int, int | None, int | None]


@dataclass(frozen=True)
class InterestRow:
    """One retained declaration row, already minimised; no database objects."""

    key: str
    kind: str
    role: str
    object_name: str
    passage: str
    reference: str
    start: date | None
    start_precision: str
    end: date | None
    end_precision: str
    temporal_status: str
    declared_on: date | None


@dataclass(frozen=True)
class Declarant:
    cadastro_id: str
    name: str
    rows: tuple[InterestRow, ...]


@dataclass(frozen=True)
class InterestsSnapshot:
    legislature: str
    url: str
    as_of: date
    retrieved_at: datetime
    declarants: tuple[Declarant, ...]
    excluded: tuple[tuple[str, int], ...]

    @property
    def row_count(self) -> int:
        return sum(len(declarant.rows) for declarant in self.declarants)


def validate_legislature_code(legislature: str) -> None:
    if not LEGISLATURE.fullmatch(legislature):
        raise ParliamentImportError("Legislature must be Cons, IA, IB or a Roman numeral.")


def _normalise(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", plain).split())


def _cell(row: JSONObject, field: str) -> str:
    """Allowlisted text cell; anything but text or null means the schema changed."""
    value = row.get(field)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > TEXT_LIMIT:
        raise ParliamentImportError(f"Invalid text in {field}.")
    return " ".join(value.split())


def _spouse_marked(row: JSONObject) -> bool:
    # Every text cell of the row is screened, including cells that are not retained
    # (participation, location, nature), because any of them may mark the holder.
    return any(
        isinstance(value, str) and SPOUSE.search(_normalise(value)) for value in row.values()
    )


def _party(*texts: str) -> bool:
    for text in texts:
        normalised = _normalise(text)
        if GROUP.search(normalised):
            continue
        if normalised in PARTY_NAMES or PARTY.search(normalised):
            return True
    return False


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "\u2026"


NUMERIC_DATES: tuple[tuple[re.Pattern[str], int, int | None, int | None], ...] = (
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), 1, 2, 3),
    (re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"), 1, 2, 3),
    (re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})"), 1, 2, 3),
    (re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})"), 3, 2, 1),
    (re.compile(r"(\d{4})[-/](\d{1,2})"), 1, 2, None),
    (re.compile(r"(\d{1,2})[-/](\d{4})"), 2, 1, None),
    (re.compile(r"(\d{4})"), 1, None, None),
)


def _groups(text: str) -> DateParts | None:
    for pattern, year, month, day in NUMERIC_DATES:
        match = pattern.fullmatch(text)
        if match:
            return (
                int(match.group(year)),
                int(match.group(month)) if month else None,
                int(match.group(day)) if day else None,
            )
    named = re.fullmatch(r"([a-z]+)(?: de)? (\d{4})", _normalise(text))
    if named and named.group(1) in MONTHS:
        return int(named.group(2)), MONTHS[named.group(1)], None
    return None


def _parts(raw: str) -> DateParts | None:
    """Known free-text date shapes; two-digit years and anything else stay unknown."""
    parts = _groups(re.sub(r"[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?$", "", raw.strip()))
    if parts is None:
        return None
    year, month, day = parts
    if not 1900 <= year <= 2100 or (month is not None and not 1 <= month <= 12):
        return None
    last = calendar.monthrange(year, month)[1] if month is not None else 31
    if day is not None and not 1 <= day <= last:
        return None
    return parts


def _bound(parts: DateParts | None, *, end: bool) -> tuple[date | None, str]:
    """Month/year starts are the first day; month/year ends the last (contract §3)."""
    if parts is None:
        return None, DatePrecision.DAY
    year, month, day = parts
    if month is None:
        return (date(year, 12, 31) if end else date(year, 1, 1)), DatePrecision.YEAR
    if day is None:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, last if end else 1), DatePrecision.MONTH
    return date(year, month, day), DatePrecision.DAY


def _period(raw: str) -> tuple[DateParts | None, DateParts | None]:
    """Free-text 'Data' cells: a date, 'desde X' or 'X a Y'; anything else is unknown."""
    text = raw.strip()
    since = re.fullmatch(r"(?:desde|a partir de)\s+(.+)", text, flags=re.IGNORECASE)
    if since:
        return _parts(since.group(1)), None
    pieces = re.split(r"\s+(?:a|até|-|\u2013)\s+", text, flags=re.IGNORECASE)
    if len(pieces) == 2:
        first, second = _parts(pieces[0]), _parts(pieces[1])
        if first and second:
            return first, second
    return _parts(text), None


def _iso_day(raw: str) -> date | None:
    parts = _parts(raw)
    if parts is None or parts[2] is None:
        return None
    return _bound(parts, end=False)[0]


@dataclass(frozen=True)
class _Context:
    legislature: str
    cadastro_id: str
    version: str
    declared_on: date | None
    as_of: date


class _Collector:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, InterestRow]] = {}
        self.names: dict[str, str] = {}
        self.excluded: Counter[str] = Counter()

    def declarant(self, cadastro_id: str, name: str) -> None:
        if not name or len(name) > 240:
            raise ParliamentImportError("Invalid declarant name.")
        self.names.setdefault(cadastro_id, name)
        self.rows.setdefault(cadastro_id, {})

    def add(
        self,
        context: _Context,
        *,
        section: str,
        line: int,
        row_id: str,
        kind: str,
        role: str,
        entity: str,
        extra: tuple[tuple[str, str], ...] = (),
        start_raw: str = "",
        end_raw: str = "",
        period_raw: str = "",
        screened: JSONObject | None = None,
        party_texts: tuple[str, ...] = (),
    ) -> None:
        if screened is not None and _spouse_marked(screened):
            self.excluded["spouse"] += 1
            return
        retained = (role, entity, start_raw, end_raw, period_raw, *(v for _, v in extra))
        if any(SPOUSE.search(_normalise(text)) for text in retained):
            self.excluded["spouse"] += 1
            return
        if _normalise(role) in NIL and _normalise(entity) in NIL:
            self.excluded["empty"] += 1
            return
        if _party(role, entity, *party_texts):
            self.excluded["party"] += 1
            return
        if any(IDENTIFIER.search(text) for text in retained):
            self.excluded["identifier"] += 1
            return
        role = "" if _normalise(role) in NIL else role
        entity = "" if _normalise(entity) in NIL else entity
        if period_raw:
            start_parts, end_parts = _period(period_raw)
        else:
            start_parts, end_parts = _parts(start_raw), _parts(end_raw)
        start, start_precision = _bound(start_parts, end=False)
        end, end_precision = _bound(end_parts, end=True)
        if start is not None and end is not None and end < start:
            # A reversed interval is a source error; keep only the start.
            end, end_precision = None, DatePrecision.DAY
        status = (
            TemporalStatus.ENDED
            if end is not None and end < context.as_of
            else TemporalStatus.UNKNOWN
        )
        passage = _passage(
            context,
            section=section,
            role=role,
            entity=entity,
            extra=extra,
            start_raw=start_raw,
            end_raw=end_raw,
            period_raw=period_raw,
        )
        if len(passage) > 10000:
            raise ParliamentImportError("Declaration passage exceeds its limit.")
        reference = (
            f"RegistoInteresses{context.legislature} / CadId={context.cadastro_id} / "
            f"{section} / linha {line} / versão {context.version}"
        )
        if len(reference) > 160:
            raise ParliamentImportError("Declaration reference exceeds its limit.")
        # Content keys keep unchanged rows stable when other rows are removed or reordered,
        # and collapse the identical rows that V3 repeats in every version.
        content: list[JSONValue] = [section, role, entity, start_raw, end_raw, period_raw]
        content.extend(value for _, value in extra)
        key = (
            f"{section}:{row_id}"
            if row_id
            else f"{section}:h" + hashlib.sha256(canonical_json(content).encode()).hexdigest()[:24]
        )
        self.rows[context.cadastro_id].setdefault(
            key,
            InterestRow(
                key=key,
                kind=kind,
                role=_clip(role, ROLE_LIMIT),
                object_name=_clip(entity, OBJECT_LIMIT),
                passage=passage,
                reference=reference,
                start=start,
                start_precision=start_precision,
                end=end,
                end_precision=end_precision,
                temporal_status=status,
                declared_on=context.declared_on,
            ),
        )

    def declarants(self) -> tuple[Declarant, ...]:
        return tuple(
            Declarant(cadastro_id, self.names[cadastro_id], tuple(rows.values()))
            for cadastro_id, rows in sorted(self.rows.items(), key=lambda item: int(item[0]))
            if rows
        )


LEADS = {
    "activity": "o cargo, função ou atividade",
    "social": "o cargo social",
    "society": "a participação na sociedade",
    "support": "o apoio ou benefício",
    "service": "o serviço prestado",
}
SECTION_LEADS = {
    "rgiActividades": "activity",
    "Activities": "activity",
    "GenCargosMenosTresAnos": "activity",
    "GenCargosMaisTresAnos": "activity",
    "rgiCargosSociais": "social",
    "SocialPositions": "social",
    "rgiSociedades": "society",
    "Societies": "society",
    "GenSociedade": "society",
    "rgiApoiosBeneficios": "support",
    "Supports": "support",
    "GenApoios": "support",
    "rgiServicosPrestados": "service",
    "ServicesProvided": "service",
    "GenServicoPrestado": "service",
}


def _passage(
    context: _Context,
    *,
    section: str,
    role: str,
    entity: str,
    extra: tuple[tuple[str, str], ...],
    start_raw: str,
    end_raw: str,
    period_raw: str,
) -> str:
    lead = SECTION_LEADS[section]
    text = f"Registo de interesses da legislatura {context.legislature}: declarou {LEADS[lead]}"
    if lead == "society":
        text += f" «{entity}»."
    else:
        text += f" «{role}»" if role else ""
        text += f" na entidade «{entity}»." if entity else "."
    for label, value in extra:
        if value:
            text += f" {label}: {value}."
    for label, value in (
        ("Início declarado", start_raw),
        ("Termo declarado", end_raw),
        ("Data declarada", period_raw),
    ):
        if value:
            text += f" {label}: {value}."
    if context.declared_on is not None:
        text += f" Versão da declaração: {context.declared_on.isoformat()}."
    return text


def _lines(value: str) -> list[str]:
    """Free-text cells (V1 activities, V1/V2 supports and services): one row per line."""
    result: list[str] = []
    for line in value.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*\u2022\u2013]\s*|\d{1,2}[.)]\s+)", "", line)
        result.append(" ".join(cleaned.split()))
    return result


def _free_text(
    collector: _Collector, context: _Context, rgi: JSONObject, section: str, kind: str
) -> None:
    value = rgi.get(section)
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 20000:
        raise ParliamentImportError(f"Invalid text in {section}.")
    for line, text in enumerate(_lines(value), start=1):
        if not text:
            continue
        if len(text) > TEXT_LIMIT:
            raise ParliamentImportError(f"Invalid text in {section}.")
        collector.add(
            context, section=section, line=line, row_id="", kind=kind, role=text, entity=""
        )


def _row_id(row: JSONObject, field: str) -> str:
    """Source row id, or "" when absent (V5 exports every row id as 0): key by content."""
    value = row.get(field)
    if value is None or (type(value) in (int, float) and value == 0):
        return ""
    return _identifier(value, field)


def _version_1_2(collector: _Collector, record: JSONObject, legislature: str, as_of: date) -> None:
    cadastro_id = _identifier(record.get("cadId"), "cadId")
    entries = [
        rgi
        for rgi in _rows(record.get("cadRgi"), "cadRgi", optional=True)
        if _cell(rgi, "rgiLegDes") == legislature
    ]
    if not entries:
        return
    collector.declarant(cadastro_id, _cell(record, "cadNomeCompleto"))
    for rgi in entries:
        if _identifier(rgi.get("rgiCadId"), "rgiCadId") != cadastro_id:
            raise ParliamentImportError("Declaration belongs to another cadastro id.")
        version_raw = _cell(rgi, "rgiDataVersao")
        declared_on = _iso_day(version_raw)
        context = _Context(
            legislature,
            cadastro_id,
            declared_on.isoformat() if declared_on else _row_id(rgi, "rgiId") or "?",
            declared_on,
            as_of,
        )
        activities = rgi.get("rgiActividades")
        if isinstance(activities, str):
            _free_text(collector, context, rgi, "rgiActividades", ACTIVITY)
        else:
            for line, row in enumerate(_rows(activities, "rgiActividades", optional=True), 1):
                collector.add(
                    context,
                    section="rgiActividades",
                    line=line,
                    row_id=_row_id(row, "rgaId"),
                    kind=ACTIVITY,
                    role=_cell(row, "rgaActividade"),
                    entity="",
                    start_raw=_cell(row, "rgaDataInicio"),
                    end_raw=_cell(row, "rgaDataFim"),
                    screened=row,
                )
        for line, row in enumerate(
            _rows(rgi.get("rgiCargosSociais"), "rgiCargosSociais", optional=True), 1
        ):
            collector.add(
                context,
                section="rgiCargosSociais",
                line=line,
                row_id=_row_id(row, "rgcId"),
                kind=DIRECTORSHIP,
                role=_cell(row, "rgcCargo"),
                entity=_cell(row, "rgcEntidade"),
                start_raw=_cell(row, "rgcDataInicio"),
                end_raw=_cell(row, "rgcDataFim"),
                screened=row,
            )
        for line, row in enumerate(
            _rows(rgi.get("rgiSociedades"), "rgiSociedades", optional=True), 1
        ):
            collector.add(
                context,
                section="rgiSociedades",
                line=line,
                row_id=_row_id(row, "rgsId"),
                kind=SHAREHOLDING,
                role="",
                entity=_cell(row, "rgsEntidade"),
                screened=row,
            )
        _free_text(collector, context, rgi, "rgiApoiosBeneficios", ACTIVITY)
        _free_text(collector, context, rgi, "rgiServicosPrestados", ACTIVITY)


def _version_3(collector: _Collector, record: JSONObject, legislature: str, as_of: date) -> None:
    cadastro_id = _identifier(record.get("RecordId"), "RecordId")
    versions = _rows(record.get("RecordInterests"), "RecordInterests", optional=True)
    if not versions:
        return
    collector.declarant(cadastro_id, _cell(record, "FullName"))
    for index, version in enumerate(versions, start=1):
        # Position dates date the mandate, not the declaration: label only.
        label_day = _iso_day(_cell(version, "PositionChangedDate")) or _iso_day(
            _cell(version, "PositionBeginDate")
        )
        label = f"{index} ({label_day.isoformat()})" if label_day else str(index)
        context = _Context(legislature, cadastro_id, label, None, as_of)
        sections: tuple[tuple[str, str, str, str], ...] = (
            ("Activities", "Activity", "Entity", ACTIVITY),
            ("SocialPositions", "Position", "Entity", DIRECTORSHIP),
            ("Societies", "", "Entity", SHAREHOLDING),
            ("Supports", "Support", "", ACTIVITY),
            ("ServicesProvided", "Service", "", ACTIVITY),
        )
        for section, role_field, entity_field, kind in sections:
            for line, row in enumerate(_rows(version.get(section), section, optional=True), 1):
                row_type = row.get("Type")
                if row_type is not None and (type(row_type) is not int or row_type not in (0, 1)):
                    # 2 marks undocumented "nothing to declare" rows.
                    collector.excluded["empty"] += 1
                    continue
                collector.add(
                    context,
                    section=section,
                    line=line,
                    row_id="",
                    kind=kind,
                    role=_cell(row, role_field) if role_field else "",
                    entity=_cell(row, entity_field) if entity_field else "",
                    start_raw=_cell(row, "BeginDate") if section == "Activities" else "",
                    end_raw=_cell(row, "EndDate") if section == "Activities" else "",
                    screened=row,
                )


def _version_5(collector: _Collector, record: JSONObject, legislature: str, as_of: date) -> None:
    # GenDadosPessoais (personal and spouse data) is never read.
    if _cell(record, "Legislatura") != legislature:
        return
    cadastro_id = _identifier(record.get("IdCadastroGODE"), "IdCadastroGODE")
    fact = record.get("FactoDeclaracao")
    label_day = None
    if fact is not None:
        declaration = _object(fact, "FactoDeclaracao")
        label_day = _iso_day(_cell(declaration, "DataAlteracaoFuncao")) or _iso_day(
            _cell(declaration, "DataInicioFuncao")
        )
    context = _Context(
        legislature, cadastro_id, label_day.isoformat() if label_day else "V5", None, as_of
    )
    collector.declarant(cadastro_id, _cell(record, "NomeIdentificacao"))
    for section in ("GenCargosMenosTresAnos", "GenCargosMaisTresAnos"):
        for line, row in enumerate(_rows(record.get(section), section, optional=True), 1):
            collector.add(
                context,
                section=section,
                line=line,
                row_id=_row_id(row, "Id"),
                kind=ACTIVITY,
                role=_cell(row, "CargoFuncaoAtividade"),
                entity=_cell(row, "Entidade"),
                start_raw=_cell(row, "DataInicio"),
                end_raw=_cell(row, "DataTermo"),
                screened=row,
                party_texts=(_cell(row, "Natureza"),),
            )
    for line, row in enumerate(_rows(record.get("GenSociedade"), "GenSociedade", optional=True), 1):
        collector.add(
            context,
            section="GenSociedade",
            line=line,
            row_id=_row_id(row, "Id"),
            kind=SHAREHOLDING,
            role="",
            entity=_cell(row, "Sociedade"),
            screened=row,
            party_texts=(_cell(row, "Natureza"),),
        )
    for line, row in enumerate(_rows(record.get("GenApoios"), "GenApoios", optional=True), 1):
        collector.add(
            context,
            section="GenApoios",
            line=line,
            row_id=_row_id(row, "Id"),
            kind=ACTIVITY,
            role=_cell(row, "Apoio"),
            entity=_cell(row, "Entidade"),
            extra=(("Natureza do benefício", _cell(row, "NaturezaBeneficio")),),
            period_raw=_cell(row, "Data"),
            screened=row,
        )
    for line, row in enumerate(
        _rows(record.get("GenServicoPrestado"), "GenServicoPrestado", optional=True), 1
    ):
        collector.add(
            context,
            section="GenServicoPrestado",
            line=line,
            row_id=_row_id(row, "Id"),
            kind=ACTIVITY,
            role=_cell(row, "Servico"),
            entity=_cell(row, "Entidade"),
            period_raw=_cell(row, "Data"),
            screened=row,
        )


def parse_snapshot(
    download: Download, *, legislature: str, as_of: date, retrieved_at: datetime | None = None
) -> InterestsSnapshot:
    """Offline, complete projection of one legislature file; raw payload is not kept."""
    validate_legislature_code(legislature)
    payload: JSONValue = _json(download)
    if not isinstance(payload, list) or not payload or len(payload) > 20000:
        raise ParliamentImportError("Expected a bounded list of declarations.")
    collector = _Collector()
    for item in payload:
        element = _object(item, "RegistoInteresses")
        if set(element) - set(VERSIONS):
            raise ParliamentImportError("Unknown registo de interesses schema version.")
        present = [key for key in VERSIONS if element.get(key) is not None]
        if len(present) != 1:
            raise ParliamentImportError("Each declaration must hold exactly one schema version.")
        record = _object(element[present[0]], present[0])
        if present[0] in ("RegistoInteressesV1", "RegistoInteressesV2"):
            _version_1_2(collector, record, legislature, as_of)
        elif present[0] == "RegistoInteressesV3":
            _version_3(collector, record, legislature, as_of)
        else:
            _version_5(collector, record, legislature, as_of)
    return InterestsSnapshot(
        legislature=legislature,
        url=download.url,
        as_of=as_of,
        retrieved_at=retrieved_at or timezone.now(),
        declarants=collector.declarants(),
        excluded=tuple(sorted(collector.excluded.items())),
    )


def fetch_snapshot(*, legislature: str, as_of: date) -> InterestsSnapshot:
    validate_legislature_code(legislature)
    download = discover_download("interests", legislature)
    return parse_snapshot(download, legislature=legislature, as_of=as_of)


def _observation(
    row: InterestRow,
    *,
    identity: SourceIdentity,
    term: Term | None,
    snapshot: InterestsSnapshot,
) -> ObservationInput:
    dataset = DATASETS[DATASET]
    projection: JSONValue = {
        "external_id": row.key,
        "identity": identity.pk,
        "term": str(term.pk) if term else None,
        "url": snapshot.url,
        "title": dataset.title,
        "publisher": dataset.publisher,
        "kind": row.kind,
        "role": row.role,
        "object_name": row.object_name,
        "passage": row.passage,
        "reference": row.reference,
        "start": row.start.isoformat() if row.start else None,
        "start_precision": row.start_precision,
        "end": row.end.isoformat() if row.end else None,
        "end_precision": row.end_precision,
        "temporal_status": row.temporal_status,
        "declared_on": row.declared_on.isoformat() if row.declared_on else None,
    }
    return ObservationInput(
        external_id=row.key,
        revision=hashlib.sha256(canonical_json(projection).encode()).hexdigest(),
        category=SourceObservation.Category.DECLARED_INTEREST,
        passage=row.passage,
        source_url=snapshot.url,
        publisher=dataset.publisher,
        reference=row.reference,
        title=dataset.title,
        identity=identity,
        effective_start=row.start,
        effective_end=row.end,
        declared_on=row.declared_on,
        object_name=row.object_name,
        kind=row.kind,
        dataset=DATASET,
        role=row.role,
        term=term,
        start_precision=row.start_precision,
        end_precision=row.end_precision,
        temporal_status=row.temporal_status,
        retrieved_at=snapshot.retrieved_at,
    )


def apply_snapshot(snapshot: InterestsSnapshot) -> dict[str, int]:
    """Atomic declared interests, one scope per deputy; absent deputies cease."""
    prefix = f"interests:{snapshot.legislature}:"
    with import_transaction():
        term = Term.objects.filter(kind=Term.Kind.LEGISLATURE, code=snapshot.legislature).first()
        snapshots: dict[str, tuple[ObservationInput, ...]] = {}
        for declarant in snapshot.declarants:
            ar_person(declarant.cadastro_id, declarant.name)
            identity = SourceIdentity.objects.select_related("entity", "reviewed_by").get(
                source=IdentityScheme.PARLIAMENT, external_id=declarant.cadastro_id
            )
            snapshots[f"{prefix}{declarant.cadastro_id}"] = tuple(
                _observation(row, identity=identity, term=term, snapshot=snapshot)
                for row in declarant.rows
            )
        return sync_scoped_snapshot(
            source=EnrichmentSource.PARLIAMENT,
            prefix=prefix,
            snapshots=snapshots,
            as_of=snapshot.as_of,
        )
