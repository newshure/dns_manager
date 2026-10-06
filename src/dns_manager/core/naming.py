# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""zone 파일명 규칙.

정방향은 `<zone>.zone`, 역방향은 `<zone>.rev` 를 기본으로 한다(확장자는 설정값).
New Zone 마법사의 기본 파일명과, 기존 파일이 규칙을 따르는지 판정하는 데 쓴다.

파일명 규칙은 BIND 가 강제하지 않는다 — named.conf 의 `file` 지시자가 진실이다.
따라서 규칙에 어긋나는 기존 파일을 거부하지 않고, 새로 만들 때만 규칙을 적용한다.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from ..config import BindConfig

REVERSE_SUFFIXES = (".in-addr.arpa", ".ip6.arpa")


def is_reverse_zone(name: str) -> bool:
    return name.lower().rstrip(".").endswith(REVERSE_SUFFIXES)


def suffix_for(cfg: BindConfig, zone_name: str) -> str:
    """zone 종류에 맞는 확장자."""
    return cfg.reverse_suffix if is_reverse_zone(zone_name) else cfg.zone_suffix


def default_file_name(cfg: BindConfig, zone_name: str) -> str:
    """zone 이름으로부터 기본 zone 파일명을 만든다.

    예) example.local        -> example.local.zone
        10.168.192.in-addr.arpa -> 10.168.192.in-addr.arpa.rev
    """
    base = zone_name.strip().rstrip(".")
    if not base:
        raise ValueError("zone 이름이 비어 있습니다.")
    # 경로 구분자가 섞인 이름은 파일 경로 탈출로 이어질 수 있다.
    if "/" in base or base in {".", ".."}:
        raise ValueError(f"zone 이름으로 쓸 수 없습니다: {zone_name!r}")
    return f"{base}{suffix_for(cfg, base)}"


def follows_convention(cfg: BindConfig, zone_name: str, file_path: str | PurePosixPath) -> bool:
    """기존 zone 파일이 확장자 규칙을 따르는지. 경고 표시용이며 편집을 막지 않는다."""
    return PurePosixPath(str(file_path)).name.endswith(suffix_for(cfg, zone_name))
