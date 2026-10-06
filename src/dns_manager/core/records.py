# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""레코드 표현 모델.

rdata 의 문법 해석·검증은 dnspython 에 위임한다(직접 파싱 금지). 이 모듈은 그 결과를
Windows DNS Manager 식 목록(Name / Type / Data / TTL)과 타입별 Properties 폼에 맞게
표현하는 일만 한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import dns.name
import dns.rdata
import dns.rdataclass
import dns.rdatatype

# Windows DNS Manager 의 New Record 메뉴에 대응하는 1급 타입
PRIMARY_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "PTR", "SRV", "TXT")
# Other New Records… 목록에 넣는 타입
OTHER_TYPES = ("CAA", "SOA", "SSHFP", "TLSA", "NAPTR", "DNAME", "HINFO", "LOC", "SPF", "URI", "SVCB", "HTTPS")
# 기본 보기에서 숨기고 Advanced 보기에서만 노출하는 타입 (Windows 와 동일한 취지)
ADVANCED_ONLY_TYPES = frozenset({"SOA", "DNSKEY", "RRSIG", "NSEC", "NSEC3", "NSEC3PARAM", "CDS", "CDNSKEY"})
# zone 파일 편집으로 다루지 않는 서명 관련 타입
DNSSEC_TYPES = frozenset({"RRSIG", "NSEC", "NSEC3", "NSEC3PARAM", "DNSKEY", "CDS", "CDNSKEY"})

# 타입별 Data 열 분해 정의: 화면 레이블과 rdata 속성 이름
FIELD_SPECS: dict[str, tuple[tuple[str, str], ...]] = {
    "A": (("IP address", "address"),),
    "AAAA": (("IPv6 address", "address"),),
    "CNAME": (("Fully qualified domain name (FQDN)", "target"),),
    "DNAME": (("Target", "target"),),
    "NS": (("Name server", "target"),),
    "PTR": (("Host name", "target"),),
    "MX": (("Mail server priority", "preference"), ("Mail server", "exchange")),
    "SRV": (
        ("Priority", "priority"),
        ("Weight", "weight"),
        ("Port number", "port"),
        ("Host offering this service", "target"),
    ),
    "CAA": (("Flags", "flags"), ("Tag", "tag"), ("Value", "value")),
    "SOA": (
        ("Primary server", "mname"),
        ("Responsible person", "rname"),
        ("Serial number", "serial"),
        ("Refresh interval", "refresh"),
        ("Retry interval", "retry"),
        ("Expires after", "expire"),
        ("Minimum (default) TTL", "minimum"),
    ),
}

APEX_LABEL = "(same as parent folder)"


@dataclass(frozen=True)
class Record:
    """zone 안의 단일 rdata 한 건 (RRset 이 아니라 행 단위)."""

    name: str  # origin 기준 상대 이름. apex 는 "@"
    fqdn: str  # 절대 이름 (끝에 점)
    rtype: str
    rdclass: str
    ttl: int
    data: str  # rdata 의 정규화된 텍스트
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def is_apex(self) -> bool:
        return self.name == "@"

    @property
    def display_name(self) -> str:
        """Windows DNS Manager 표기 관례: apex 는 (same as parent folder)."""
        return APEX_LABEL if self.is_apex else self.name

    @property
    def advanced_only(self) -> bool:
        return self.rtype in ADVANCED_ONLY_TYPES

    @property
    def id(self) -> str:
        """레코드 식별자. 이름·타입·rdata 로 결정되는 안정적 해시.

        zone 파일에는 행 번호 같은 영속 식별자가 없으므로 내용 기반으로 식별한다.
        같은 내용의 중복 레코드는 zone 파일에서도 구분 불가하므로 동일 id 를 갖는다.
        """
        raw = f"{self.name}\x00{self.rdclass}\x00{self.rtype}\x00{self.data}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _rdata_fields(rtype: str, rdata: dns.rdata.Rdata) -> dict[str, str]:
    """타입별 Properties 폼용으로 rdata 를 필드로 분해한다."""
    out: dict[str, str] = {}
    for label, attr in FIELD_SPECS.get(rtype, ()):
        if not hasattr(rdata, attr):
            continue
        value = getattr(rdata, attr)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", "replace")
        out[label] = str(value)
    return out


def relative_name(name: dns.name.Name, origin: dns.name.Name) -> str:
    """absolute 이름을 origin 기준 상대 표기로 바꾼다. apex 는 '@'."""
    if name == origin:
        return "@"
    try:
        return name.relativize(origin).to_text()
    except Exception:  # noqa: BLE001 - origin 밖의 이름은 절대 표기로 남긴다
        return name.to_text()


def from_rdata(name: dns.name.Name, origin: dns.name.Name, ttl: int, rdata: dns.rdata.Rdata) -> Record:
    rtype = dns.rdatatype.to_text(rdata.rdtype)
    return Record(
        name=relative_name(name, origin),
        fqdn=name.to_text(),
        rtype=rtype,
        rdclass=dns.rdataclass.to_text(rdata.rdclass),
        ttl=int(ttl),
        data=rdata.to_text(origin=origin, relativize=False),
        fields=_rdata_fields(rtype, rdata),
    )


def sort_key(record: Record) -> tuple:
    """Windows DNS Manager 와 유사한 정렬: SOA·NS 먼저, 그다음 이름·타입 순."""
    priority = {"SOA": 0, "NS": 1}.get(record.rtype, 2)
    apex_first = 0 if record.is_apex else 1
    return (priority, apex_first, record.name.lower(), record.rtype, record.data.lower())
