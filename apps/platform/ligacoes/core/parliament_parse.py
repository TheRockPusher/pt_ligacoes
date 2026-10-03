"""Project complete legislature histories onto a minimised public-office allowlist."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import cast

from .parliament_fetch import (
    MAX_BYTES,
    Download,
    ParliamentImportError,
    canonical_legislature,
    validate_legislature,
)

SERVING = frozenset({"Efetivo", "Efetivo Definitivo", "Efetivo Temporário"})
type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None
type JSONObject = dict[str, JSONValue]


@dataclass(frozen=True)
class MemberRecord:
    cadastro_id: str
    name: str
    start_date: date | None
    end_date: date | None
    data: JSONObject
    periods: tuple[tuple[str, date, date | None], ...] = ()

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.data).encode()).hexdigest()


@dataclass(frozen=True)
class ParliamentSnapshot:
    legislature: str
    as_of: date
    members: tuple[MemberRecord, ...]
    roster_url: str
    biography_url: str
    # DetalheLegislatura dates; parse_snapshot always sets the start.
    legislature_start: date | None = None
    legislature_end: date | None = None

    @property
    def mandate_count(self) -> int:
        return sum(len(member.periods) for member in self.members)


def canonical_json(value: JSONValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: JSONValue, field: str) -> JSONObject:
    if not isinstance(value, dict):
        raise ParliamentImportError(f"Expected an object for {field}.")
    return value


def _rows(value: JSONValue, field: str, *, optional: bool = False) -> list[JSONObject]:
    if value is None and optional:
        return []
    if not isinstance(value, list) or len(value) > 10000:
        raise ParliamentImportError(f"Expected a bounded list for {field}.")
    return [_object(item, field) for item in value]


def _text(value: JSONValue, field: str, *, optional: bool = False, limit: int = 10000) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ParliamentImportError(f"Invalid text in {field}.")
    return value


def _identifier(value: JSONValue, field: str) -> str:
    # The official exporter serialises numeric identifiers as e.g. 1234.0.
    if isinstance(value, float) and value.is_integer() and 0 < value <= 2**53 - 1:
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ParliamentImportError(f"Invalid stable identifier in {field}.")
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", result):
        raise ParliamentImportError(f"Invalid stable identifier in {field}.")
    return result


def _date(value: JSONValue, field: str) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ParliamentImportError(f"Invalid date in {field}.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ParliamentImportError(f"Invalid date in {field}.") from exc


def _pairs(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
    result: JSONObject = {}
    for key, value in pairs:
        if key in result:
            raise ParliamentImportError("Duplicate JSON object key.")
        result[key] = value
    return result


def _json(download: Download) -> JSONValue:
    if len(download.content) > MAX_BYTES:
        raise ParliamentImportError("JSON payload exceeds the size limit.")
    try:
        return cast(
            JSONValue, json.loads(download.content.decode("utf-8-sig"), object_pairs_hook=_pairs)
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ParliamentImportError("Invalid official JSON payload.") from exc


def _biography(row: JSONObject) -> JSONObject:
    qualifications: list[JSONValue] = []
    for qualification in _rows(row.get("CadHabilitacoes"), "CadHabilitacoes", optional=True):
        qualifications.append(
            {
                "HabDes": _text(qualification.get("HabDes"), "HabDes"),
                "HabEstado": _text(qualification.get("HabEstado"), "HabEstado", optional=True),
            }
        )
    roles: list[JSONValue] = []
    for role in _rows(row.get("CadCargosFuncoes"), "CadCargosFuncoes", optional=True):
        former = role.get("FunAntiga")
        if former not in ("S", "N", None):
            raise ParliamentImportError("Unknown prior/current biography role marker.")
        roles.append(
            {
                "FunId": _identifier(role.get("FunId"), "FunId"),
                "FunAntiga": former,
                "FunDes": _text(role.get("FunDes"), "FunDes"),
            }
        )
    return {
        "CadProfissao": _text(row.get("CadProfissao"), "CadProfissao", optional=True),
        "CadHabilitacoes": qualifications,
        "CadCargosFuncoes": roles,
    }


def parse_snapshot(
    roster: Download,
    biography: Download | None = None,
    *,
    legislature: str,
    as_of: date,
) -> ParliamentSnapshot:
    # Imported lazily: bodies shares the JSON allowlist helpers in this module.
    from .parliament_bodies import _serving

    legislature = canonical_legislature(legislature)
    validate_legislature(legislature)
    root = _object(_json(roster), "roster")
    detail = _object(root.get("DetalheLegislatura"), "DetalheLegislatura")
    code = detail.get("siglaAntiga") if legislature in {"IA", "IB"} else detail.get("sigla")
    if code != legislature:
        raise ParliamentImportError("Roster legislature does not match the requested legislature.")
    start, end = _date(detail.get("dtini"), "dtini"), _date(detail.get("dtfim"), "dtfim")
    if start is None:
        raise ParliamentImportError("The legislature has no published start.")
    if legislature == "IA":
        start, end = date(1976, 6, 3), date(1980, 1, 2)
    elif legislature == "IB":
        start, end = date(1980, 1, 3), date(1980, 11, 12)
    people: dict[str, JSONObject] = {}
    for row in _rows(root.get("Deputados"), "Deputados"):
        cadastro = _identifier(row.get("DepCadId"), "DepCadId")
        name = _text(row.get("DepNomeCompleto"), "DepNomeCompleto", limit=240)
        alias = _text(row.get("DepNomeParlamentar"), "DepNomeParlamentar", optional=True, limit=240)
        dep_id = _identifier(row.get("DepId"), "DepId")
        projected: JSONObject = {
            "DepCadId": cadastro,
            "DepIds": [dep_id],
            "DepNomeCompleto": name,
            "DepNomeParlamentar": alias,
            "DepCPDes": _text(row.get("DepCPDes"), "DepCPDes", optional=True, limit=240),
            "DepGP": [],
            "DepSituacao": [],
        }
        for status in _rows(row.get("DepSituacao"), "DepSituacao", optional=True):
            status_start = _date(status.get("sioDtInicio"), "sioDtInicio")
            status_end = _date(status.get("sioDtFim"), "sioDtFim")
            cast(list[JSONValue], projected["DepSituacao"]).append(
                {
                    "sioDes": _text(status.get("sioDes"), "sioDes", limit=80),
                    "sioDtInicio": status_start.isoformat() if status_start else None,
                    "sioDtFim": status_end.isoformat() if status_end else None,
                    "DepId": dep_id,
                }
            )
        for group in _rows(row.get("DepGP"), "DepGP", optional=True):
            cast(list[JSONValue], projected["DepGP"]).append(
                {
                    "gpId": str(group.get("gpId") or "0"),
                    **{
                        key: _text(group.get(key), key, optional=True)
                        for key in ("gpSigla", "gpDtInicio", "gpDtFim")
                    },
                }
            )
        previous = people.get(cadastro)
        if previous is None:
            people[cadastro] = projected
        else:
            if previous["DepNomeCompleto"] != name:
                raise ParliamentImportError("Conflicting names for one stable cadastro identifier.")
            for key in ("DepIds", "DepGP", "DepSituacao"):
                target = cast(list[JSONValue], previous[key])
                for item in cast(list[JSONValue], projected[key]):
                    if item not in target:
                        target.append(item)
    biographies: dict[str, JSONObject] = {}
    if biography is not None:
        for row in _rows(_json(biography), "biographies"):
            cadastro = _identifier(row.get("CadId"), "CadId")
            if cadastro not in people:
                continue
            projected_bio = _biography(row)
            if cadastro in biographies and biographies[cadastro] != projected_bio:
                raise ParliamentImportError("Ambiguous biography cadastro identifier.")
            biographies[cadastro] = projected_bio
    members: list[MemberRecord] = []
    for cadastro, row in sorted(people.items(), key=lambda item: int(item[0])):
        periods = _serving(row)
        if legislature in {"IA", "IB"}:
            if end is None:
                raise ParliamentImportError(
                    "A first-legislature subperiod requires its published end."
                )
            periods = [
                (status, max(first, start), min(last or end, end))
                for status, first, last in periods
                if first <= end and (last is None or last >= start)
            ]
        data: JSONObject = {
            "legislature": legislature,
            "roster": row,
            "biography": biographies.get(cadastro, {}),
            "date_conflicts": [first.isoformat() for _, first, _ in periods if first < start],
        }
        members.append(
            MemberRecord(
                cadastro,
                cast(str, row["DepNomeCompleto"]),
                periods[0][1] if periods else None,
                periods[-1][2] if periods else None,
                data,
                tuple(periods),
            )
        )
    return ParliamentSnapshot(
        legislature,
        as_of,
        tuple(members),
        roster.url,
        biography.url if biography else "",
        start,
        end,
    )
