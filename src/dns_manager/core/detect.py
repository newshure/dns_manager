# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""BIND 설정 레이아웃 자동 감지.

배포판마다 named 패키지의 파일 배치가 다르다. 경로를 코드에 박으면 다른 배포판에서
즉시 깨지므로, **파일이 실제로 있는지**를 보고 판단한다(배포판 이름이 아니라 파일 위치 기준).

    RedHat 계열 (Rocky, RHEL, CentOS, Fedora — bind 패키지)
        named.conf   /etc/named.conf          (options 블록이 이 파일 안에 있다)
        zone 디렉터리 /var/named
        zone 정의     /etc/named/zones.conf    (이 도구가 만들어 쓰는 include)
        rndc 키       /etc/rndc.key

    Debian 계열 (Debian, Ubuntu — bind9 패키지)
        named.conf   /etc/bind/named.conf
        options      /etc/bind/named.conf.options   ← named.conf 가 아니다
        zone 정의     /etc/bind/named.conf.local     (배포판이 이미 제공하는 include)
        zone 디렉터리 /var/cache/bind
        rndc 키       /etc/bind/rndc.key

options 블록의 위치는 추측하지 않고 named.conf 와 그 include 들을 따라가며 **실제로 찾는다**.
전역 전달자를 고치려면 그 파일을 고쳐야 하기 때문이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import BindConfig

_INCLUDE = re.compile(r'^\s*include\s+"([^"]+)"\s*;', re.MULTILINE)
_OPTIONS = re.compile(r"^\s*options\s*\{", re.MULTILINE)


@dataclass(frozen=True)
class Profile:
    """감지된 레이아웃."""

    family: str  # redhat | debian | unknown
    named_conf: Path
    zones_conf: Path
    zone_dir: Path
    rndc_key: Path | None = None
    service: str = "named"
    # zones_conf 가 named.conf 에서 include 되어 있는지 (아니면 사용자가 추가해야 한다)
    zones_conf_included: bool = False
    zones_conf_exists: bool = False


# 후보 레이아웃. 위에서부터 named.conf 가 있는 첫 항목을 고른다.
CANDIDATES: tuple[dict[str, object], ...] = (
    {
        "family": "redhat",
        "named_conf": Path("/etc/named.conf"),
        "zones_conf": Path("/etc/named/zones.conf"),
        "zone_dir": Path("/var/named"),
        "rndc_key": Path("/etc/rndc.key"),
        "service": "named",
    },
    {
        "family": "debian",
        "named_conf": Path("/etc/bind/named.conf"),
        # 배포판이 "사용자 zone 은 여기에" 라고 제공하는 파일을 그대로 쓴다.
        "zones_conf": Path("/etc/bind/named.conf.local"),
        "zone_dir": Path("/var/cache/bind"),
        "rndc_key": Path("/etc/bind/rndc.key"),
        "service": "named",  # bind9.service 의 별칭
    },
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_file(path: Path) -> bool:
    """존재 확인. 권한이 없어 들여다볼 수 없는 경로도 '아니오' 로 다룬다.

    감지는 넘겨짚는 작업이다. 여기서 예외가 새어 나가면 앱이 아예 기동하지 못한다
    (실제로 일반 사용자로 띄웠을 때 /etc/named 접근 불가로 죽었다).
    """
    try:
        return path.is_file()
    except OSError:
        return False


def includes_of(named_conf: Path, *, root: Path | None = None) -> list[Path]:
    """named.conf 가 직접 include 하는 파일 목록(존재하는 것만, 1단계 재귀 포함)."""
    seen: set[Path] = set()
    found: list[Path] = []

    def walk(path: Path, depth: int) -> None:
        if depth > 4 or path in seen:
            return
        seen.add(path)
        for match in _INCLUDE.finditer(_read(path)):
            target = Path(match.group(1))
            if root is not None and target.is_absolute():
                target = root / target.relative_to("/")
            if not target.is_absolute():
                target = path.parent / target
            if _is_file(target):
                found.append(target)
                walk(target, depth + 1)

    walk(named_conf, 0)
    return found


def find_options_file(named_conf: Path, *, root: Path | None = None) -> Path | None:
    """options 블록이 실제로 들어 있는 파일을 찾는다.

    Rocky 는 named.conf 자신, Debian 은 named.conf.options 다. 전역 전달자를 고치려면
    이 파일을 고쳐야 한다.
    """
    if _OPTIONS.search(_read(named_conf)):
        return named_conf
    for candidate in includes_of(named_conf, root=root):
        if _OPTIONS.search(_read(candidate)):
            return candidate
    return None


def detect(root: Path | None = None) -> Profile | None:
    """설치된 BIND 레이아웃을 감지한다. root 를 주면 그 아래에서 찾는다(시험용)."""
    base = root or Path("/")
    for spec in CANDIDATES:
        named_conf = base / Path(spec["named_conf"]).relative_to("/")  # type: ignore[arg-type]
        if not _is_file(named_conf):
            continue

        zones_conf = base / Path(spec["zones_conf"]).relative_to("/")  # type: ignore[arg-type]
        included = any(p == zones_conf for p in includes_of(named_conf, root=root))
        rndc_key = base / Path(spec["rndc_key"]).relative_to("/")  # type: ignore[arg-type]

        return Profile(
            family=str(spec["family"]),
            named_conf=named_conf,
            zones_conf=zones_conf,
            zone_dir=base / Path(spec["zone_dir"]).relative_to("/"),  # type: ignore[arg-type]
            rndc_key=rndc_key if _is_file(rndc_key) else None,
            service=str(spec["service"]),
            zones_conf_included=included,
            zones_conf_exists=_is_file(zones_conf),
        )
    return None


def apply_profile(cfg: BindConfig, profile: Profile | None, *, explicit: set[str]) -> BindConfig:
    """감지 결과를 설정에 채운다. 사용자가 명시한 값은 건드리지 않는다."""
    if profile is None:
        return cfg
    updates: dict[str, object] = {}
    if "named_conf" not in explicit:
        updates["named_conf"] = profile.named_conf
    if "zones_conf" not in explicit:
        updates["zones_conf"] = profile.zones_conf
    if "zone_dir" not in explicit and cfg.zone_dir is None:
        updates["zone_dir"] = profile.zone_dir
    if "rndc_key" not in explicit and cfg.rndc_key is None and profile.rndc_key is not None:
        updates["rndc_key"] = profile.rndc_key
    return replace(cfg, **updates) if updates else cfg
