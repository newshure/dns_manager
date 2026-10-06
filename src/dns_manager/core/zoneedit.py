# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""레코드 단위 편집 — zone 파일 텍스트를 외과적으로 고친다.

왜 텍스트를 직접 다루는가: dnspython 으로 파싱 후 재직렬화하면 주석·정렬·빈 줄·지시자
배치가 전부 사라진다. 운영자가 손으로 관리해 온 zone 파일에서 이는 받아들이기 어렵다.
그래서 "각 레코드가 원문의 몇 번째 줄에 있는지" 색인을 만들고, 바꿀 레코드의 줄만 교체한다.

rdata 의 해석·정규화는 여전히 dnspython 이 한다. 이 모듈이 하는 일은 줄 경계를 찾는 것뿐이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import dns.exception
import dns.name
import dns.rdata
import dns.rdataclass
import dns.rdatatype

from . import records as records_mod
from .records import Record

CLASSES = frozenset({"IN", "CH", "HS", "CS"})
_TTL_TOKEN = re.compile(r"^\d+[smhdwSMHDW]?$")
DEFAULT_TTL = 3600


class ZoneEditError(RuntimeError):
    """편집할 수 없는 zone 이거나, 대상 레코드를 찾을 수 없다."""


class RecordNotFound(ZoneEditError):
    pass


@dataclass(frozen=True)
class RecordLocation:
    """원문에서 레코드 한 건이 차지하는 줄 범위."""

    record: Record
    start: int  # 0-based, 포함
    end: int  # 0-based, 배타적
    raw: str  # 원문 그대로의 줄들

    @property
    def id(self) -> str:
        return self.record.id


@dataclass
class _ScanState:
    origin: dns.name.Name
    ttl: int
    last_owner: dns.name.Name | None = None
    locations: list[RecordLocation] = field(default_factory=list)


def strip_comment(line: str) -> str:
    """따옴표 밖의 `;` 부터를 잘라낸다. TXT 안의 세미콜론을 주석으로 오인하면 안 된다."""
    out = []
    in_quotes = False
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ";" and not in_quotes:
            break
        out.append(ch)
    return "".join(out)


def _tokenize(text: str) -> list[str]:
    """따옴표를 하나의 토큰으로 유지하며 공백 분리. 괄호는 버린다(여러 줄 RR 결합용)."""
    tokens: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if not in_quotes and ch in "()":
            continue
        if not in_quotes and ch in " \t\r\n":
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _paren_balance(text: str) -> int:
    depth = 0
    in_quotes = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
    return depth


def _parse_ttl(token: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if token[-1].lower() in units:
        return int(token[:-1]) * units[token[-1].lower()]
    return int(token)


def _owner_name(token: str, origin: dns.name.Name, last: dns.name.Name | None) -> dns.name.Name:
    if token == "@":
        return origin
    return dns.name.from_text(token, origin=origin)


def index_records(text: str, origin_name: str) -> list[RecordLocation]:
    """zone 파일 텍스트에서 레코드별 줄 범위를 찾는다.

    해석할 수 없는 줄은 조용히 건너뛴다 — 색인의 목적은 "편집할 줄 찾기"이며,
    유효성 판정은 named-checkzone 의 몫이다.
    """
    origin = dns.name.from_text(origin_name)
    state = _ScanState(origin=origin, ttl=DEFAULT_TTL)

    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        raw_start = i
        stripped = strip_comment(lines[i]).strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("$"):
            parts = stripped.split()
            directive = parts[0].upper()
            if directive == "$TTL" and len(parts) > 1:
                try:
                    state.ttl = _parse_ttl(parts[1])
                except ValueError:
                    pass
            elif directive == "$ORIGIN" and len(parts) > 1:
                try:
                    state.origin = dns.name.from_text(parts[1], origin=state.origin)
                except dns.exception.DNSException:
                    pass
            i += 1
            continue

        # 여러 줄에 걸친 RR(괄호) 결합
        buffer = strip_comment(lines[i])
        depth = _paren_balance(buffer)
        while depth > 0 and i + 1 < len(lines):
            i += 1
            buffer += strip_comment(lines[i])
            depth = _paren_balance(buffer)
        raw_end = i + 1

        owner_explicit = bool(lines[raw_start][:1].strip())
        location = _build_location(buffer, state, owner_explicit, lines, raw_start, raw_end)
        if location is not None:
            state.locations.append(location)
        i += 1

    return state.locations


def _build_location(
    buffer: str,
    state: _ScanState,
    owner_explicit: bool,
    lines: list[str],
    start: int,
    end: int,
) -> RecordLocation | None:
    tokens = _tokenize(buffer)
    if not tokens:
        return None

    if owner_explicit:
        owner_token = tokens.pop(0)
        try:
            owner = _owner_name(owner_token, state.origin, state.last_owner)
        except dns.exception.DNSException:
            return None
        state.last_owner = owner
    else:
        if state.last_owner is None:
            return None
        owner = state.last_owner

    ttl = state.ttl
    rdclass = "IN"
    while tokens:
        token = tokens[0]
        if _TTL_TOKEN.match(token):
            try:
                ttl = _parse_ttl(tokens.pop(0))
                continue
            except ValueError:
                break
        if token.upper() in CLASSES:
            rdclass = tokens.pop(0).upper()
            continue
        break

    if not tokens:
        return None
    rtype = tokens.pop(0).upper()
    try:
        rdtype = dns.rdatatype.from_text(rtype)
        rdata = dns.rdata.from_text(
            dns.rdataclass.from_text(rdclass), rdtype, " ".join(tokens), origin=state.origin, relativize=False
        )
    except (dns.exception.DNSException, ValueError):
        return None

    record = records_mod.from_rdata(owner, state.origin, ttl, rdata)
    return RecordLocation(record=record, start=start, end=end, raw="".join(lines[start:end]))


def find(locations: list[RecordLocation], record_id: str) -> RecordLocation:
    for location in locations:
        if location.id == record_id:
            return location
    raise RecordNotFound(f"레코드를 찾을 수 없습니다: {record_id}")


def format_record(name: str, ttl: int | None, rtype: str, rdata: str, rdclass: str = "IN") -> str:
    """새 레코드 한 줄을 만든다. 기존 파일의 정렬 관례에 맞춰 열을 맞춘다."""
    ttl_text = str(ttl) if ttl is not None else ""
    return f"{name:<12}{ttl_text:>6} {rdclass:<3} {rtype:<7} {rdata}".rstrip() + "\n"


def normalize(name: str, rtype: str, rdata: str, origin_name: str, rdclass: str = "IN") -> Record:
    """입력값을 dnspython 으로 정규화해 Record 로 만든다(검증 겸용).

    여기서 통과해도 최종 판정은 named-checkzone 이 한다.
    """
    origin = dns.name.from_text(origin_name)
    try:
        owner = origin if name in ("@", "") else dns.name.from_text(name, origin=origin)
    except dns.exception.DNSException as exc:
        raise ZoneEditError(f"이름이 올바르지 않습니다: {name} ({exc})") from exc
    try:
        rd = dns.rdata.from_text(
            dns.rdataclass.from_text(rdclass),
            dns.rdatatype.from_text(rtype.upper()),
            rdata,
            origin=origin,
            relativize=False,
        )
    except (dns.exception.DNSException, ValueError) as exc:
        raise ZoneEditError(f"{rtype.upper()} 레코드 값이 올바르지 않습니다: {rdata} ({exc})") from exc
    return records_mod.from_rdata(owner, origin, 0, rd)


def delete_records(text: str, origin_name: str, record_ids: list[str]) -> str:
    """지정한 레코드들의 줄을 지운다. 다른 줄은 손대지 않는다."""
    locations = index_records(text, origin_name)
    targets = [find(locations, rid) for rid in record_ids]

    lines = text.splitlines(keepends=True)
    drop: set[int] = set()
    for target in targets:
        if target.record.rtype == "SOA":
            raise ZoneEditError("SOA 레코드는 삭제할 수 없습니다. Properties 에서 편집하세요.")
        drop.update(range(target.start, target.end))

        # 지우는 줄이 뒤따르는 레코드의 소유자 이름을 제공하고 있었다면,
        # 다음 레코드에 이름을 명시해 줘야 의미가 바뀌지 않는다.
        follower = next((loc for loc in locations if loc.start == target.end), None)
        if follower is not None and not lines[follower.start][:1].strip():
            owner = target.record.name
            lines[follower.start] = f"{owner:<12}" + lines[follower.start].lstrip()

    return "".join(line for idx, line in enumerate(lines) if idx not in drop)


def replace_record(text: str, origin_name: str, record_id: str, new_line: str) -> str:
    """레코드 한 건의 줄을 새 줄로 바꾼다."""
    locations = index_records(text, origin_name)
    target = find(locations, record_id)
    lines = text.splitlines(keepends=True)
    return "".join(lines[: target.start]) + new_line + "".join(lines[target.end :])


def append_record(text: str, new_line: str) -> str:
    """파일 끝에 레코드를 덧붙인다."""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + new_line
