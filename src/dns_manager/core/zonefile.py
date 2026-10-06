# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""zone 파일 읽기.

파싱·직렬화는 dnspython 에 전적으로 위임한다. 앱은 파일 내용과 함께 mtime·sha256 을
들고 다니며, 저장 시점에 외부 변경이 있었는지 낙관적으로 검사한다(수동 편집과의 충돌 방지).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import dns.exception
import dns.name
import dns.rdatatype
import dns.zone

from . import fileaudit
from . import records as records_mod
from .records import Record

# 테이블 편집을 막아야 하는 지시자. $INCLUDE 는 dnspython 이 전개해버려서
# 재직렬화하면 원본 구조가 깨지고, $GENERATE 는 dnspython 이 다루지 못한다.
UNSUPPORTED_DIRECTIVES = ("$INCLUDE", "$GENERATE")


class ZoneReadError(RuntimeError):
    """zone 파일을 읽거나 해석할 수 없음."""


@dataclass(frozen=True)
class ZoneSnapshot:
    """특정 시점의 zone 파일 상태."""

    origin: str
    path: Path
    text: str
    sha256: str
    mtime_ns: int

    @property
    def version(self) -> str:
        """낙관적 동시성 제어용 버전 토큰."""
        return f"{self.mtime_ns}-{self.sha256[:16]}"


@dataclass(frozen=True)
class ZoneContent:
    """파싱까지 끝난 zone."""

    snapshot: ZoneSnapshot
    records: tuple[Record, ...]
    soa: Record | None
    unsupported: tuple[str, ...] = ()
    parse_error: str | None = None

    @property
    def parsable(self) -> bool:
        return self.parse_error is None

    @property
    def table_editable(self) -> bool:
        """테이블/구조화 편집이 가능한지. 미지원 지시자가 있으면 raw 전용."""
        return self.parsable and not self.unsupported

    @property
    def serial(self) -> int | None:
        if self.soa is None:
            return None
        try:
            return int(self.soa.fields.get("Serial number", ""))
        except ValueError:
            return None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_snapshot(path: Path, origin: str) -> ZoneSnapshot:
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        fileaudit.record(path, "read", "zone-file", detail=origin, ok=False)
        raise ZoneReadError(f"zone 파일을 읽을 수 없습니다: {path} ({exc.strerror})") from exc
    fileaudit.record(path, "read", "zone-file" if origin != "conf" else "zones-conf", detail=origin)
    return ZoneSnapshot(
        origin=origin,
        path=path,
        text=text,
        sha256=_digest(text),
        mtime_ns=stat.st_mtime_ns,
    )


def find_unsupported(text: str) -> tuple[str, ...]:
    upper = text.upper()
    return tuple(d for d in UNSUPPORTED_DIRECTIVES if d in upper)


def parse(snapshot: ZoneSnapshot) -> ZoneContent:
    """스냅샷 텍스트를 레코드 목록으로 해석한다. 실패해도 예외를 던지지 않고 담아 둔다.

    (파싱 불가한 zone 도 raw 에디터로는 열 수 있어야 하기 때문)
    """
    unsupported = find_unsupported(snapshot.text)
    origin = dns.name.from_text(snapshot.origin)
    try:
        zone = dns.zone.from_text(
            snapshot.text,
            origin=origin,
            relativize=False,
            check_origin=False,
            allow_include=False,
        )
    except (dns.exception.DNSException, ValueError) as exc:
        return ZoneContent(
            snapshot=snapshot,
            records=(),
            soa=None,
            unsupported=unsupported,
            parse_error=f"{type(exc).__name__}: {exc}",
        )

    rows: list[Record] = []
    soa: Record | None = None
    for name, ttl, rdata in zone.iterate_rdatas():
        record = records_mod.from_rdata(name, origin, ttl, rdata)
        if record.rtype == "SOA" and record.is_apex and soa is None:
            soa = record
        rows.append(record)

    rows.sort(key=records_mod.sort_key)
    return ZoneContent(
        snapshot=snapshot,
        records=tuple(rows),
        soa=soa,
        unsupported=unsupported,
    )


def load(path: Path, origin: str) -> ZoneContent:
    return parse(read_snapshot(path, origin))
