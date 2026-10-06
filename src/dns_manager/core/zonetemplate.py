# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""새 zone 파일 생성 (New Zone 마법사).

Windows DNS Manager 는 zone 을 만들면 SOA 와 NS 를 자동으로 넣는다. 같은 결과를 만들되,
파일 기반이므로 **만들어질 파일 전문을 먼저 보여주고** 확인 후 기록한다.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from datetime import date

import dns.name
import dns.reversename

from . import serial as serial_mod

DEFAULT_SOA = {
    "refresh": 3600,
    "retry": 600,
    "expire": 604800,
    "minimum": 3600,
}


class TemplateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SoaValues:
    primary: str  # mname — 기본 네임서버 FQDN
    responsible: str  # rname — 관리자 메일 (admin.example.local. 형식)
    serial: int
    refresh: int = DEFAULT_SOA["refresh"]
    retry: int = DEFAULT_SOA["retry"]
    expire: int = DEFAULT_SOA["expire"]
    minimum: int = DEFAULT_SOA["minimum"]


def responsible_from_email(email: str) -> str:
    """admin@example.com → admin.example.com. (SOA rname 표기)"""
    email = email.strip()
    if "@" not in email:
        return email if email.endswith(".") else email + "."
    local, _, domain = email.partition("@")
    local = local.replace(".", "\\.")  # 로컬 파트의 점은 이스케이프해야 한다
    return f"{local}.{domain}."


def network_to_zone(network_id: str, family: int = 4) -> str:
    """정방향 순서의 network ID 를 역방향 zone 이름으로 바꾼다.

    Windows DNS Manager 의 Network ID 입력과 같은 방식:
        192.168.220      → 220.168.192.in-addr.arpa
        192.168.10.0/24  → 10.168.192.in-addr.arpa
    """
    value = network_id.strip().rstrip(".")
    if not value:
        raise TemplateError("Network ID 를 입력하세요.")

    prefix: int | None = None
    if "/" in value:
        value, _, bits = value.partition("/")
        try:
            prefix = int(bits)
        except ValueError as exc:
            raise TemplateError(f"CIDR 길이가 올바르지 않습니다: {bits}") from exc

    if family == 6:
        if prefix is None or prefix % 4 != 0:
            raise TemplateError("IPv6 역방향 zone 은 4의 배수인 CIDR 길이가 필요합니다 (예: 2001:db8::/32).")
        try:
            full = dns.reversename.from_address(value if "::" in value or ":" in value else value)
        except Exception as exc:  # noqa: BLE001 - 입력 형식 오류를 사용자 문구로 돌려준다
            raise TemplateError(f"IPv6 주소를 해석할 수 없습니다: {value} ({exc})") from exc
        labels = full.to_text().removesuffix(".ip6.arpa.").split(".")
        keep = prefix // 4
        return ".".join(labels[len(labels) - keep :]) + ".ip6.arpa"

    octets = [o for o in value.split(".") if o != ""]
    if prefix is not None:
        if prefix not in (8, 16, 24):
            raise TemplateError(
                f"/{prefix} 는 단순 역방향 zone 으로 표현할 수 없습니다. "
                f"/8, /16, /24 를 쓰거나 RFC 2317 방식으로 상위 zone 에 위임하세요."
            )
        octets = octets[: prefix // 8]
    if not 1 <= len(octets) <= 3:
        raise TemplateError(f"Network ID 가 올바르지 않습니다: {network_id}")
    for octet in octets:
        if not octet.isdigit() or not 0 <= int(octet) <= 255:
            raise TemplateError(f"Network ID 가 올바르지 않습니다: {network_id}")
    return ".".join(reversed(octets)) + ".in-addr.arpa"


def validate_zone_name(name: str) -> str:
    cleaned = name.strip().rstrip(".")
    if not cleaned:
        raise TemplateError("zone 이름을 입력하세요.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*", cleaned):
        raise TemplateError(f"zone 이름에 쓸 수 없는 문자가 있습니다: {name}")
    try:
        dns.name.from_text(cleaned)
    except Exception as exc:  # noqa: BLE001
        raise TemplateError(f"zone 이름을 해석할 수 없습니다: {name} ({exc})") from exc
    return cleaned


def is_inside(name: str, zone: str) -> bool:
    """name 이 zone 안쪽 이름인지."""
    n = name.strip().rstrip(".").lower()
    z = zone.strip().rstrip(".").lower()
    return n == z or n.endswith("." + z)


def default_primary_ns(zone: str) -> str:
    """기본 네임서버 이름.

    zone 안쪽 이름(ns1.<zone>)을 기본으로 쓰면 주소 레코드(glue)가 없어서
    named-checkzone 이 "NS has no address records" 로 **거부**한다.
    그래서 이 호스트의 FQDN 을 우선 쓴다(Windows 가 서버 자신의 이름을 쓰는 것과 같은 취지).
    """
    fqdn = socket.getfqdn()
    if "." in fqdn and not fqdn.endswith(".localdomain") and not is_inside(fqdn, zone):
        return fqdn.rstrip(".") + "."
    return f"ns1.{zone}."


def default_soa(zone: str, primary: str | None = None, email: str = "hostmaster") -> SoaValues:
    ns = primary or default_primary_ns(zone)
    if not ns.endswith("."):
        ns += "."
    responsible = responsible_from_email(email if "@" in email else f"{email}@{zone}")
    return SoaValues(primary=ns, responsible=responsible, serial=serial_mod.next_serial(None, date.today()))


def local_ipv4() -> str | None:
    """이 호스트의 바깥쪽 IPv4 주소(최선 추정).

    새 zone 의 기본 네임서버가 zone 안쪽 이름일 때 glue 로 쓴다. 추측값이지만
    마법사가 **만들어질 파일 전문을 먼저 보여주므로** 운영자가 보고 고칠 수 있다.
    """
    import socket as _socket

    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1 — 패킷을 실제로 보내지 않는다
            address = sock.getsockname()[0]
        return address if address and not address.startswith("127.") else None
    except OSError:
        return None


def require_glue(zone: str, name_servers: list[str], glue: dict[str, str]) -> None:
    """zone 안쪽 NS 에는 주소가 반드시 있어야 한다.

    없으면 named-checkzone 이 zone 을 아예 적재하지 않으므로, 만들기 전에 막고
    무엇을 넣어야 하는지 알려준다.
    """
    have = {k.rstrip(".").lower() for k in glue}
    have |= {f"{k.rstrip('.')}.{zone}".lower() for k in glue}
    for server in name_servers:
        target = server.rstrip(".").lower()
        if not is_inside(target, zone):
            continue
        if target in have:
            continue
        label = target[: -(len(zone) + 1)] if target != zone.lower() else "@"
        raise TemplateError(
            f"'{server}' 는 이 zone 안쪽 이름이므로 주소(glue) 레코드가 필요합니다. "
            f"네임서버 주소를 함께 입력하거나(예: {label} → 192.168.10.10), "
            f"zone 밖의 이름을 기본 네임서버로 쓰세요."
        )


def render_soa(soa: SoaValues) -> str:
    """SOA 레코드 블록. 새 zone 생성과 SOA 편집이 같은 서식을 쓰도록 한 곳에 둔다."""
    return (
        f"@       IN  SOA {soa.primary} {soa.responsible} (\n"
        f"                {soa.serial}  ; serial\n"
        f"                {soa.refresh}        ; refresh\n"
        f"                {soa.retry}         ; retry\n"
        f"                {soa.expire}      ; expire\n"
        f"                {soa.minimum} )      ; minimum\n"
    )


def render_zone_file(
    zone: str,
    soa: SoaValues,
    *,
    ttl: int = 3600,
    name_servers: list[str] | None = None,
    glue: dict[str, str] | None = None,
    comment: str | None = None,
) -> str:
    """새 zone 파일 전문을 만든다. 마법사의 미리보기와 실제 기록 내용이 같아야 한다."""
    servers = name_servers or [soa.primary]
    lines = [
        f"; {comment or f'{zone} — dns_manager 가 생성'}",
        f"$TTL {ttl}",
        *render_soa(soa).rstrip("\n").split("\n"),
    ]
    for server in servers:
        target = server if server.endswith(".") else f"{server}."
        lines.append(f"@           IN  NS      {target}")
    for name, address in (glue or {}).items():
        rtype = "AAAA" if ":" in address else "A"
        lines.append(f"{name:<12}IN  {rtype:<7} {address}")
    return "\n".join(lines) + "\n"
