"""Validate complete serving-MP snapshots and retain only an explicit field allowlist."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import cast

from .parliament_fetch import MAX_BYTES, Download, ParliamentImportError, validate_legislature

SERVING = frozenset({"Efetivo", "Efetivo Definitivo", "Efetivo Temporário"})
type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None
type JSONObject = dict[str, JSONValue]


@dataclass(frozen=True)
class MemberRecord:
    cadastro_id: str
    name: str
    start_date: date
    end_date: date | None
    data: JSONObject

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
    expected_count: int


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


def _active_interval(row: JSONObject, as_of: date) -> tuple[date, date | None] | None:
    end = _date(row.get("sioDtFim"), "sioDtFim")
    # Old reversed or missing-start intervals occur upstream. They cannot affect the
    # requested day when a valid end date is already past; do not repair or import them.
    if end is not None and end < as_of:
        return None
    start = _date(row.get("sioDtInicio"), "sioDtInicio")
    if start is None or (end is not None and end < start):
        raise ParliamentImportError("Ambiguous relevant mandate interval.")
    if start > as_of:
        return None
    return start, end


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
    biography: Download,
    *,
    legislature: str,
    as_of: date,
    expected_count: int = 230,
) -> ParliamentSnapshot:
    validate_legislature(legislature)
    if not 1 <= expected_count <= 1000:
        raise ParliamentImportError("Expected serving count must be between 1 and 1000.")
    root = _object(_json(roster), "roster")
    detail = _object(root.get("DetalheLegislatura"), "DetalheLegislatura")
    if detail.get("sigla") != legislature:
        raise ParliamentImportError("Roster legislature does not match the requested legislature.")
    start, end = _date(detail.get("dtini"), "dtini"), _date(detail.get("dtfim"), "dtfim")
    if start is None or as_of < start or (end is not None and as_of > end):
        raise ParliamentImportError("Requested day is outside the legislature.")
    selected: list[tuple[JSONObject, date, date | None]] = []
    ids: set[str] = set()
    for row in _rows(root.get("Deputados"), "Deputados"):
        cadastro = _identifier(row.get("DepCadId"), "DepCadId")
        if cadastro in ids:
            raise ParliamentImportError("Duplicate roster cadastro identifier.")
        ids.add(cadastro)
        active: list[tuple[JSONObject, date, date | None]] = []
        for status in _rows(row.get("DepSituacao"), "DepSituacao"):
            interval = _active_interval(status, as_of)
            if interval:
                active.append((status, *interval))
        serving = [item for item in active if _text(item[0].get("sioDes"), "sioDes") in SERVING]
        if not serving:
            continue
        if len(active) != 1:
            raise ParliamentImportError("Overlapping current status intervals.")
        status, mandate_start, mandate_end = serving[0]
        if mandate_start < start or (
            end is not None and mandate_end is not None and mandate_end > end
        ):
            raise ParliamentImportError("Mandate dates are outside the legislature.")
        selected.append(({**row, "DepSituacao": status}, mandate_start, mandate_end))
    if len(selected) != expected_count:
        raise ParliamentImportError(
            f"Serving count {len(selected)} does not match expected count {expected_count}."
        )
    selected_ids = {_identifier(row.get("DepCadId"), "DepCadId") for row, _, _ in selected}
    biographies: dict[str, JSONObject] = {}
    for row in _rows(_json(biography), "biographies"):
        cadastro = _identifier(row.get("CadId"), "CadId")
        if cadastro not in selected_ids:
            continue
        if cadastro in biographies:
            raise ParliamentImportError("Ambiguous biography cadastro identifier.")
        biographies[cadastro] = row
    if set(biographies) != selected_ids:
        raise ParliamentImportError("A serving MP has no biography matched by cadastro identifier.")
    members: list[MemberRecord] = []
    for row, mandate_start, mandate_end in selected:
        cadastro = _identifier(row.get("DepCadId"), "DepCadId")
        if row.get("LegDes") != legislature:
            raise ParliamentImportError("MP legislature does not match the roster.")
        groups: list[JSONValue] = []
        for group in _rows(row.get("DepGP"), "DepGP", optional=True):
            groups.append(
                {
                    key: _text(group.get(key), key, optional=True)
                    for key in ("gpSigla", "gpDtInicio", "gpDtFim")
                }
            )
        status = _object(row.get("DepSituacao"), "DepSituacao")
        name = _text(row.get("DepNomeCompleto"), "DepNomeCompleto", limit=240)
        data: JSONObject = {
            "legislature": legislature,
            "roster": {
                "DepCadId": cadastro,
                "DepId": _identifier(row.get("DepId"), "DepId"),
                "DepNomeCompleto": name,
                "DepNomeParlamentar": _text(
                    row.get("DepNomeParlamentar"), "DepNomeParlamentar", limit=240
                ),
                "DepCPDes": _text(row.get("DepCPDes"), "DepCPDes", limit=240),
                "DepGP": groups,
                "DepSituacao": {
                    "sioDes": status["sioDes"],
                    "sioDtInicio": mandate_start.isoformat(),
                    "sioDtFim": mandate_end.isoformat() if mandate_end else None,
                },
            },
            "biography": _biography(biographies[cadastro]),
        }
        members.append(MemberRecord(cadastro, name, mandate_start, mandate_end, data))
    return ParliamentSnapshot(
        legislature,
        as_of,
        tuple(sorted(members, key=lambda m: int(m.cadastro_id))),
        roster.url,
        biography.url,
        expected_count,
    )
