# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""설정 로딩.

BIND 의 디스크 레이아웃을 가정하지 않는다. 모든 경로·명령은 설정값이며,
Rocky Linux 9 기본값을 기본 프로필로 제공한다.

우선순위: 환경변수 DNS_MANAGER_CONFIG 가 가리키는 TOML > /etc/dns-manager/config.toml > 기본값
개별 값은 DNS_MANAGER_<SECTION>_<KEY> 환경변수로도 덮어쓸 수 있다(개발·테스트용).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_PATHS = (Path("/etc/dns-manager/config.toml"),)


@dataclass(frozen=True)
class BindConfig:
    """관리 대상 BIND 의 레이아웃과 제어 명령."""

    named_conf: Path = Path("/etc/named.conf")
    # zone 블록을 추가/삭제할 include 파일. named.conf 에서 include 되어 있어야 한다.
    zones_conf: Path = Path("/etc/named/zones.conf")
    # zone 파일 기본 디렉터리. 비우면 named.conf 의 options.directory 를 사용한다.
    zone_dir: Path | None = None
    # chroot 경로 (미사용 시 None)
    chroot: Path | None = None

    # zone 파일 확장자 규칙. New Zone 마법사의 기본 파일명을 이 규칙으로 만든다.
    # 정방향과 역방향을 다른 확장자로 두면 디렉터리만 봐도 종류가 구분된다.
    zone_suffix: str = ".zone"
    reverse_suffix: str = ".rev"

    named_checkconf: str = "named-checkconf"
    named_checkzone: str = "named-checkzone"
    rndc: str = "rndc"
    rndc_key: Path | None = None
    dig: str = "dig"

    # 외부 명령 타임아웃(초)
    command_timeout: float = 20.0


@dataclass(frozen=True)
class AppConfig:
    """앱 자체 설정."""

    # 전 인터페이스 수신. 폐쇄망 내부에서 필요할 때만 띄우는 온디맨드 도구이므로
    # 앱 자체 인증은 두지 않는다.
    host: str = "0.0.0.0"  # noqa: S104 - 의도된 설정값
    port: int = 8100
    # 앞단에 프록시를 두는 경우에만 사용. 전달된 사용자명을 감사 로그 주체로 기록한다.
    user_header: str = "X-Forwarded-User"
    state_dir: Path = Path("/var/lib/dns-manager")
    backup_dir: Path = Path("/var/lib/dns-manager/backups")
    # 보기 기본값: Windows DNS Manager 의 View ▸ Advanced 에 해당
    advanced_view_default: bool = False


@dataclass(frozen=True)
class Config:
    bind: BindConfig = field(default_factory=BindConfig)
    app: AppConfig = field(default_factory=AppConfig)
    source: Path | None = None
    # 자동 감지된 배포판 레이아웃 (redhat | debian | None)
    detected_family: str | None = None
    # 설정을 그대로 쓸 수 없어 보정했거나, 운영자가 손봐야 하는 사항
    warnings: tuple[str, ...] = ()


def _coerce(target_type: object, raw: object) -> object:
    """TOML/환경변수 문자열을 데이터클래스 필드 타입으로 변환."""
    type_name = getattr(target_type, "__name__", str(target_type))
    optional_path = "Path" in str(target_type) and "None" in str(target_type)

    if type_name == "Path" or optional_path:
        if raw in (None, ""):
            return None if optional_path else raw
        return Path(str(raw))
    if type_name == "int":
        return int(raw)  # type: ignore[arg-type]
    if type_name == "float":
        return float(raw)  # type: ignore[arg-type]
    if type_name == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return raw


def _build(cls, values: dict[str, object], env_prefix: str) -> tuple[object, set[str]]:
    """설정 객체와 '사용자가 명시한 키' 집합을 함께 돌려준다.

    명시한 값은 자동 감지가 덮어쓰지 않아야 한다.
    """
    kwargs: dict[str, object] = {}
    for f in fields(cls):
        env_key = f"{env_prefix}_{f.name.upper()}"
        if env_key in os.environ:
            raw: object = os.environ[env_key]
        elif f.name in values:
            raw = values[f.name]
        else:
            continue
        kwargs[f.name] = _coerce(f.type, raw)
    return cls(**kwargs), set(kwargs)


def load_config(path: Path | None = None, *, detect_layout: bool = True) -> Config:
    """설정 파일을 읽어 Config 를 만든다. 파일이 없으면 기본값 + 환경변수.

    명시되지 않은 BIND 경로는 설치된 레이아웃을 감지해 채운다
    (RedHat 계열 /etc/named.conf, Debian 계열 /etc/bind/named.conf).
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    elif env_path := os.environ.get("DNS_MANAGER_CONFIG"):
        candidates.append(Path(env_path))
    else:
        candidates.extend(DEFAULT_CONFIG_PATHS)

    data: dict[str, object] = {}
    source: Path | None = None
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            with candidate.open("rb") as fh:
                data = tomllib.load(fh)
        except PermissionError as exc:
            # 서비스 계정이 설정을 못 읽는 상황은 조용히 기본값으로 넘어가면 더 혼란스럽다.
            raise PermissionError(
                f"설정 파일을 읽을 수 없습니다: {candidate} "
                f"(실행 계정에 읽기 권한과 상위 디렉터리 진입 권한이 필요합니다)"
            ) from exc
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"설정 파일 문법 오류: {candidate}: {exc}") from exc
        source = candidate
        from .core import fileaudit

        fileaudit.record(candidate, "read", "app-config")
        break

    bind_values = data.get("bind") or {}
    app_values = data.get("app") or {}
    if not isinstance(bind_values, dict) or not isinstance(app_values, dict):
        raise ValueError(f"설정 형식 오류: {source}")

    bind, explicit = _build(BindConfig, bind_values, "DNS_MANAGER_BIND")
    app, _ = _build(AppConfig, app_values, "DNS_MANAGER_APP")

    family: str | None = None
    warnings: list[str] = []
    if detect_layout:
        from .core import detect as detect_mod

        profile = detect_mod.detect()
        if profile is not None:
            bind = detect_mod.apply_profile(bind, profile, explicit=explicit)
            family = profile.family
        bind, warnings = _repair_dead_paths(bind, profile, explicit)

    return Config(
        bind=bind, app=app, source=source, detected_family=family, warnings=tuple(warnings)
    )


def _exists(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return Path(path).exists()
    except OSError:
        return False


def _repair_dead_paths(bind: BindConfig, profile, explicit: set[str]) -> tuple[BindConfig, list[str]]:
    """설정에 적힌 경로가 사라졌으면 감지 결과로 되돌린다.

    **없는 경로는 설정이 아니라 고장이다.** 서버를 옮기거나 BIND 를 재설치하면
    예전 경로가 그대로 남아, 앱이 지워진 파일을 계속 들여다보며 "zone 이 없다" 고만 한다.
    그 상태를 조용히 유지하는 것보다, 감지 결과로 돌아가고 그 사실을 알리는 쪽이 낫다.
    """
    from dataclasses import replace

    warnings: list[str] = []
    updates: dict[str, object] = {}

    if "named_conf" in explicit and not _exists(bind.named_conf):
        if profile is not None and _exists(profile.named_conf):
            updates["named_conf"] = profile.named_conf
            warnings.append(
                f"설정의 named.conf 가 없어({bind.named_conf}) 감지한 경로를 씁니다: {profile.named_conf}"
            )
        else:
            warnings.append(
                f"설정의 named.conf 를 찾을 수 없습니다: {bind.named_conf}. "
                f"Settings 에서 경로를 지정하세요."
            )

    if "zones_conf" in explicit and not _exists(bind.zones_conf):
        if profile is not None and _exists(profile.zones_conf):
            updates["zones_conf"] = profile.zones_conf
            warnings.append(
                f"설정의 zone 정의 파일이 없어({bind.zones_conf}) 감지한 경로를 씁니다: "
                f"{profile.zones_conf}"
            )
        else:
            warnings.append(
                f"zone 정의 파일을 찾을 수 없습니다: {bind.zones_conf}. "
                f"zone 을 추가·삭제하려면 이 파일이 named.conf 에서 include 되어 있어야 합니다."
            )

    if bind.zone_dir is not None and not _exists(bind.zone_dir):
        if profile is not None and _exists(profile.zone_dir):
            updates["zone_dir"] = profile.zone_dir
            warnings.append(
                f"설정의 zone 디렉터리가 없어({bind.zone_dir}) 감지한 경로를 씁니다: {profile.zone_dir}"
            )
        else:
            warnings.append(f"zone 디렉터리를 찾을 수 없습니다: {bind.zone_dir}")

    if bind.rndc_key is not None and not _exists(bind.rndc_key):
        updates["rndc_key"] = profile.rndc_key if profile and profile.rndc_key else None
        warnings.append(f"설정의 rndc 키 파일이 없습니다: {bind.rndc_key}")

    return (replace(bind, **updates) if updates else bind), warnings
