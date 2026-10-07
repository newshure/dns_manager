# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""실행 중인 named 상태 조회 (rndc / dig).

zone 파일의 serial 과 서버에 적재된 serial 이 다르면 "미적용 변경"이 있다는 뜻이다.
Windows DNS Manager 에는 없는 개념이지만 파일 기반 BIND 에서는 핵심 정보다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

from ..config import BindConfig
from .commands import CommandError, CommandResult, run

_SERIAL_RE = re.compile(r"^\s*serial:\s*(\d+)", re.MULTILINE)
_TYPE_RE = re.compile(r"^\s*type:\s*(\S+)", re.MULTILINE)
# rndc 는 "last loaded:" 로 내보낸다. 'loaded:' 만 찾으면 영영 못 맞춘다(조용히 None 이 된다).
_LOADED_RE = re.compile(r"^\s*(?:last\s+)?loaded:\s*(.+)$", re.MULTILINE)
# named 가 이 zone 을 동적으로 다루는지. named.conf 파싱보다 이쪽이 사실이다 —
# allow-update 가 options 에 전역으로 걸려 있으면 zone 블록만 봐서는 알 수 없다.
_DYNAMIC_RE = re.compile(r"^\s*dynamic:\s*(yes|no)\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class ServerStatus:
    running: bool
    version: str | None
    raw: str


@dataclass(frozen=True)
class ZoneStatus:
    name: str
    available: bool
    serial: int | None = None
    zone_type: str | None = None
    loaded: str | None = None
    # named 가 보는 동적 여부. 모르면 None (status 를 못 읽은 경우).
    dynamic: bool | None = None
    raw: str = ""

    @property
    def loaded_at(self) -> datetime | None:
        """zone 을 마지막으로 읽어들인 시각.

        파일을 고치면서 serial 을 올리지 않으면 serial 비교로는 변화를 알 수 없다.
        그때도 "파일이 적재 시각보다 새것인가" 로는 잡힌다.
        """
        if not self.loaded:
            return None
        try:
            return parsedate_to_datetime(self.loaded)
        except (TypeError, ValueError):
            return None


def _parse_dynamic(text: str) -> bool | None:
    """zonestatus 출력에서 'dynamic: yes|no' 를 읽는다. 없으면 None."""
    match = _DYNAMIC_RE.search(text)
    return match.group(1).lower() == "yes" if match else None


def _rndc_argv(cfg: BindConfig) -> list[str]:
    argv = [cfg.rndc]
    if cfg.rndc_key:
        argv += ["-k", str(cfg.rndc_key)]
    return argv


def status(cfg: BindConfig) -> ServerStatus:
    try:
        result = run([*_rndc_argv(cfg), "status"], timeout=cfg.command_timeout)
    except CommandError as exc:
        return ServerStatus(running=False, version=None, raw=str(exc))
    version = None
    for line in result.stdout.splitlines():
        if line.startswith("version:"):
            version = line.split(":", 1)[1].strip()
            break
    return ServerStatus(running=result.ok, version=version, raw=result.message or result.stdout)


def zone_status(cfg: BindConfig, zone: str, view: str | None = None) -> ZoneStatus:
    argv = [*_rndc_argv(cfg), "zonestatus", zone]
    if view:
        argv += ["IN", view]
    try:
        result = run(argv, timeout=cfg.command_timeout)
    except CommandError as exc:
        return ZoneStatus(name=zone, available=False, raw=str(exc))
    if not result.ok:
        return ZoneStatus(name=zone, available=False, raw=result.message)

    serial_match = _SERIAL_RE.search(result.stdout)
    type_match = _TYPE_RE.search(result.stdout)
    loaded_match = _LOADED_RE.search(result.stdout)
    return ZoneStatus(
        name=zone,
        available=True,
        serial=int(serial_match.group(1)) if serial_match else None,
        zone_type=type_match.group(1) if type_match else None,
        loaded=loaded_match.group(1).strip() if loaded_match else None,
        dynamic=_parse_dynamic(result.stdout),
        raw=result.stdout,
    )


def query(cfg: BindConfig, name: str, rtype: str = "A", server: str = "127.0.0.1") -> CommandResult:
    """변경 후 실제 응답 확인용. Windows DNS Manager 의 'Launch nslookup' 대응."""
    return run(
        [cfg.dig, f"@{server}", name, rtype, "+norecurse", "+nocmd", "+noall", "+answer", "+comments"],
        timeout=cfg.command_timeout,
    )
